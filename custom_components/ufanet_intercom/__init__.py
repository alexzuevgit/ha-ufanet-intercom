"""Home Assistant runtime for dynamically discovered Ufanet intercom doors."""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import UfanetClient
from .const import (
    CONF_CONTRACT,
    CONF_IDENTITY_KEY,
    CONF_PASSWORD,
    CONF_REQUIRES_ACK,
    CONF_TRUSTED_BINDINGS,
    IDENTITY_KEY_BYTES,
    PLATFORMS,
)
from .coordinator import UfanetCoordinator


@dataclass(slots=True)
class UfanetRuntimeData:
    """Runtime objects owned by one config entry."""

    client: UfanetClient
    coordinator: UfanetCoordinator
    read_session: ClientSession
    open_session: ClientSession


type UfanetConfigEntry = ConfigEntry[UfanetRuntimeData]

_SESSION_TIMEOUT = ClientTimeout(total=15, connect=5, sock_read=10)
_ACKNOWLEDGEMENT_REQUIRED = "Ufanet intercom acknowledgement is required"


def _decode_identity_key(encoded: object) -> bytes:
    """Decode only the canonical URL-safe encoding of an exact 32-byte key."""

    if type(encoded) is not str:
        raise ValueError("Invalid stored identity key.")
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError):
        raise ValueError("Invalid stored identity key.") from None
    canonical = base64.urlsafe_b64encode(raw).decode("ascii")
    if len(raw) != IDENTITY_KEY_BYTES or encoded != canonical:
        raise ValueError("Invalid stored identity key.")
    return raw


def _disable_implicit_retry(session: ClientSession) -> bool:
    """Disable only an existing, inspectable aiohttp retry mechanism."""

    sentinel = object()
    try:
        retry_connection = getattr(session, "_retry_connection", sentinel)
        if type(retry_connection) is not bool:
            return False
        session._retry_connection = False  # type: ignore[attr-defined]
        return getattr(session, "_retry_connection", sentinel) is False
    except Exception:  # noqa: BLE001
        return False


def _new_session() -> ClientSession:
    """Create a raw, code-owned session with a private connection pool."""

    session = ClientSession(
        connector=TCPConnector(),
        timeout=_SESSION_TIMEOUT,
        middlewares=(),
    )
    _disable_implicit_retry(session)
    return session


def _sessions_are_safe(read_session: object, open_session: object) -> bool:
    """Verify the complete read/physical transport separation contract."""

    try:
        sessions = (read_session, open_session)
        return (
            type(read_session) is ClientSession
            and type(open_session) is type(read_session)
            and read_session is not open_session
            and type(read_session.connector) is TCPConnector
            and type(open_session.connector) is TCPConnector
            and read_session.connector is not open_session.connector
            and all(session._retry_connection is False for session in sessions)
            and all(type(session._middlewares) in (tuple, list) for session in sessions)
            and all(not session._middlewares for session in sessions)
            and all(
                not any(
                    str(name).lower() == "authorization" for name in session.headers
                )
                for session in sessions
            )
            and all(session.timeout.total is not None for session in sessions)
            and all(0 < session.timeout.total <= 30 for session in sessions)
        )
    except Exception:  # noqa: BLE001
        return False


async def _async_close_sessions(*sessions: ClientSession | None) -> None:
    await asyncio.gather(
        *(session.close() for session in sessions if session is not None)
    )


async def _async_drain_and_close(runtime: UfanetRuntimeData) -> None:
    """Finish active operations before closing their isolated transports."""

    try:
        await runtime.client.async_drain()
    finally:
        await _async_close_sessions(runtime.read_session, runtime.open_session)


async def async_setup_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> bool:
    """Set up a validated Ufanet account without performing physical actions."""

    if entry.data.get(CONF_REQUIRES_ACK) is not False:
        raise ConfigEntryNotReady(_ACKNOWLEDGEMENT_REQUIRED)

    read_session: ClientSession | None = None
    open_session: ClientSession | None = None
    try:
        read_session = _new_session()
        open_session = _new_session()
        if not _sessions_are_safe(read_session, open_session):
            raise ConfigEntryNotReady("Safe isolated HTTP transports are unavailable")
        identity_key = _decode_identity_key(entry.data.get(CONF_IDENTITY_KEY))
        trusted_bindings = entry.data.get(CONF_TRUSTED_BINDINGS)
        if not isinstance(trusted_bindings, Mapping):
            raise ValueError("Invalid trusted bindings.")  # noqa: TRY004
        client = UfanetClient(
            read_session,
            entry.data[CONF_CONTRACT],
            entry.data[CONF_PASSWORD],
            identity_key=identity_key,
            open_session=open_session,
            trusted_bindings=trusted_bindings,
        )
        coordinator = UfanetCoordinator(hass, entry, client)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = UfanetRuntimeData(
            client=client,
            coordinator=coordinator,
            read_session=read_session,
            open_session=open_session,
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await _async_close_sessions(read_session, open_session)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> bool:
    """Unload entities; no physical command is ever performed here."""

    runtime = entry.runtime_data
    runtime.client.begin_close()
    try:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except BaseException:
        runtime.client.cancel_close()
        raise
    if not unloaded:
        runtime.client.cancel_close()
        return False

    cleanup_task = asyncio.create_task(_async_drain_and_close(runtime))
    first_cancellation: asyncio.CancelledError | None = None
    cleanup_error: BaseException | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as error:
            if first_cancellation is None:
                first_cancellation = error
        except BaseException as error:  # noqa: BLE001
            cleanup_error = error
            break

    if cleanup_error is None:
        try:
            cleanup_task.result()
        except BaseException as error:  # noqa: BLE001
            cleanup_error = error

    if first_cancellation is not None:
        first_cancellation.__cause__ = None
        first_cancellation.__context__ = None
        raise first_cancellation from None
    if cleanup_error is not None:
        cleanup_error.__cause__ = None
        cleanup_error.__context__ = None
        raise cleanup_error from None
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> bool:
    """Migrate legacy entries without discovery or any physical command."""

    version = entry.version
    if type(version) is not int:
        return False
    if version == 2:
        return True
    if version != 1:
        return False

    data = dict(entry.data)
    data[CONF_IDENTITY_KEY] = base64.urlsafe_b64encode(
        secrets.token_bytes(IDENTITY_KEY_BYTES)
    ).decode("ascii")
    data[CONF_TRUSTED_BINDINGS] = {}
    data[CONF_REQUIRES_ACK] = True
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True
