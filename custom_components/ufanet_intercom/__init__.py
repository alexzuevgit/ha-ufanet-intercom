"""Home Assistant runtime for dynamically discovered Ufanet intercom doors."""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
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
from .history import CallHistoryManager, CallHistoryPoller
from .media import CameraBinding, GatewayError, MediaCredentials, UfanetMediaClient
from .rtsp_proxy import RtspProxyRuntime


@dataclass(slots=True)
class UfanetRuntimeData:
    """Runtime objects owned by one config entry."""

    client: UfanetClient
    coordinator: UfanetCoordinator
    read_session: ClientSession
    open_session: ClientSession
    history_manager: CallHistoryManager
    history_poller: CallHistoryPoller
    rtsp_proxy: RtspProxyRuntime
    voice_manager: Any | None = None
    voice_session: ClientSession | None = None
    proxy_unsubscribe: Any | None = None


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


def _session_is_safe(session: object) -> bool:
    """Verify one isolated no-retry transport without exposing its purpose."""

    try:
        return (
            type(session) is ClientSession
            and type(session.connector) is TCPConnector
            and session._retry_connection is False
            and type(session._middlewares) in (tuple, list)
            and not session._middlewares
            and not any(
                str(name).lower() == "authorization" for name in session.headers
            )
            and session.timeout.total is not None
            and 0 < session.timeout.total <= 30
        )
    except Exception:  # noqa: BLE001
        return False


def _sessions_are_safe(read_session: object, open_session: object) -> bool:
    """Verify the complete read/physical transport separation contract."""

    try:
        return (
            _session_is_safe(read_session)
            and _session_is_safe(open_session)
            and read_session is not open_session
            and read_session.connector is not open_session.connector
        )
    except Exception:  # noqa: BLE001
        return False


async def _async_close_sessions(*sessions: ClientSession | None) -> None:
    await asyncio.gather(
        *(session.close() for session in sessions if session is not None)
    )


async def _async_drain_and_close(runtime: UfanetRuntimeData) -> None:
    """Stop every owner in order and report only after terminal cleanup."""

    first_error: BaseException | None = None

    async def finish(operation: Awaitable[object]) -> None:
        nonlocal first_error
        try:
            await operation
        except BaseException as error:  # noqa: BLE001 - cleanup must continue
            if first_error is None:
                first_error = error

    voice_manager = getattr(runtime, "voice_manager", None)
    if voice_manager is not None:
        await finish(voice_manager.async_stop())
    history_poller = getattr(runtime, "history_poller", None)
    if history_poller is not None:
        await finish(history_poller.async_stop())
    await finish(runtime.client.async_drain())

    unsubscribe = getattr(runtime, "proxy_unsubscribe", None)
    if callable(unsubscribe):
        try:
            unsubscribe()
        except BaseException as error:  # noqa: BLE001 - cleanup must continue
            if first_error is None:
                first_error = error
    proxy = getattr(runtime, "rtsp_proxy", None)
    if proxy is not None:
        await finish(asyncio.to_thread(proxy.close))
    for session in (
        getattr(runtime, "voice_session", None),
        runtime.read_session,
        runtime.open_session,
    ):
        if session is not None:
            await finish(session.close())

    if first_error is not None:
        first_error.__cause__ = None
        first_error.__context__ = None
        raise first_error from None


def _enabled_voice_config(options: object) -> Any | None:
    """Return validated enabled voice options or fail this optional feature closed."""

    if type(options) not in (dict, MappingProxyType) or "voice_phrase" not in options:
        return None
    try:
        from .voice_runtime import parse_voice_phrase_options

        config = parse_voice_phrase_options(options)
    except Exception:  # noqa: BLE001 - optional malformed storage stays disabled
        return None
    return config if config.enabled else None


async def _async_build_voice_manager(
    hass: HomeAssistant,
    config: Any,
    coordinator: UfanetCoordinator,
    rtsp_proxy: RtspProxyRuntime,
) -> tuple[Any | None, ClientSession | None]:
    """Build an inert manager; allocate external resources only when FFmpeg is ready."""

    from .voice_runtime import VoicePhraseManager

    ffmpeg_binary: str | None = None
    stt_client: Any | None = None
    voice_session: ClientSession | None = None
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager
        from homeassistant.setup import async_setup_component

        if await async_setup_component(hass, "ffmpeg", {}) is True:
            candidate = get_ffmpeg_manager(hass).binary
            if type(candidate) is str and candidate and "\x00" not in candidate:
                ffmpeg_binary = candidate
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - optional infrastructure stays unavailable
        ffmpeg_binary = None

    if ffmpeg_binary is not None:
        try:
            from .voice_stt import SttClient

            stt_config = config.stt_config
            if stt_config is not None:
                voice_session = _new_session()
                if _session_is_safe(voice_session):
                    stt_client = SttClient(voice_session, stt_config)
                else:
                    await voice_session.close()
                    voice_session = None
                    ffmpeg_binary = None
        except asyncio.CancelledError:
            if voice_session is not None:
                await voice_session.close()
            raise
        except Exception:  # noqa: BLE001 - optional STT stays unavailable
            if voice_session is not None:
                await voice_session.close()
            voice_session = None
            stt_client = None
            ffmpeg_binary = None

    kwargs: dict[str, Any] = {}
    async_executor = getattr(hass, "async_add_executor_job", None)
    if callable(async_executor):
        kwargs["async_executor"] = async_executor
    try:
        manager = VoicePhraseManager(
            config,
            snapshot_provider=lambda: coordinator.data or {},
            stream_url_provider=rtsp_proxy.stream_url,
            ffmpeg_binary=ffmpeg_binary,
            stt_client=stt_client,
            **kwargs,
        )
    except asyncio.CancelledError:
        if voice_session is not None:
            await voice_session.close()
        raise
    except Exception:  # noqa: BLE001 - optional feature cannot block the entry
        if voice_session is not None:
            await voice_session.close()
        return None, None
    return manager, voice_session


async def _async_reload_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> None:
    """Reload one entry after its optional voice settings change."""

    await hass.config_entries.async_reload(entry.entry_id)


def _camera_bindings(doors: Mapping[str, Any]) -> tuple[CameraBinding, ...]:
    """Build only validated account-derived camera bindings."""

    bindings: list[CameraBinding] = []
    for door in doors.values():
        if not getattr(door, "trusted", False):
            continue
        try:
            bindings.append(CameraBinding(door.key, door.cctv_number))
        except (AttributeError, GatewayError):
            continue
    return tuple(bindings)


def _start_rtsp_proxy(
    credentials: MediaCredentials, bindings: tuple[CameraBinding, ...]
) -> RtspProxyRuntime:
    """Construct and bind the synchronous media stack outside HA's event loop."""

    media_client = UfanetMediaClient(credentials)
    try:
        proxy = RtspProxyRuntime(media_client, bindings)
        proxy.start()
    except BaseException:
        media_client.close()
        raise
    return proxy


async def _async_start_rtsp_proxy(
    credentials: MediaCredentials, bindings: tuple[CameraBinding, ...]
) -> RtspProxyRuntime:
    """Finish or clean up an executor start even if HA cancels setup."""

    start_task = asyncio.create_task(
        asyncio.to_thread(_start_rtsp_proxy, credentials, bindings)
    )
    first_cancellation: asyncio.CancelledError | None = None
    start_error: BaseException | None = None
    while not start_task.done():
        try:
            await asyncio.shield(start_task)
        except asyncio.CancelledError as error:
            if first_cancellation is None:
                first_cancellation = error
        except BaseException as error:  # noqa: BLE001
            start_error = error
            break

    proxy: RtspProxyRuntime | None = None
    if start_error is None:
        try:
            proxy = start_task.result()
        except BaseException as error:  # noqa: BLE001
            start_error = error

    if first_cancellation is not None:
        if proxy is not None:
            cleanup_task = asyncio.create_task(asyncio.to_thread(proxy.close))
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
                except BaseException:  # noqa: BLE001
                    break
        first_cancellation.__cause__ = None
        first_cancellation.__context__ = None
        raise first_cancellation from None
    if start_error is not None:
        start_error.__cause__ = None
        start_error.__context__ = None
        raise start_error from None
    if proxy is None:
        raise GatewayError("RTSP proxy start failed")
    return proxy


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Expose settings independently of provider login, only if HA has a UI."""
    from homeassistant.const import EVENT_COMPONENT_LOADED

    async def register() -> None:
        from .settings_panel import async_register_settings_panel

        await async_register_settings_panel(hass)

    if "frontend" in hass.config.components:
        await register()
    else:

        async def frontend_loaded(event: Any) -> None:
            if event.data.get("component") == "frontend":
                unsubscribe()
                await register()

        unsubscribe = hass.bus.async_listen(EVENT_COMPONENT_LOADED, frontend_loaded)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> bool:
    """Set up a validated Ufanet account without performing physical actions."""

    if entry.data.get(CONF_REQUIRES_ACK) is not False:
        raise ConfigEntryNotReady(_ACKNOWLEDGEMENT_REQUIRED)

    voice_config = _enabled_voice_config(getattr(entry, "options", {}))
    read_session: ClientSession | None = None
    open_session: ClientSession | None = None
    voice_session: ClientSession | None = None
    voice_manager: Any | None = None
    history_poller: CallHistoryPoller | None = None
    rtsp_proxy: RtspProxyRuntime | None = None
    proxy_unsubscribe: Any | None = None
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
        rtsp_proxy = await _async_start_rtsp_proxy(
            MediaCredentials(entry.data[CONF_CONTRACT], entry.data[CONF_PASSWORD]),
            _camera_bindings(coordinator.data or {}),
        )

        active_proxy = rtsp_proxy
        if voice_config is not None:
            voice_manager, voice_session = await _async_build_voice_manager(
                hass, voice_config, coordinator, active_proxy
            )
        active_voice_manager = voice_manager

        def update_camera_bindings() -> None:
            try:
                active_proxy.replace_bindings(_camera_bindings(coordinator.data or {}))
            finally:
                if active_voice_manager is not None:
                    active_voice_manager.reconcile()

        proxy_unsubscribe = coordinator.async_add_listener(update_camera_bindings)
        history_manager = CallHistoryManager((coordinator.data or {}).values())
        history_poller = CallHistoryPoller(
            client,
            history_manager,
            lambda: (coordinator.data or {}).values(),
        )
        entry.runtime_data = UfanetRuntimeData(
            client=client,
            coordinator=coordinator,
            read_session=read_session,
            open_session=open_session,
            history_manager=history_manager,
            history_poller=history_poller,
            rtsp_proxy=rtsp_proxy,
            voice_manager=voice_manager,
            voice_session=voice_session,
            proxy_unsubscribe=proxy_unsubscribe,
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if voice_manager is not None:
            await voice_manager.async_start()
        await history_poller.async_start()
        add_update_listener = getattr(entry, "add_update_listener", None)
        async_on_unload = getattr(entry, "async_on_unload", None)
        if callable(add_update_listener) and callable(async_on_unload):
            async_on_unload(add_update_listener(_async_reload_entry))
    except BaseException as setup_error:

        async def finish_setup_cleanup(operation: Awaitable[object]) -> None:
            try:
                await operation
            except BaseException:  # noqa: BLE001, S110 - preserve original outcome
                pass

        if voice_manager is not None:
            await finish_setup_cleanup(voice_manager.async_stop())
        if history_poller is not None:
            await finish_setup_cleanup(history_poller.async_stop())
        if callable(proxy_unsubscribe):
            try:
                proxy_unsubscribe()
            except BaseException:  # noqa: BLE001, S110 - continue terminal cleanup
                pass
        if rtsp_proxy is not None:
            await finish_setup_cleanup(asyncio.to_thread(rtsp_proxy.close))
        for session in (voice_session, read_session, open_session):
            if session is not None:
                await finish_setup_cleanup(session.close())
        entry.runtime_data = None
        setup_error.__cause__ = None
        setup_error.__context__ = None
        raise setup_error from None
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
        if getattr(entry, "minor_version", 1) < 2:
            # v2 exposed a momentary opening command as a Lock. Home Assistant
            # cannot change an entity's domain in-place: its registry migration
            # helper changes only the platform and keeps the old ``lock.*``
            # domain. Remove the stale legacy row instead; the next Button
            # platform load recreates ``button.*`` with the same opaque unique
            # ID and device relationship. The domain change necessarily means
            # old lock.* entity IDs cannot be preserved; the registry keeps the
            # old row in deleted_entities as a migration tombstone.
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(hass)
            for registered in er.async_entries_for_config_entry(
                registry, entry.entry_id
            ):
                if (
                    registered.domain != "lock"
                    or registered.platform != "ufanet_intercom"
                ):
                    continue
                registry.async_remove(registered.entity_id)
            hass.config_entries.async_update_entry(entry, minor_version=2)
        return True
    if version != 1:
        return False

    data = dict(entry.data)
    data[CONF_IDENTITY_KEY] = base64.urlsafe_b64encode(
        secrets.token_bytes(IDENTITY_KEY_BYTES)
    ).decode("ascii")
    data[CONF_TRUSTED_BINDINGS] = {}
    data[CONF_REQUIRES_ACK] = True
    hass.config_entries.async_update_entry(entry, data=data, version=2, minor_version=2)
    return True
