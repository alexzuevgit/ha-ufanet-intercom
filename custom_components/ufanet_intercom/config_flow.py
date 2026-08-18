"""Privacy-safe config, reauthentication, and target-adoption flows."""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
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
    """Adopt newly discovered or changed bindings without selecting targets."""

    _candidate: _AdoptionCandidate | None = None

    def _show_adoption(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        """Show only pending/new/changed aggregate counts."""

        candidate = self._candidate
        if candidate is None:
            return self.async_show_form(
                step_id="init",
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
            step_id="init",
            data_schema=_EMPTY_SCHEMA,
            errors={"base": error},
        )

    async def async_step_init(
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
