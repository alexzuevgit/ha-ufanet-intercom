"""Integration-owned admin settings panel; metadata and persistence are separate."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant

from .config_flow import _stored_voice_root
from .const import DOMAIN
from .voice_models import async_discover_models, endpoint_origin, valid_model_id
from .voice_runtime import (
    VOICE_PHRASE_OPTIONS_ROOT,
    VOICE_PHRASE_STORAGE_VERSION,
    parse_voice_phrase_options,
)
from .voice_stt import SttConfig

PANEL_PATH = "ufanet-settings"
ASSET_PATH = "/ufanet_intercom/ufanet-settings.js"
MODULE_URL = f"{ASSET_PATH}?v=2.1.0b7"
_DATA = f"{DOMAIN}_settings_panel"
_BUSY = f"{DOMAIN}_settings_busy"
_REVISION_KEY = secrets.token_bytes(32)
_MAX_BODY = 16 * 1024
_FIELDS = {"enabled", "endpoint", "token", "model", "allow_insecure_http"}
_REQUEST_FIELDS = _FIELDS | {"revision", "token_action", "confirm_token_origin"}


class SettingsError(Exception):
    """Fixed public error, never constructed from a private exception."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        payload, status=status, headers={"Cache-Control": "no-store"}
    )


def _revision(entry: Any) -> str:
    """Opaque process-local revision covering other Options/account edits too."""
    encoded = json.dumps(
        [entry.entry_id, dict(entry.data), dict(entry.options)],
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: dict(value) if isinstance(value, Mapping) else None,
    ).encode()
    return hmac.new(_REVISION_KEY, encoded, hashlib.sha256).hexdigest()


def _settings(entry: Any) -> dict[str, Any]:
    root = _stored_voice_root(entry.options)
    if root is None:
        # Do not silently overwrite damaged service/phrase storage.
        existing = entry.options.get(VOICE_PHRASE_OPTIONS_ROOT, {})
        if isinstance(existing, Mapping) and "endpoint" in existing:
            raise SettingsError("invalid_stt")
        result = {
            "enabled": False,
            "endpoint": "",
            "token": "",
            "model": "",
            "allow_insecure_http": False,
        }
    else:
        result = {key: root[key] for key in _FIELDS}
    return {
        **result,
        "revision": _revision(entry),
        "has_targets": bool(root and root["targets"]),
    }


async def _body(request: web.Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > _MAX_BODY:
        raise SettingsError("invalid_request")
    try:
        async with asyncio.timeout(5):
            body = bytearray()
            while chunk := await request.content.read(
                min(4096, _MAX_BODY + 1 - len(body))
            ):
                body.extend(chunk)
                if len(body) > _MAX_BODY:
                    raise SettingsError("invalid_request")
            value = json.loads(body)
        if type(value) is not dict or set(value) != _REQUEST_FIELDS:
            raise SettingsError("invalid_request")
        return value
    except asyncio.CancelledError:
        raise
    except Exception:
        raise SettingsError("invalid_request") from None


def _validate(entry: Any, payload: dict[str, Any], *, checking: bool) -> dict[str, Any]:
    if type(payload["revision"]) is not str or payload["revision"] != _revision(entry):
        raise SettingsError("stale_revision", 409)
    if (
        any(
            type(payload[key]) is not bool
            for key in ("enabled", "allow_insecure_http", "confirm_token_origin")
        )
        or type(payload["token_action"]) is not str
        or payload["token_action"] not in ("keep", "replace")
        or type(payload["token"]) is not str
    ):
        raise SettingsError("invalid_request")
    stored = _stored_voice_root(entry.options)
    _settings(entry)  # Fail closed on malformed stored configuration.
    previous_key = "" if stored is None else stored["token"]
    token = previous_key if payload["token_action"] == "keep" else payload["token"]
    if payload["token_action"] == "replace" and token and set(token) <= {"*", "•", "●"}:
        raise SettingsError("invalid_stt")
    try:
        # Validate endpoint/TLS before considering retained credentials.
        config = SttConfig(
            endpoint=payload["endpoint"],
            token=token,
            allow_insecure_http=payload["allow_insecure_http"],
        )
    except ValueError:
        raise SettingsError("invalid_stt") from None
    if (
        previous_key
        and token == previous_key
        and endpoint_origin(config.endpoint) != endpoint_origin(stored["endpoint"])
        and not payload["confirm_token_origin"]
    ):
        raise SettingsError("token_origin_changed")
    pause = bool(
        stored is not None
        and stored["enabled"] is True
        and payload["enabled"] is False
        and all(
            payload[key] == stored[key]
            for key in ("endpoint", "model", "allow_insecure_http")
        )
        and token == previous_key
    )
    if not checking and not pause and not valid_model_id(payload["model"]):
        raise SettingsError("invalid_model")
    if checking and (type(payload["model"]) is not str or len(payload["model"]) > 128):
        raise SettingsError("invalid_model")
    targets = {} if stored is None else copy.deepcopy(stored["targets"])
    if not checking and payload["enabled"] and not targets:
        raise SettingsError("no_voice_targets")
    return {
        "version": VOICE_PHRASE_STORAGE_VERSION,
        "enabled": payload["enabled"],
        "endpoint": config.endpoint,
        "token": config.token,
        "model": payload["model"],
        "allow_insecure_http": config.allow_insecure_http,
        "targets": targets,
    }


class SettingsView(HomeAssistantView):
    """Read a saved key only for the authenticated admin's exact domain entry."""

    url = "/api/ufanet_intercom/settings/{entry_id}"
    name = "api:ufanet_intercom:settings"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _entry(self, request: web.Request, entry_id: str) -> Any:
        user = request.get("hass_user")
        if user is None:
            raise SettingsError("unauthorized", 401)
        if not user.is_admin:
            raise SettingsError("forbidden", 403)
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise SettingsError("not_found", 404)
        return entry

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        """Opening settings performs no external requests or runtime work."""
        try:
            return _json(_settings(self._entry(request, entry_id)))
        except SettingsError as error:
            return _json({"error": error.code}, error.status)
        except Exception:
            return _json({"error": "invalid_stt"}, 400)

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        """Persist service fields atomically with respect to the HA event loop."""
        return await self._post(request, entry_id, checking=False)

    async def _post(
        self, request: web.Request, entry_id: str, *, checking: bool
    ) -> web.Response:
        owned = False
        busy = self.hass.data.setdefault(_BUSY, set())
        try:
            entry = self._entry(request, entry_id)
            if entry_id in busy:
                raise SettingsError("busy", 409)
            busy.add(entry_id)
            owned = True
            payload = await _body(request)
            if self._entry(request, entry_id) is not entry:
                raise SettingsError("stale_revision", 409)
            root = _validate(entry, payload, checking=checking)
            if checking:
                catalog = await async_discover_models(
                    SttConfig(
                        endpoint=root["endpoint"],
                        token=root["token"],
                        allow_insecure_http=root["allow_insecure_http"],
                    )
                )
                if (
                    self._entry(request, entry_id) is not entry
                    or _revision(entry) != payload["revision"]
                ):
                    raise SettingsError("stale_revision", 409)
                return _json({"models": list(catalog.models), "error": catalog.error})
            options = {**entry.options, VOICE_PHRASE_OPTIONS_ROOT: root}
            parse_voice_phrase_options(options)
            # No awaits between revision validation and write; unrelated options,
            # targets and all account data remain untouched. Normal entry listener
            # applies the explicit saved enable/disable setting.
            self.hass.config_entries.async_update_entry(entry, options=options)
            return _json(_settings(entry))
        except asyncio.CancelledError:
            raise
        except SettingsError as error:
            return _json({"error": error.code}, error.status)
        except Exception:
            return _json({"error": "invalid_stt"}, 400)
        finally:
            if owned:
                busy.discard(entry_id)


class ModelsView(SettingsView):
    """Check only metadata using the unsaved current draft; model is optional."""

    url = "/api/ufanet_intercom/settings/{entry_id}/models"
    name = "api:ufanet_intercom:settings:models"

    # Do not expose the saved key from a second read route.
    get = None

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        return await self._post(request, entry_id, checking=True)


async def async_register_settings_panel(hass: HomeAssistant) -> None:
    """One HA-global registration, retained across account reloads/removals."""
    from homeassistant.components import panel_custom

    lock = hass.data.setdefault(_DATA, asyncio.Lock())
    async with lock:
        if hass.data.get(f"{_DATA}_registered"):
            return
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    ASSET_PATH,
                    str(Path(__file__).parent / "frontend/ufanet-settings.js"),
                    False,
                )
            ]
        )
        hass.http.register_view(SettingsView(hass))
        hass.http.register_view(ModelsView(hass))
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_PATH,
            webcomponent_name="ufanet-settings-panel",
            module_url=MODULE_URL,
            require_admin=True,
            config_panel_domain=DOMAIN,
            embed_iframe=False,
            sidebar_title=None,
        )
        hass.data[f"{_DATA}_registered"] = True
