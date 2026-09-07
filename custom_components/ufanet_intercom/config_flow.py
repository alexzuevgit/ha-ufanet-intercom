"""Privacy-safe config, reauthentication, and target-adoption flows."""

from __future__ import annotations

import asyncio
import base64
import copy
import re
import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import _decode_identity_key, _new_session
from .api import (
    UfanetAuthenticationError,
    UfanetClient,
    UfanetConnectionError,
    UfanetDiscoveryError,
    UfanetError,
)
from .const import (
    CONF_CONTRACT,
    CONF_IDENTITY_KEY,
    CONF_PASSWORD,
    CONF_REQUIRES_ACK,
    CONF_TRUSTED_BINDINGS,
    DOMAIN,
    IDENTITY_KEY_BYTES,
    DiscoveredDoor,
    contract_fingerprint,
)
from .voice_models import async_discover_models, endpoint_origin, valid_model_id
from .voice_phrase import PhraseConfigError, PhraseKdfError, encode_phrase_set
from .voice_runtime import (
    MAX_VOICE_TARGETS,
    VOICE_PHRASE_OPTIONS_ROOT,
    VOICE_PHRASE_STORAGE_VERSION,
    parse_voice_phrase_options,
)
from .voice_stt import SttConfig

_ACKNOWLEDGE: Final = "acknowledge"
_HMAC_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_FLOW_IDENTITY_KEY: Final = secrets.token_bytes(IDENTITY_KEY_BYTES)
_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTRACT): str,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR})
_ACKNOWLEDGEMENT_SCHEMA = vol.Schema({vol.Required(_ACKNOWLEDGE): bool})
_EMPTY_SCHEMA = vol.Schema({})
_VOICE_ENABLED = "enabled"
_VOICE_ENDPOINT = "endpoint"
_VOICE_TOKEN = "token"
_VOICE_MODEL = "model"
_VOICE_ALLOW_HTTP = "allow_insecure_http"
_VOICE_TARGET = "target"
_VOICE_PHRASES = "phrases"
_VOICE_CLEAR_PHRASES = "clear_phrases"
_VOICE_MENU = ["voice_service", "voice_device", "bindings", "voice_reset"]
_STT_TOKEN_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="new-password")
)
_PHRASES_SELECTOR = TextSelector(TextSelectorConfig(multiline=True))


def _active_contract_flow_id(contract: str) -> str:
    """Return one process-local, non-reversible ID for concurrent-flow gating."""

    return f"active-{contract_fingerprint(_ACTIVE_FLOW_IDENTITY_KEY, contract)}"


@dataclass(slots=True, repr=False)
class _InitialCandidate:
    """Validated setup state that must never be included in a flow result."""

    contract: str = field(repr=False)
    password: str = field(repr=False)
    identity_key: bytes = field(repr=False)
    encoded_identity_key: str = field(repr=False)
    trusted_bindings: dict[str, str] = field(repr=False)
    discovered_count: int
    distinct_name_count: int
    duplicate_name_count: int


@dataclass(slots=True, repr=False)
class _AdoptionCandidate:
    """Pending private bindings and safe aggregate counts."""

    contract: str = field(repr=False)
    password: str = field(repr=False)
    identity_key: bytes = field(repr=False)
    pending_bindings: dict[str, str] = field(repr=False)
    pending_count: int
    new_count: int
    changed_count: int


@dataclass(slots=True, repr=False)
class _VoiceCandidate:
    """Private pending service data and administrator-editable target records."""

    endpoint: str = field(repr=False)
    token: str = field(repr=False)
    model: str = field(repr=False)
    allow_insecure_http: bool
    targets: dict[str, dict[str, object]] = field(repr=False)
    enabled: bool = True


@dataclass(slots=True, repr=False)
class _VoiceTargetCandidate:
    """Exact current trusted target selected for phrase replacement."""

    key: str = field(repr=False)
    binding: str = field(repr=False)


def _canonical_identity_key(identity_key: bytes) -> str:
    """Encode an identity key in the one accepted persistent representation."""

    if type(identity_key) is not bytes or len(identity_key) != IDENTITY_KEY_BYTES:
        raise ValueError("Invalid identity key.")
    return base64.urlsafe_b64encode(identity_key).decode("ascii")


def _stored_trusted_bindings(value: object) -> dict[str, str]:
    """Copy a strictly valid stored binding mapping or fail closed."""

    if not isinstance(value, Mapping):
        raise TypeError("Invalid trusted bindings.")
    bindings = dict(value)
    if any(
        type(key) is not str
        or _HMAC_HEX_RE.fullmatch(key) is None
        or type(binding) is not str
        or _HMAC_HEX_RE.fullmatch(binding) is None
        for key, binding in bindings.items()
    ):
        raise ValueError("Invalid trusted bindings.")
    return bindings


async def _async_login_and_discover(
    contract: str,
    password: str,
    identity_key: bytes,
    trusted_bindings: Mapping[str, str],
) -> Mapping[str, DiscoveredDoor]:
    """Use and close one isolated read transport for a fresh discovery."""

    session = _new_session()
    try:
        client = UfanetClient(
            session,
            contract,
            password,
            identity_key=identity_key,
            trusted_bindings=trusted_bindings,
        )
        return await client.async_login_and_discover()
    finally:
        await session.close()


async def _async_discover_with_error(
    contract: str,
    password: str,
    identity_key: bytes,
    trusted_bindings: Mapping[str, str],
) -> tuple[Mapping[str, DiscoveredDoor] | None, str | None]:
    """Return only discovered private state or a fixed public error code."""

    try:
        discovered = await _async_login_and_discover(
            contract,
            password,
            identity_key,
            trusted_bindings,
        )
    except asyncio.CancelledError:
        raise
    except UfanetAuthenticationError:
        return None, "invalid_auth"
    except UfanetDiscoveryError:
        return None, "invalid_targets"
    except UfanetConnectionError:
        return None, "cannot_connect"
    except UfanetError:
        return None, "unknown"
    except Exception:  # noqa: BLE001
        return None, "unknown"
    return discovered, None


def _bindings_and_name_counts(
    discovered: Mapping[str, DiscoveredDoor],
) -> tuple[dict[str, str], int, int, int]:
    """Extract private bindings and only the safe name aggregates."""

    bindings: dict[str, str] = {}
    names: set[str] = set()
    discovered_count = 0
    for door in discovered.values():
        if door.key in bindings:
            raise ValueError("Duplicate discovered target.")
        bindings[door.key] = door.binding
        names.add(door.display_name)
        discovered_count += 1
    distinct_name_count = len(names)
    return (
        bindings,
        discovered_count,
        distinct_name_count,
        discovered_count - distinct_name_count,
    )


def _is_exact_acknowledgement(user_input: dict[str, Any] | None) -> bool:
    """Accept no truthy lookalikes or additional fields."""

    return (
        user_input is not None
        and set(user_input) == {_ACKNOWLEDGE}
        and user_input[_ACKNOWLEDGE] is True
    )


def _stored_voice_root(options: object) -> dict[str, object] | None:
    """Return validated active or paused settings without starting any work."""

    try:
        parse_voice_phrase_options(options)
        if type(options) not in (dict, MappingProxyType):
            return None
        root = options[VOICE_PHRASE_OPTIONS_ROOT]
        if type(root) is not dict or "endpoint" not in root:
            return None
        return copy.deepcopy(root)
    except Exception:  # noqa: BLE001 - malformed optional data is not reused
        return None


def _voice_root(candidate: _VoiceCandidate) -> dict[str, object]:
    """Build active or paused storage, retaining private configuration records."""

    return {
        "version": VOICE_PHRASE_STORAGE_VERSION,
        "enabled": candidate.enabled and bool(candidate.targets),
        "endpoint": candidate.endpoint,
        "token": candidate.token,
        "model": candidate.model,
        "allow_insecure_http": candidate.allow_insecure_http,
        "targets": copy.deepcopy(candidate.targets),
    }


def _voice_service_schema(root: dict[str, object] | None) -> vol.Schema:
    """Build service form defaults without ever pre-filling the secret token."""

    return vol.Schema(
        {
            vol.Required(
                _VOICE_ENABLED, default=False if root is None else root["enabled"]
            ): bool,
            vol.Required(
                _VOICE_ENDPOINT,
                default="" if root is None else root["endpoint"],
            ): str,
            vol.Optional(_VOICE_TOKEN, default=""): _STT_TOKEN_SELECTOR,
            vol.Required(
                _VOICE_ALLOW_HTTP,
                default=False if root is None else root["allow_insecure_http"],
            ): bool,
        }
    )


def _phrases_schema(entered: list[str] | None = None) -> vol.Schema:
    """Expose entered configuration only in the HA-admin Options editor.

    A callable default keeps the phrase text out of the schema's repr.
    Legacy hash-only records have no recoverable default.
    """

    return vol.Schema(
        {
            vol.Required(
                _VOICE_PHRASES, default=lambda: "\n".join(entered or [])
            ): _PHRASES_SELECTOR,
            vol.Required(_VOICE_CLEAR_PHRASES, default=False): bool,
        }
    )


class UfanetIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure and reauthenticate one Ufanet intercom account."""

    VERSION = 2
    MINOR_VERSION = 2

    _candidate: _InitialCandidate | None = None

    def _contract_is_configured(self, contract: str) -> bool:
        """Return whether this account already has a local config entry."""

        canonical = contract.upper()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            stored = entry.data.get(CONF_CONTRACT)
            if type(stored) is str and stored.upper() == canonical:
                return True
        return False

    async def _async_validate_initial(
        self, contract: str, password: str
    ) -> tuple[_InitialCandidate | None, str | None]:
        """Build one in-memory candidate without exposing provider metadata."""

        identity_key = secrets.token_bytes(IDENTITY_KEY_BYTES)
        discovered, error = await _async_discover_with_error(
            contract,
            password,
            identity_key,
            {},
        )
        if error is not None or discovered is None:
            return None, error or "unknown"

        try:
            encoded_identity_key = _canonical_identity_key(identity_key)
            bindings, discovered_count, distinct_count, duplicate_count = (
                _bindings_and_name_counts(discovered)
            )
            # Validate the contract before retaining credentials in the candidate.
            contract_fingerprint(identity_key, contract)
        except Exception:  # noqa: BLE001
            return None, "unknown"

        return (
            _InitialCandidate(
                contract=contract.upper(),
                password=password,
                identity_key=identity_key,
                encoded_identity_key=encoded_identity_key,
                trusted_bindings=bindings,
                discovered_count=discovered_count,
                distinct_name_count=distinct_count,
                duplicate_name_count=duplicate_count,
            ),
            None,
        )

    def _show_acknowledgement(self) -> ConfigFlowResult:
        """Show only aggregate discovery information."""

        candidate = self._candidate
        if candidate is None:
            return self.async_abort(reason="unknown")
        return self.async_show_form(
            step_id="acknowledge",
            data_schema=_ACKNOWLEDGEMENT_SCHEMA,
            description_placeholders={
                "discovered_count": str(candidate.discovered_count),
                "distinct_name_count": str(candidate.distinct_name_count),
                "duplicate_name_count": str(candidate.duplicate_name_count),
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover targets using credentials and retain a private candidate."""

        errors: dict[str, str] = {}
        if user_input is not None:
            self._candidate = None
            if (
                set(user_input) != {CONF_CONTRACT, CONF_PASSWORD}
                or type(user_input[CONF_CONTRACT]) is not str
                or type(user_input[CONF_PASSWORD]) is not str
            ):
                errors["base"] = "unknown"
            elif self._contract_is_configured(user_input[CONF_CONTRACT]):
                return self.async_abort(reason="already_configured")
            else:
                try:
                    active_flow_id = _active_contract_flow_id(user_input[CONF_CONTRACT])
                except Exception:  # noqa: BLE001
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(active_flow_id)
                    candidate, error = await self._async_validate_initial(
                        user_input[CONF_CONTRACT],
                        user_input[CONF_PASSWORD],
                    )
                    if candidate is not None:
                        self._candidate = candidate
                        return self._show_acknowledgement()
                    await self.async_set_unique_id(None, raise_on_progress=False)
                    errors["base"] = error or "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_acknowledge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist the candidate only after an exact acknowledgement."""

        candidate = self._candidate
        if candidate is None:
            return self.async_abort(reason="unknown")
        if not _is_exact_acknowledgement(user_input):
            return self._show_acknowledgement()
        if self._contract_is_configured(candidate.contract):
            self._candidate = None
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(
            contract_fingerprint(candidate.identity_key, candidate.contract),
            raise_on_progress=False,
        )
        self._abort_if_unique_id_configured()
        data = {
            CONF_CONTRACT: candidate.contract,
            CONF_PASSWORD: candidate.password,
            CONF_IDENTITY_KEY: candidate.encoded_identity_key,
            CONF_TRUSTED_BINDINGS: dict(candidate.trusted_bindings),
            CONF_REQUIRES_ACK: False,
        }
        self._candidate = None
        return self.async_create_entry(title="Ufanet Intercom", data=data)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for an existing entry."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace only the password after fresh read-only discovery."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                set(user_input) != {CONF_PASSWORD}
                or type(user_input[CONF_PASSWORD]) is not str
            ):
                errors["base"] = "unknown"
            else:
                entry = self._get_reauth_entry()
                try:
                    identity_key = _decode_identity_key(
                        entry.data.get(CONF_IDENTITY_KEY)
                    )
                    trusted_bindings = _stored_trusted_bindings(
                        entry.data.get(CONF_TRUSTED_BINDINGS)
                    )
                    contract = entry.data[CONF_CONTRACT]
                except Exception:  # noqa: BLE001
                    errors["base"] = "unknown"
                else:
                    password = user_input[CONF_PASSWORD]
                    _discovered, error = await _async_discover_with_error(
                        contract,
                        password,
                        identity_key,
                        trusted_bindings,
                    )
                    if error is None:
                        return self.async_update_reload_and_abort(
                            entry,
                            data_updates={CONF_PASSWORD: password},
                            reason="reauth_successful",
                        )
                    errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_REAUTH_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the target-adoption options flow."""

        return UfanetIntercomOptionsFlow()


class UfanetIntercomOptionsFlow(config_entries.OptionsFlow):
    """Configure optional voice recognition and adopt provider bindings."""

    _candidate: _AdoptionCandidate | None = None
    _voice_candidate: _VoiceCandidate | None = None
    _voice_target: _VoiceTargetCandidate | None = None
    _voice_models: tuple[str, ...] = ()
    _voice_model_pending: bool = False
    _voice_catalog_error: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show separate voice and binding workflows."""

        if user_input not in (None, {}):
            return self.async_abort(reason="unknown")
        return self.async_show_menu(step_id="init", menu_options=_VOICE_MENU)

    def _finish_voice(self, root: dict[str, object]) -> ConfigFlowResult:
        """Return complete options while retaining unrelated future fields."""

        entry = self.config_entry
        try:
            options = dict(entry.options)
            options[VOICE_PHRASE_OPTIONS_ROOT] = root
            parse_voice_phrase_options(options)
        except Exception:  # noqa: BLE001 - never persist an invalid private root
            return self.async_abort(reason="unknown")
        self._voice_candidate = None
        self._voice_target = None
        self._voice_model_pending = False
        self._voice_models = ()
        self._voice_catalog_error = None
        return self.async_create_entry(title="", data=options)

    def _candidate_from_stored(self) -> _VoiceCandidate | None:
        root = _stored_voice_root(self.config_entry.options)
        if root is None:
            return None
        targets = root.get("targets")
        if type(targets) is not dict:
            return None
        return _VoiceCandidate(
            endpoint=cast("str", root["endpoint"]),
            token=cast("str", root["token"]),
            model=cast("str", root["model"]),
            allow_insecure_http=cast("bool", root["allow_insecure_http"]),
            targets=cast("dict[str, dict[str, object]]", copy.deepcopy(targets)),
            enabled=cast("bool", root["enabled"]),
        )

    def _current_voice_targets(self) -> dict[str, DiscoveredDoor]:
        """Return current trusted camera targets keyed only by opaque identity."""

        try:
            runtime = self.config_entry.runtime_data
            snapshot = runtime.coordinator.data or {}
            proxy = runtime.rtsp_proxy
        except (AttributeError, TypeError):
            return {}
        result: dict[str, DiscoveredDoor] = {}
        try:
            for target in snapshot.values():
                if (
                    type(target) is DiscoveredDoor
                    and target.trusted
                    and target.cctv_number
                    and proxy.stream_url(target.key) is not None
                ):
                    result[target.key] = target
        except Exception:  # noqa: BLE001 - stale runtime snapshot fails closed
            return {}
        return result

    def _voice_target_labels(
        self, current: Mapping[str, DiscoveredDoor]
    ) -> dict[str, str]:
        """Read current HA device names without changing provider target identity."""

        registry = dr.async_get(self.hass)
        labels: dict[str, str] = {}
        for key, target in sorted(current.items()):
            label = target.display_name
            device = registry.async_get_device(identifiers={(DOMAIN, key)})
            if (
                device is not None
                and self.config_entry.entry_id in device.config_entries
            ):
                for name in (device.name_by_user, device.name):
                    if isinstance(name, str) and name.strip():
                        label = name
                        break
            labels[key] = label
        counts = Counter(labels.values())
        return {
            key: f"{label} ({key[:8]})" if counts[label] > 1 else label
            for key, label in labels.items()
        }

    async def async_step_voice_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicit Next requests metadata; unchanged true-to-false saves pause."""

        stored = _stored_voice_root(self.config_entry.options)
        errors: dict[str, str] = {}
        self._voice_model_pending = False
        self._voice_candidate = None
        self._voice_target = None
        self._voice_models = ()
        self._voice_catalog_error = None
        if user_input is not None:
            # HA omits optional empty strings; preserve the minimal pause form.
            if _VOICE_ENDPOINT in user_input and _VOICE_TOKEN not in user_input:
                user_input = {**user_input, _VOICE_TOKEN: ""}
            allowed = {_VOICE_ENABLED, _VOICE_ENDPOINT, _VOICE_TOKEN, _VOICE_ALLOW_HTTP}
            if (
                set(user_input) - allowed
                or type(user_input.get(_VOICE_ENABLED)) is not bool
            ):
                errors["base"] = "invalid_stt"
            elif user_input[_VOICE_ENABLED] is False and (
                set(user_input) == {_VOICE_ENABLED}
                or (
                    set(user_input) == allowed
                    and stored is not None
                    and stored["enabled"] is True
                    and user_input[_VOICE_ENDPOINT] == stored["endpoint"]
                    and user_input[_VOICE_TOKEN] in ("", stored["token"])
                    and user_input[_VOICE_ALLOW_HTTP] is stored["allow_insecure_http"]
                )
                or (
                    stored is None
                    and user_input
                    == {
                        _VOICE_ENABLED: False,
                        _VOICE_ENDPOINT: "",
                        _VOICE_TOKEN: "",
                        _VOICE_ALLOW_HTTP: False,
                    }
                )
            ):
                paused = {"version": VOICE_PHRASE_STORAGE_VERSION, "enabled": False}
                if stored is not None:
                    paused = {**stored, "enabled": False}
                return self._finish_voice(paused)
            elif (
                set(user_input) != allowed
                or any(
                    type(user_input[key]) is not str
                    for key in (_VOICE_ENDPOINT, _VOICE_TOKEN)
                )
                or type(user_input[_VOICE_ALLOW_HTTP]) is not bool
            ):
                errors["base"] = "invalid_stt"
            else:
                token = user_input[_VOICE_TOKEN]
                try:
                    # Validate before comparing origins or reusing any saved key.
                    validated = SttConfig(
                        endpoint=user_input[_VOICE_ENDPOINT],
                        token=token,
                        allow_insecure_http=user_input[_VOICE_ALLOW_HTTP],
                    )
                    if token == "" and stored is not None and stored["token"]:
                        if endpoint_origin(validated.endpoint) != endpoint_origin(
                            cast("str", stored["endpoint"])
                        ):
                            errors[_VOICE_TOKEN] = "token_origin_changed"
                        else:
                            validated = SttConfig(
                                endpoint=validated.endpoint,
                                token=cast("str", stored["token"]),
                                allow_insecure_http=validated.allow_insecure_http,
                            )
                except ValueError:
                    errors["base"] = "invalid_stt"
                else:
                    if errors:
                        return self.async_show_form(
                            step_id="voice_service",
                            data_schema=_voice_service_schema(user_input),
                            errors=errors,
                            last_step=False,
                        )
                    targets = (
                        {}
                        if stored is None
                        else cast(
                            "dict[str, dict[str, object]]",
                            copy.deepcopy(stored["targets"]),
                        )
                    )
                    self._voice_candidate = _VoiceCandidate(
                        endpoint=validated.endpoint,
                        token=validated.token,
                        model="" if stored is None else cast("str", stored["model"]),
                        allow_insecure_http=validated.allow_insecure_http,
                        targets=targets,
                        enabled=user_input[_VOICE_ENABLED],
                    )
                    catalog = await async_discover_models(validated)
                    self._voice_models = catalog.models
                    self._voice_catalog_error = catalog.error
                    self._voice_model_pending = True
                    return await self.async_step_voice_model()

        return self.async_show_form(
            step_id="voice_service",
            data_schema=_voice_service_schema(stored),
            errors=errors,
            last_step=False,
        )

    async def async_step_voice_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require an exact selected/manual model before saving service changes."""
        candidate = self._voice_candidate
        if candidate is None or not self._voice_model_pending:
            return self.async_abort(reason="unknown")
        errors: dict[str, str] = {}
        if user_input is not None:
            if set(user_input) != {_VOICE_MODEL} or not valid_model_id(
                user_input.get(_VOICE_MODEL)
            ):
                errors[_VOICE_MODEL] = "invalid_model"
            else:
                candidate.model = user_input[_VOICE_MODEL]
                self._voice_model_pending = False
                if candidate.targets:
                    return self._finish_voice(_voice_root(candidate))
                return await self.async_step_voice_device()
        elif self._voice_catalog_error is not None:
            errors["base"] = self._voice_catalog_error
        model_key = (
            vol.Required(_VOICE_MODEL, default=candidate.model)
            if valid_model_id(candidate.model)
            else vol.Required(_VOICE_MODEL)
        )
        return self.async_show_form(
            step_id="voice_model",
            data_schema=vol.Schema(
                {
                    model_key: SelectSelector(
                        SelectSelectorConfig(
                            options=list(self._voice_models),
                            custom_value=True,
                            mode="dropdown",
                        )
                    )
                }
            ),
            errors=errors,
            last_step=bool(candidate.targets),
        )

    async def async_step_voice_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one exact current trusted camera target."""

        if self._voice_model_pending:
            return await self.async_step_voice_model()
        candidate = self._voice_candidate or self._candidate_from_stored()
        if candidate is None:
            return self.async_show_form(
                step_id="voice_service",
                data_schema=_voice_service_schema(None),
                errors={"base": "voice_service_required"},
            )
        self._voice_candidate = candidate
        current = self._current_voice_targets()
        if not current:
            return self.async_abort(reason="no_voice_targets")
        labels = self._voice_target_labels(current)
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input.get(_VOICE_TARGET)
            target = current.get(key) if type(key) is str else None
            if set(user_input) != {_VOICE_TARGET} or target is None:
                errors["base"] = "stale_target"
            elif (
                key not in candidate.targets
                and len(candidate.targets) >= MAX_VOICE_TARGETS
            ):
                errors["base"] = "too_many_targets"
            else:
                self._voice_target = _VoiceTargetCandidate(
                    key=cast("str", key), binding=target.binding
                )
                return await self.async_step_voice_phrases()
        return self.async_show_form(
            step_id="voice_device",
            data_schema=vol.Schema({vol.Required(_VOICE_TARGET): vol.In(labels)}),
            errors=errors,
        )

    async def async_step_voice_phrases_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use the same editor with a translated one-time re-entry explanation."""

        return await self.async_step_voice_phrases(user_input)

    async def async_step_voice_phrases(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """View, replace, or explicitly clear one exact target's phrase list."""

        candidate = self._voice_candidate
        selected = self._voice_target
        if candidate is None or selected is None:
            return self.async_abort(reason="unknown")
        current_targets = self._current_voice_targets()
        current = current_targets.get(selected.key)
        if current is None or current.binding != selected.binding:
            # Do not redisplay private text after source trust/identity changes.
            self._voice_target = None
            return self.async_show_form(
                step_id="voice_phrases",
                data_schema=_phrases_schema(),
                errors={"base": "stale_target"},
                description_placeholders={"target_name": "—", "phrase_count": "0"},
            )
        stored = candidate.targets.get(selected.key)
        if stored is not None and stored["binding"] != selected.binding:
            stored = None  # A replacement camera must not inherit the old list.
        entered = (
            None
            if stored is None
            else cast("list[str] | None", stored.get("entered_phrases"))
        )
        legacy = stored is not None and entered is None
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                set(user_input)
                not in ({_VOICE_PHRASES}, {_VOICE_PHRASES, _VOICE_CLEAR_PHRASES})
                or type(user_input.get(_VOICE_PHRASES)) is not str
                or type(user_input.get(_VOICE_CLEAR_PHRASES, False)) is not bool
            ):
                errors["base"] = "invalid_phrases"
            else:
                raw = cast("str", user_input[_VOICE_PHRASES])
                phrases = [line for line in raw.splitlines() if line.strip()]
                targets = copy.deepcopy(candidate.targets)
                if user_input.get(_VOICE_CLEAR_PHRASES) is True:
                    targets.pop(selected.key, None)
                elif not phrases or phrases == entered:
                    # Blank means keep, especially for unreadable hash-only lists.
                    # An unchanged save also retains the exact salt and digests.
                    if stored is None:
                        errors["base"] = "invalid_phrases"
                else:
                    try:
                        protected = await self.hass.async_add_executor_job(
                            encode_phrase_set, phrases
                        )
                    except (PhraseConfigError, PhraseKdfError):
                        errors["base"] = "invalid_phrases"
                    except Exception:  # noqa: BLE001 - private KDF failure stays fixed
                        errors["base"] = "invalid_phrases"
                    else:
                        targets[selected.key] = {
                            "binding": selected.binding,
                            "phrases": protected,
                            "entered_phrases": phrases,
                        }
                if not errors:
                    candidate.targets = targets
                    return self._finish_voice(_voice_root(candidate))
        return self.async_show_form(
            step_id="voice_phrases_legacy" if legacy else "voice_phrases",
            data_schema=_phrases_schema(entered),
            errors=errors,
            description_placeholders={
                "target_name": self._voice_target_labels(current_targets)[selected.key],
                "phrase_count": str(
                    0
                    if stored is None
                    else len(cast("dict", stored["phrases"])["digests"])
                ),
            },
        )

    async def async_step_voice_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erase voice-only settings after explicit confirmation, not on disable."""

        if _is_exact_acknowledgement(user_input):
            return self._finish_voice(
                {"version": VOICE_PHRASE_STORAGE_VERSION, "enabled": False}
            )
        return self.async_show_form(
            step_id="voice_reset", data_schema=_ACKNOWLEDGEMENT_SCHEMA, errors={}
        )

    def _show_adoption(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        """Show only pending/new/changed aggregate counts."""

        candidate = self._candidate
        if candidate is None:
            return self.async_show_form(
                step_id="bindings",
                data_schema=_EMPTY_SCHEMA,
                errors={"base": "unknown"} if errors is None else errors,
            )
        return self.async_show_form(
            step_id="adopt",
            data_schema=_ACKNOWLEDGEMENT_SCHEMA,
            errors={} if errors is None else errors,
            description_placeholders={
                "pending_count": str(candidate.pending_count),
                "new_count": str(candidate.new_count),
                "changed_count": str(candidate.changed_count),
            },
        )

    def _show_init_error(self, error: str) -> ConfigFlowResult:
        """Show a fixed privacy-safe options error."""

        return self.async_show_form(
            step_id="bindings",
            data_schema=_EMPTY_SCHEMA,
            errors={"base": error},
        )

    async def async_step_bindings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover and classify all pending untrusted bindings."""

        self._candidate = None
        if user_input is not None and user_input != {}:
            return self._show_init_error("unknown")

        entry = self.config_entry
        try:
            identity_key = _decode_identity_key(entry.data.get(CONF_IDENTITY_KEY))
            trusted_bindings = _stored_trusted_bindings(
                entry.data.get(CONF_TRUSTED_BINDINGS)
            )
            contract = entry.data[CONF_CONTRACT]
            password = entry.data[CONF_PASSWORD]
        except Exception:  # noqa: BLE001
            return self._show_init_error("unknown")

        discovered, error = await _async_discover_with_error(
            contract,
            password,
            identity_key,
            trusted_bindings,
        )
        if error is not None or discovered is None:
            return self._show_init_error(error or "unknown")

        try:
            pending_bindings: dict[str, str] = {}
            new_count = 0
            for door in discovered.values():
                if trusted_bindings.get(door.key) == door.binding:
                    continue
                if door.key in pending_bindings:
                    raise ValueError("Duplicate discovered target.")
                pending_bindings[door.key] = door.binding
                if door.key not in trusted_bindings:
                    new_count += 1
        except Exception:  # noqa: BLE001
            return self._show_init_error("unknown")

        if not pending_bindings:
            return self.async_abort(reason="no_pending_targets")

        pending_count = len(pending_bindings)
        self._candidate = _AdoptionCandidate(
            contract=contract,
            password=password,
            identity_key=identity_key,
            pending_bindings=pending_bindings,
            pending_count=pending_count,
            new_count=new_count,
            changed_count=pending_count - new_count,
        )
        return self._show_adoption()

    async def async_step_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Merge every pending binding after an exact acknowledgement."""

        candidate = self._candidate
        if candidate is None:
            return self._show_init_error("unknown")
        if not _is_exact_acknowledgement(user_input):
            return self._show_adoption()

        entry = self.config_entry
        try:
            identity_key = _decode_identity_key(entry.data.get(CONF_IDENTITY_KEY))
            trusted_bindings = _stored_trusted_bindings(
                entry.data.get(CONF_TRUSTED_BINDINGS)
            )
            if (
                identity_key != candidate.identity_key
                or entry.data.get(CONF_CONTRACT) != candidate.contract
                or entry.data.get(CONF_PASSWORD) != candidate.password
            ):
                raise ValueError("Stored credentials changed during adoption.")
        except Exception:  # noqa: BLE001
            return self._show_adoption(errors={"base": "unknown"})

        updated_data = dict(entry.data)
        updated_data[CONF_TRUSTED_BINDINGS] = {
            **trusted_bindings,
            **candidate.pending_bindings,
        }
        updated_data[CONF_REQUIRES_ACK] = False
        self.hass.config_entries.async_update_entry(entry, data=updated_data)
        await self.hass.config_entries.async_reload(entry.entry_id)
        self._candidate = None
        return self.async_abort(reason="adoption_successful")
