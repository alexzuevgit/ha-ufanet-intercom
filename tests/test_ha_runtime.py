"""Synthetic tests for the dynamic Home Assistant runtime slice."""

# Imports must follow the dynamic sys.modules/sys.path Home Assistant stub bootstrap.
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import base64
import contextvars
import importlib
import sys
import threading
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

# Pure API tests install a package placeholder in conftest. Replace it so this
# module exercises the real HA package initializer against the lightweight stubs.
for module_name in tuple(sys.modules):
    if module_name == "custom_components.ufanet_intercom" or module_name.startswith(
        "custom_components.ufanet_intercom."
    ):
        del sys.modules[module_name]
sys.path.insert(0, str(Path(__file__).parent))
import ha_stub_import  # noqa: F401

runtime_module = importlib.import_module("custom_components.ufanet_intercom")
coordinator_module = importlib.import_module(
    "custom_components.ufanet_intercom.coordinator"
)
diagnostics_module = importlib.import_module(
    "custom_components.ufanet_intercom.diagnostics"
)
button_module = importlib.import_module("custom_components.ufanet_intercom.button")
camera_module = importlib.import_module("custom_components.ufanet_intercom.camera")
binary_sensor_module = importlib.import_module(
    "custom_components.ufanet_intercom.binary_sensor"
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.ufanet_intercom.api import (
    UfanetAuthenticationError,
    UfanetConcurrentOpenError,
    UfanetConnectionError,
    UfanetOpenError,
    UfanetOpenUnknownOutcome,
)
from custom_components.ufanet_intercom.const import (
    CONF_CONTRACT,
    CONF_IDENTITY_KEY,
    CONF_PASSWORD,
    CONF_REQUIRES_ACK,
    CONF_TRUSTED_BINDINGS,
    DOMAIN,
    DiscoveredDoor,
)
from custom_components.ufanet_intercom.voice_phrase import encode_phrase_set
from custom_components.ufanet_intercom.voice_runtime import VOICE_PHRASE_OPTIONS_ROOT

IDENTITY_KEY = bytes(range(32))
ENCODED_IDENTITY_KEY = base64.urlsafe_b64encode(IDENTITY_KEY).decode("ascii")


def door(
    digit: str,
    *,
    trusted: bool = True,
    openable: bool = True,
    display_name: str = "Synthetic lobby",
    shared_id: int = 1001,
    binding_digit: str | None = None,
    cctv_number: str = "",
) -> DiscoveredDoor:
    return DiscoveredDoor(
        key=digit * 64,
        shared_id=shared_id,
        door=0,
        model=21,
        display_name=display_name,
        binding=(binding_digit or digit) * 64,
        openable=openable,
        trusted=trusted,
        cctv_number=cctv_number,
    )


class FakeConfigEntries:
    def __init__(self, *, unload_result: bool = True) -> None:
        self.forwarded: list[tuple[Any, tuple[str, ...]]] = []
        self.unload_result = unload_result
        self.events: list[str] = []
        self.updated: dict[str, Any] | None = None
        self.update_calls: list[tuple[Any, dict[str, Any]]] = []
        self.reload_calls: list[str] = []

    async def async_forward_entry_setups(
        self, entry: Any, platforms: tuple[str, ...]
    ) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(
        self, _entry: Any, _platforms: tuple[str, ...]
    ) -> bool:
        self.events.append("unload")
        return self.unload_result

    def async_update_entry(self, entry: Any, **updates: Any) -> None:
        self.updated = updates
        self.update_calls.append((entry, updates))
        for attribute, value in updates.items():
            setattr(entry, attribute, value)

    async def async_reload(self, entry_id: str) -> bool:
        self.reload_calls.append(entry_id)
        return True


def config_data(**updates: Any) -> dict[str, Any]:
    data = {
        CONF_CONTRACT: "SYNTHETIC",
        CONF_PASSWORD: "synthetic-password",
        CONF_IDENTITY_KEY: ENCODED_IDENTITY_KEY,
        CONF_TRUSTED_BINDINGS: {"a" * 64: "a" * 64},
        CONF_REQUIRES_ACK: False,
    }
    data.update(updates)
    return data


def acknowledged_entry(**updates: Any) -> ConfigEntry:
    """Return an entry whose current runtime acknowledgement is exact False."""

    return ConfigEntry(data=config_data(**updates), version=2, minor_version=2)


def enabled_voice_options(target: DiscoveredDoor) -> dict[str, object]:
    """Return canonical editable options for one synthetic target."""

    return {
        VOICE_PHRASE_OPTIONS_ROOT: {
            "version": 1,
            "enabled": True,
            "endpoint": "https://stt.invalid/v1/audio/transcriptions",
            "token": "SYNTHETIC-STT-TOKEN",
            "model": "synthetic-model",
            "allow_insecure_http": False,
            "targets": {
                target.key: {
                    "binding": target.binding,
                    "phrases": encode_phrase_set(
                        ["синтетическая фраза"], salt=bytes(range(16))
                    ),
                    "entered_phrases": ["синтетическая фраза"],
                }
            },
        }
    }


def coordinator_snapshot(
    targets: dict[str, DiscoveredDoor],
    *,
    entry: ConfigEntry | None = None,
) -> DataUpdateCoordinator:
    """Build a listener-capable coordinator with safe config-entry context."""

    coordinator = DataUpdateCoordinator(
        object(), object(), config_entry=entry or acknowledged_entry()
    )
    coordinator.data = MappingProxyType(targets)
    coordinator.last_update_success = True
    return coordinator


_MISSING_ACK = object()


def set_current_ack(entry: ConfigEntry, value: object) -> None:
    """Replace the current immutable-style entry data acknowledgement value."""

    data = dict(entry.data)
    if value is _MISSING_ACK:
        data.pop(CONF_REQUIRES_ACK, None)
    else:
        data[CONF_REQUIRES_ACK] = value
    entry.data = data


def test_runtime_transport_guard_requires_exact_connector_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpectedConnector:
        pass

    class ExpectedSession:
        def __init__(self, connector: object) -> None:
            self.connector = connector
            self._retry_connection = False
            self._middlewares: tuple[Any, ...] = ()
            self.headers: dict[str, str] = {}
            self.timeout = SimpleNamespace(total=15)

    monkeypatch.setattr(runtime_module, "ClientSession", ExpectedSession)
    monkeypatch.setattr(runtime_module, "TCPConnector", ExpectedConnector)
    read_session = ExpectedSession(ExpectedConnector())
    open_session = ExpectedSession(ExpectedConnector())

    assert runtime_module._sessions_are_safe(read_session, open_session)
    open_session.connector = object()
    assert not runtime_module._sessions_are_safe(read_session, open_session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acknowledgement",
    [_MISSING_ACK, True, 0, 1, "false", "private-ack-token", None],
    ids=["missing", "true", "zero", "one", "false-string", "secret-string", "none"],
)
async def test_setup_rejects_non_exact_ack_before_any_runtime_work(
    monkeypatch: pytest.MonkeyPatch,
    acknowledgement: object,
) -> None:
    effects: list[str] = []

    class Session:
        async def close(self) -> None:
            effects.append("session-close")

    def new_session() -> Session:
        effects.append("new-session")
        return Session()

    class Client:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            effects.append("client")

    class Coordinator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            effects.append("coordinator")

        async def async_config_entry_first_refresh(self) -> None:
            effects.append("discovery")

    class ConfigEntries(FakeConfigEntries):
        async def async_forward_entry_setups(
            self, entry: Any, platforms: tuple[str, ...]
        ) -> None:
            effects.append("forward")
            await super().async_forward_entry_setups(entry, platforms)

    monkeypatch.setattr(runtime_module, "_new_session", new_session)
    monkeypatch.setattr(runtime_module, "_sessions_are_safe", lambda *_args: True)
    monkeypatch.setattr(runtime_module, "UfanetClient", Client)
    monkeypatch.setattr(runtime_module, "UfanetCoordinator", Coordinator)
    entry = acknowledged_entry()
    set_current_ack(entry, acknowledgement)
    hass = SimpleNamespace(config_entries=ConfigEntries())

    with pytest.raises(Exception) as raised:
        await runtime_module.async_setup_entry(hass, entry)

    assert effects == []
    assert entry.runtime_data is None
    assert hass.config_entries.forwarded == []
    assert "private-ack-token" not in str(raised.value)


def test_retry_disabling_requires_an_existing_boolean_attribute() -> None:
    missing = SimpleNamespace()
    assert runtime_module._disable_implicit_retry(missing) is False
    assert "_retry_connection" not in vars(missing)

    for malformed in (None, 0, "false", object()):
        session = SimpleNamespace(_retry_connection=malformed)
        assert runtime_module._disable_implicit_retry(session) is False
        assert session._retry_connection is malformed

    inspectable = SimpleNamespace(_retry_connection=True)
    assert runtime_module._disable_implicit_retry(inspectable) is True
    assert inspectable._retry_connection is False


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_case", ["missing", "malformed"])
async def test_setup_fails_closed_when_retry_disabling_cannot_be_proven(
    monkeypatch: pytest.MonkeyPatch,
    retry_case: str,
) -> None:
    sessions: list[Any] = []

    class UnsafeSession:
        def __init__(
            self, *, connector: object, timeout: object, middlewares: object
        ) -> None:
            self.connector = connector
            self.timeout = timeout
            self._middlewares = middlewares
            self.headers: dict[str, str] = {}
            self.closed = False
            if retry_case == "malformed":
                self._retry_connection = "not-a-boolean"
            sessions.append(self)

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runtime_module, "ClientSession", UnsafeSession)
    monkeypatch.setattr(runtime_module, "TCPConnector", object)
    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    entry = SimpleNamespace(data=config_data(), runtime_data=None)

    with pytest.raises(
        ConfigEntryNotReady,
        match="^Safe isolated HTTP transports are unavailable$",
    ):
        await runtime_module.async_setup_entry(hass, entry)

    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    if retry_case == "missing":
        assert all("_retry_connection" not in vars(session) for session in sessions)
    else:
        assert all(session._retry_connection == "not-a-boolean" for session in sessions)
    assert hass.config_entries.forwarded == []


@pytest.mark.asyncio
async def test_setup_owns_two_safe_sessions_and_performs_no_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered = MappingProxyType({"a" * 64: door("a", cctv_number="SYNTHETIC-CAMERA")})
    clients: list[Any] = []
    event_loop_thread = threading.get_ident()
    proxy_thread_ids: list[int] = []
    real_start_proxy = runtime_module._start_rtsp_proxy

    def recording_start_proxy(*args: Any) -> Any:
        proxy_thread_ids.append(threading.get_ident())
        return real_start_proxy(*args)

    class FakeClient:
        def __init__(self, read_session: Any, *_args: Any, **kwargs: Any) -> None:
            self.read_session = read_session
            self.open_session = kwargs["open_session"]
            self.identity_key = kwargs["identity_key"]
            self.trusted_bindings = kwargs["trusted_bindings"]
            self.open_calls = 0
            clients.append(self)

        async def async_update_inventory(self) -> Any:
            return discovered

    monkeypatch.setattr(runtime_module, "UfanetClient", FakeClient)
    monkeypatch.setattr(runtime_module, "_start_rtsp_proxy", recording_start_proxy)
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = SimpleNamespace(data=config_data(), runtime_data=None)

    assert await runtime_module.async_setup_entry(hass, entry) is True
    client = clients[0]
    read_session, open_session = client.read_session, client.open_session
    assert type(read_session) is type(open_session) is runtime_module.ClientSession
    assert read_session.connector is not None
    assert open_session.connector is not None
    assert read_session.connector is not open_session.connector
    for session in (read_session, open_session):
        assert session._retry_connection is False
        assert session._middlewares == ()
        assert "Authorization" not in session.headers
        assert 0 < session.timeout.total <= 30
    assert client.identity_key == IDENTITY_KEY
    assert client.trusted_bindings == config_data()[CONF_TRUSTED_BINDINGS]
    assert client.open_calls == 0
    assert entry.runtime_data.coordinator.data == discovered
    assert entry.runtime_data.rtsp_proxy.camera_count == 1
    assert entry.runtime_data.rtsp_proxy.stream_url("a" * 64) is not None
    assert proxy_thread_ids and proxy_thread_ids[0] != event_loop_thread
    assert config_entries.forwarded == [(entry, ("button", "binary_sensor", "camera"))]
    assert entry.runtime_data.voice_manager is None
    assert entry.runtime_data.voice_session is None

    await asyncio.to_thread(entry.runtime_data.rtsp_proxy.close)
    await read_session.close()
    await open_session.close()


@pytest.mark.asyncio
async def test_setup_failure_closes_both_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[Any] = []

    class RejectingClient:
        def __init__(self, read_session: Any, *_args: Any, **kwargs: Any) -> None:
            sessions.extend((read_session, kwargs["open_session"]))

        async def async_update_inventory(self) -> Any:
            raise UfanetConnectionError("private detail")

    monkeypatch.setattr(runtime_module, "UfanetClient", RejectingClient)
    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    entry = SimpleNamespace(data=config_data(), runtime_data=None)

    with pytest.raises(UpdateFailed, match="^Ufanet door availability update failed$"):
        await runtime_module.async_setup_entry(hass, entry)
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert hass.config_entries.forwarded == []


@pytest.mark.asyncio
async def test_cancelled_media_proxy_start_finishes_and_closes_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class Proxy:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()

    proxy = Proxy()

    def blocking_start(*_args: Any) -> Any:
        started.set()
        release.wait(timeout=2)
        return proxy

    monkeypatch.setattr(runtime_module, "_start_rtsp_proxy", blocking_start)
    task = asyncio.create_task(
        runtime_module._async_start_rtsp_proxy(
            runtime_module.MediaCredentials("CONTRACT-42", "synthetic-password"),
            (),
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel("synthetic setup cancellation")
    release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("synthetic setup cancellation",)
    assert proxy.closed.is_set()


@pytest.mark.asyncio
async def test_coordinator_replaces_omitted_data_and_maps_fixed_errors() -> None:
    first = {"a" * 64: door("a"), "b" * 64: door("b", shared_id=1002)}
    second = {"b" * 64: first["b" * 64]}

    class Client:
        def __init__(self) -> None:
            self.results: list[Any] = [first, second]

        async def async_update_inventory(self) -> Any:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

    client = Client()
    coordinator = coordinator_module.UfanetCoordinator(object(), object(), client)
    # Availability and trust are intentionally excluded from DiscoveredDoor
    # identity equality. The coordinator must therefore always notify listeners
    # after a successful fresh inventory so blocked/quarantined targets become
    # unavailable immediately in Home Assistant.
    assert coordinator.always_update is True
    initial = await coordinator._async_update_data()
    assert set(initial) == {"a" * 64, "b" * 64}
    with pytest.raises(TypeError):
        initial["c" * 64] = door("c")
    current = await coordinator._async_update_data()
    assert set(current) == {"b" * 64}

    client.results = [UfanetAuthenticationError("provider secret")]
    with pytest.raises(ConfigEntryAuthFailed) as auth:
        await coordinator._async_update_data()
    assert str(auth.value) == "Ufanet authentication failed"
    client.results = [UfanetConnectionError("provider secret")]
    with pytest.raises(UpdateFailed) as update:
        await coordinator._async_update_data()
    assert str(update.value) == "Ufanet door availability update failed"


@pytest.mark.asyncio
async def test_dynamic_buttons_use_only_opaque_identity_and_strict_availability() -> (
    None
):
    trusted = door("a", display_name="Provider display")
    untrusted = door("b", trusted=False, shared_id=1002)
    entry = acknowledged_entry()
    coordinator = coordinator_snapshot(
        {trusted.key: trusted, untrusted.key: untrusted}, entry=entry
    )

    class Client:
        async def async_open(self, _key: str) -> None:
            return None

    entry.runtime_data = SimpleNamespace(coordinator=coordinator, client=Client())
    entities: list[Any] = []
    await button_module.async_setup_entry(
        None, entry, lambda values: entities.extend(values)
    )

    assert len(entities) == 1
    entity = entities[0]
    assert entity._attr_unique_id == trusted.key
    assert entity._attr_name == "Provider display"
    assert entity.entity_id == f"button.{trusted.suggested_object_id}"
    assert "provider" not in entity.entity_id
    assert entity._attr_device_info == {
        "identifiers": {(DOMAIN, trusted.key)},
        "name": "Provider display",
    }
    assert entity._attr_translation_key == "open_door"
    assert entity.available is True

    coordinator.data = MappingProxyType({})
    assert entity.available is False
    coordinator.data = MappingProxyType(
        {trusted.key: door("a", trusted=False, display_name="changed")}
    )
    assert entity.available is False
    coordinator.data = MappingProxyType(
        {trusted.key: door("a", openable=False, display_name="changed")}
    )
    assert entity.available is False
    coordinator.data = MappingProxyType(
        {trusted.key: door("a", binding_digit="c", display_name="changed")}
    )
    assert entity.available is False
    coordinator.last_update_success = False
    coordinator.data = MappingProxyType({trusted.key: trusted})
    assert entity.available is False


@pytest.mark.asyncio
async def test_button_setup_listens_for_new_trusted_targets_and_uses_opaque_names() -> (
    None
):
    duplicate_a = door("a", display_name="Shared name")
    duplicate_b = door("b", display_name="Shared name", shared_id=1002)
    unique = door("c", display_name="Unique name", shared_id=1003)
    fallback = door("d", display_name="Ufanet intercom", shared_id=1004)
    ignored = door(
        "e", display_name="Private ignored name", trusted=False, shared_id=1005
    )
    later = door("f", display_name="Later unique name", shared_id=1006)
    later_untrusted = door(
        "9", display_name="Private later name", trusted=False, shared_id=1007
    )
    initial = {
        target.key: target
        for target in (duplicate_a, duplicate_b, unique, fallback, ignored)
    }
    entry = acknowledged_entry()
    coordinator = coordinator_snapshot(initial, entry=entry)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, client=object())
    add_batches: list[list[Any]] = []

    await button_module.async_setup_entry(
        None, entry, lambda values: add_batches.append(list(values))
    )

    initial_entities = add_batches[0]
    assert {entity._attr_unique_id for entity in initial_entities} == {
        duplicate_a.key,
        duplicate_b.key,
        unique.key,
        fallback.key,
    }
    entities_by_key = {entity._attr_unique_id: entity for entity in initial_entities}
    assert entities_by_key[duplicate_a.key]._attr_name == "Shared name (aaaaaaaa)"
    assert entities_by_key[duplicate_b.key]._attr_name == "Shared name (bbbbbbbb)"
    assert entities_by_key[unique.key]._attr_name == "Unique name"
    assert entities_by_key[fallback.key]._attr_name == "Ufanet intercom (dddddddd)"
    for target in (duplicate_a, duplicate_b, unique, fallback):
        entity = entities_by_key[target.key]
        assert entity.entity_id == f"button.{target.suggested_object_id}"
        assert entity._attr_device_info["name"] == entity._attr_name

    assert len(coordinator.listeners) == 1
    assert len(entry.unload_callbacks) == 1

    with_later = {**initial, later.key: later}
    coordinator.async_set_updated_data(MappingProxyType(with_later))
    coordinator.async_set_updated_data(MappingProxyType(dict(with_later)))
    coordinator.async_set_updated_data(
        MappingProxyType({**with_later, later_untrusted.key: later_untrusted})
    )

    all_entities = [entity for batch in add_batches for entity in batch]
    assert [entity._attr_unique_id for entity in all_entities].count(later.key) == 1
    assert later_untrusted.key not in {
        entity._attr_unique_id for entity in all_entities
    }
    later_entity = next(
        entity for entity in all_entities if entity._attr_unique_id == later.key
    )
    assert later_entity._attr_name == "Later unique name"
    assert later_entity.entity_id == f"button.{later.suggested_object_id}"
    assert later_entity._attr_device_info["name"] == later_entity._attr_name

    entry.unload_callbacks[0]()
    assert coordinator.listeners == []


@pytest.mark.parametrize(
    "acknowledgement",
    [_MISSING_ACK, True, 0, 1, "false", "private-ack-token", None],
    ids=["missing", "true", "zero", "one", "false-string", "secret-string", "none"],
)
def test_entity_is_unavailable_when_current_ack_is_not_exact_false(
    acknowledgement: object,
) -> None:
    target = door("a")
    entry = acknowledged_entry()
    coordinator = coordinator_snapshot({target.key: target}, entry=entry)
    client = SimpleNamespace()
    entity = button_module.UfanetDoorOpenButton(coordinator, client, target)
    assert entity.available is True

    set_current_ack(entry, acknowledgement)

    assert entity.available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acknowledgement",
    [_MISSING_ACK, True, 0, 1, "false", "private-ack-token", None],
    ids=["missing", "true", "zero", "one", "false-string", "secret-string", "none"],
)
async def test_open_rejects_current_non_exact_ack_without_calling_client(
    acknowledgement: object,
) -> None:
    target = door("a")
    entry = acknowledged_entry()
    coordinator = coordinator_snapshot({target.key: target}, entry=entry)

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def async_open(self, key: str) -> None:
            self.calls.append(key)

    client = Client()
    entity = button_module.UfanetDoorOpenButton(coordinator, client, target)
    set_current_ack(entry, acknowledgement)

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_press()
    assert client.calls == []
    assert "private-ack-token" not in str(raised.value)


@pytest.mark.asyncio
async def test_open_passes_only_fixed_key_and_maps_unknown_but_preserves_cancel() -> (
    None
):
    target = door("a")
    coordinator = coordinator_snapshot({target.key: target})

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            self.error: BaseException | None = None
            self.last_physical_outcome: str | None = None

        async def async_open(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append((args, kwargs))
            self.last_physical_outcome = None
            if self.error is not None:
                if isinstance(self.error, UfanetOpenUnknownOutcome):
                    self.last_physical_outcome = "unknown"
                raise self.error
            self.last_physical_outcome = "confirmed"

    client = Client()
    entity = button_module.UfanetDoorOpenButton(coordinator, client, target)
    await entity.async_press()
    assert client.calls == [((target.key,), {})]
    assert entity.extra_state_attributes == {
        "last_command_outcome": "confirmed",
        "do_not_retry": False,
    }

    client.error = UfanetOpenUnknownOutcome("provider detail")
    with pytest.raises(HomeAssistantError) as unknown:
        await entity.async_press()
    assert str(unknown.value) == (
        "Door opening outcome is unknown; the command was not repeated"
    )
    assert entity.extra_state_attributes == {
        "last_command_outcome": "unknown",
        "do_not_retry": True,
    }

    cancellation = asyncio.CancelledError("preserved")
    client.error = cancellation
    writes_before_cancel = entity.state_writes
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await entity.async_press()
    assert cancelled.value is cancellation
    assert entity.extra_state_attributes == {
        "last_command_outcome": "unknown",
        "do_not_retry": True,
    }
    assert entity.state_writes == writes_before_cancel


@pytest.mark.asyncio
async def test_post_transmission_cancel_sets_unknown_once_and_preserves_identity() -> (
    None
):
    target = door("a")
    coordinator = coordinator_snapshot({target.key: target})
    cancellation = asyncio.CancelledError("preserved-private-detail")

    class Client:
        def __init__(self) -> None:
            self._last_physical_outcome: str | None = None

        @property
        def last_physical_outcome(self) -> str | None:
            return self._last_physical_outcome

        async def async_open(self, _key: str) -> None:
            self._last_physical_outcome = "unknown"
            raise cancellation

    entity = button_module.UfanetDoorOpenButton(coordinator, Client(), target)

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await entity.async_press()

    assert cancelled.value is cancellation
    assert entity.extra_state_attributes == {
        "last_command_outcome": "unknown",
        "do_not_retry": True,
    }
    assert entity.state_writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "physical_outcome", "message", "attributes"),
    [
        (
            UfanetOpenUnknownOutcome("private unknown detail"),
            "unknown",
            "Door opening outcome is unknown; the command was not repeated",
            {"last_command_outcome": "unknown", "do_not_retry": True},
        ),
        (
            UfanetConcurrentOpenError("private concurrent detail"),
            None,
            "Another door command is already in progress",
            {"last_command_outcome": "none", "do_not_retry": False},
        ),
        (
            UfanetOpenError("private not-confirmed detail"),
            "not_confirmed",
            "Door opening was not confirmed",
            {"last_command_outcome": "not_confirmed", "do_not_retry": False},
        ),
    ],
)
async def test_open_maps_fixed_ha_errors_without_provider_exception_context(
    provider_error: BaseException,
    physical_outcome: str | None,
    message: str,
    attributes: dict[str, str | bool],
) -> None:
    target = door("a")
    coordinator = coordinator_snapshot({target.key: target})

    class Client:
        last_physical_outcome: str | None = None

        async def async_open(self, _key: str) -> None:
            self.last_physical_outcome = physical_outcome
            raise provider_error

    entity = button_module.UfanetDoorOpenButton(coordinator, Client(), target)

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_press()

    assert str(raised.value) == message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)
    assert entity.extra_state_attributes == attributes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("later_error", "later_outcome", "message"),
    [
        (
            UfanetConcurrentOpenError("private concurrent detail"),
            None,
            "Another door command is already in progress",
        ),
        (
            UfanetOpenError("private cooldown detail"),
            "unknown",
            "Door opening was not confirmed",
        ),
        (
            UfanetOpenError("private preflight detail"),
            None,
            "Door opening was not confirmed",
        ),
    ],
)
async def test_pretransmission_rejection_preserves_prior_unknown_warning(
    later_error: BaseException,
    later_outcome: str | None,
    message: str,
) -> None:
    target = door("a")
    coordinator = coordinator_snapshot({target.key: target})

    class Client:
        def __init__(self) -> None:
            self.last_physical_outcome: str | None = None
            self.attempt = 0

        async def async_open(self, _key: str) -> None:
            self.attempt += 1
            if self.attempt == 1:
                self.last_physical_outcome = "unknown"
                raise UfanetOpenUnknownOutcome("private initial detail")
            self.last_physical_outcome = later_outcome
            raise later_error

    entity = button_module.UfanetDoorOpenButton(coordinator, Client(), target)
    with pytest.raises(HomeAssistantError):
        await entity.async_press()
    writes_after_unknown = entity.state_writes

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_press()

    assert str(raised.value) == message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert entity.extra_state_attributes == {
        "last_command_outcome": "unknown",
        "do_not_retry": True,
    }
    assert entity.state_writes == writes_after_unknown


@pytest.mark.asyncio
async def test_transmitted_not_confirmed_replaces_prior_unknown_warning() -> None:
    target = door("a")
    coordinator = coordinator_snapshot({target.key: target})

    class Client:
        def __init__(self) -> None:
            self.last_physical_outcome: str | None = None
            self.attempt = 0

        async def async_open(self, _key: str) -> None:
            self.attempt += 1
            if self.attempt == 1:
                self.last_physical_outcome = "unknown"
                raise UfanetOpenUnknownOutcome("private initial detail")
            self.last_physical_outcome = "not_confirmed"
            raise UfanetOpenError("private current detail")

    entity = button_module.UfanetDoorOpenButton(coordinator, Client(), target)
    with pytest.raises(HomeAssistantError):
        await entity.async_press()

    with pytest.raises(
        HomeAssistantError, match="^Door opening was not confirmed$"
    ) as raised:
        await entity.async_press()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert entity.extra_state_attributes == {
        "last_command_outcome": "not_confirmed",
        "do_not_retry": False,
    }
    assert entity.state_writes == 2


@pytest.mark.asyncio
async def test_concurrent_entities_keep_task_local_physical_outcomes() -> None:
    target_a = door("a")
    target_b = door("b", shared_id=1002)
    coordinator = coordinator_snapshot({target_a.key: target_a, target_b.key: target_b})
    b_transmitted = asyncio.Event()
    release_b = asyncio.Event()

    class Client:
        def __init__(self) -> None:
            self._outcome: contextvars.ContextVar[str | None] = contextvars.ContextVar(
                "synthetic_physical_outcome", default=None
            )
            self.a_attempts = 0

        @property
        def last_physical_outcome(self) -> str | None:
            return self._outcome.get()

        async def async_open(self, key: str) -> None:
            self._outcome.set(None)
            if key == target_a.key:
                self.a_attempts += 1
                if self.a_attempts == 1:
                    self._outcome.set("unknown")
                    raise UfanetOpenUnknownOutcome("private initial detail")
                raise UfanetOpenError("private pre-send detail")

            self._outcome.set("unknown")
            self._outcome.set("not_confirmed")
            b_transmitted.set()
            await release_b.wait()
            raise UfanetOpenError("private not-confirmed detail")

    client = Client()
    entity_a = button_module.UfanetDoorOpenButton(coordinator, client, target_a)
    entity_b = button_module.UfanetDoorOpenButton(coordinator, client, target_b)

    with pytest.raises(HomeAssistantError):
        await entity_a.async_press()
    assert entity_a.extra_state_attributes == {
        "last_command_outcome": "unknown",
        "do_not_retry": True,
    }
    writes_after_unknown = entity_a.state_writes

    opening_b = asyncio.create_task(entity_b.async_press())
    await asyncio.wait_for(b_transmitted.wait(), timeout=1)
    with pytest.raises(HomeAssistantError, match="^Door opening was not confirmed$"):
        await entity_a.async_press()

    assert entity_a.extra_state_attributes == {
        "last_command_outcome": "unknown",
        "do_not_retry": True,
    }
    assert entity_a.state_writes == writes_after_unknown

    release_b.set()
    with pytest.raises(HomeAssistantError, match="^Door opening was not confirmed$"):
        await opening_b
    assert entity_b.extra_state_attributes == {
        "last_command_outcome": "not_confirmed",
        "do_not_retry": False,
    }


class LifecycleClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def begin_close(self) -> None:
        self.events.append("begin")

    def cancel_close(self) -> None:
        self.events.append("cancel")

    async def async_drain(self) -> None:
        self.events.append("drain")


class LifecycleSession:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.events.append(self.name)


class LifecycleProxy:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.events.append("proxy-close")


@pytest.mark.asyncio
@pytest.mark.parametrize("unload_result", [True, False])
async def test_unload_lifecycle_retains_sessions_only_on_failure(
    unload_result: bool,
) -> None:
    config_entries = FakeConfigEntries(unload_result=unload_result)
    events = config_entries.events
    client = LifecycleClient(events)
    read_session = LifecycleSession("read-close", events)
    open_session = LifecycleSession("open-close", events)
    proxy = LifecycleProxy(events)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            client=client,
            history_poller=SimpleNamespace(async_stop=lambda: asyncio.sleep(0)),
            read_session=read_session,
            open_session=open_session,
            rtsp_proxy=proxy,
            proxy_unsubscribe=lambda: events.append("proxy-unsubscribe"),
        )
    )
    hass = SimpleNamespace(config_entries=config_entries)

    assert await runtime_module.async_unload_entry(hass, entry) is unload_result
    if unload_result:
        assert events[:3] == ["begin", "unload", "drain"]
        assert events[3:5] == ["proxy-unsubscribe", "proxy-close"]
        assert set(events[5:]) == {"read-close", "open-close"}
        assert read_session.closed and open_session.closed
        assert proxy.closed
    else:
        assert events == ["begin", "unload", "cancel"]
        assert not read_session.closed and not open_session.closed
        assert not proxy.closed


@pytest.mark.asyncio
async def test_repeatedly_cancelled_unload_finishes_drain_and_propagates_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_entries = FakeConfigEntries()
    events = config_entries.events
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()
    first_cancel_seen = asyncio.Event()
    second_cancel_seen = asyncio.Event()
    cancellations: list[asyncio.CancelledError] = []
    real_shield = asyncio.shield

    async def recording_shield(awaitable: Any) -> Any:
        try:
            return await real_shield(awaitable)
        except asyncio.CancelledError as error:
            cancellations.append(error)
            (first_cancel_seen if len(cancellations) == 1 else second_cancel_seen).set()
            raise

    monkeypatch.setattr(runtime_module.asyncio, "shield", recording_shield)

    class BlockingClient(LifecycleClient):
        async def async_drain(self) -> None:
            self.events.append("drain-start")
            drain_started.set()
            await release_drain.wait()
            self.events.append("drain-complete")

    client = BlockingClient(events)
    read_session = LifecycleSession("read-close", events)
    open_session = LifecycleSession("open-close", events)
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            client=client,
            history_poller=SimpleNamespace(async_stop=lambda: asyncio.sleep(0)),
            read_session=read_session,
            open_session=open_session,
        )
    )
    hass = SimpleNamespace(config_entries=config_entries)

    unloading = asyncio.create_task(runtime_module.async_unload_entry(hass, entry))
    await asyncio.wait_for(drain_started.wait(), timeout=1)
    unloading.cancel("first synthetic unload cancellation")
    await asyncio.wait_for(first_cancel_seen.wait(), timeout=1)
    unloading.cancel("second synthetic unload cancellation")
    await asyncio.wait_for(second_cancel_seen.wait(), timeout=1)

    assert not unloading.done()
    assert not read_session.closed
    assert not open_session.closed
    assert events == ["begin", "unload", "drain-start"]

    release_drain.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await unloading

    assert len(cancellations) == 2
    assert cancellations[0].args == ("first synthetic unload cancellation",)
    assert cancellations[1].args == ("second synthetic unload cancellation",)
    assert raised.value is cancellations[0]
    assert raised.value.args == ("first synthetic unload cancellation",)
    assert events[:4] == ["begin", "unload", "drain-start", "drain-complete"]
    assert set(events[4:]) == {"read-close", "open-close"}
    assert read_session.closed and open_session.closed


@pytest.mark.asyncio
async def test_diagnostics_are_aggregate_only_and_private() -> None:
    doors = {
        "a" * 64: door("a"),
        "b" * 64: door("b", openable=False, shared_id=1002),
        "c" * 64: door("c", trusted=False, shared_id=1003),
    }
    entry = SimpleNamespace(
        title="private title",
        data=config_data(**{CONF_REQUIRES_ACK: True}),
        options=enabled_voice_options(doors["a" * 64]),
        runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(data=doors, last_update_success=True)
        ),
    )
    result = await diagnostics_module.async_get_config_entry_diagnostics(None, entry)

    assert result == {
        "last_update_success": True,
        "requires_ack": True,
        "discovered_count": 3,
        "trusted_count": 2,
        "openable_trusted_count": 1,
        "call_history_available": False,
        "call_history_poll_interval_seconds": 3,
        "voice_phrase_enabled": False,
        "voice_phrase_configured_count": 0,
        "voice_phrase_available_count": 0,
    }
    rendered = repr(result)
    for private in (
        entry.title,
        entry.data[CONF_CONTRACT],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_IDENTITY_KEY],
        "a" * 64,
        "Provider display",
        "синтетическая фраза",
        "entered_phrases",
    ):
        assert private not in rendered

    entry.runtime_data.voice_manager = SimpleNamespace(
        config=SimpleNamespace(enabled=True, target_count=2),
        available_count=1,
        token="synthetic-secret-that-must-not-appear",
        transcript="synthetic transcript that must not appear",
    )
    enabled_result = await diagnostics_module.async_get_config_entry_diagnostics(
        None, entry
    )
    assert enabled_result["voice_phrase_enabled"] is True
    assert enabled_result["voice_phrase_configured_count"] == 2
    assert enabled_result["voice_phrase_available_count"] == 1
    assert "synthetic-secret" not in repr(enabled_result)
    assert "synthetic transcript" not in repr(enabled_result)


def forbid_migration_runtime_work(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Fail immediately if a local migration reaches any runtime boundary."""

    effects: list[str] = []

    def forbidden(name: str) -> Any:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            effects.append(name)
            raise AssertionError(f"migration called {name}")

        return record

    monkeypatch.setattr(runtime_module, "_new_session", forbidden("session"))
    monkeypatch.setattr(runtime_module, "UfanetClient", forbidden("client"))
    monkeypatch.setattr(runtime_module, "UfanetCoordinator", forbidden("discovery"))
    return effects


@pytest.mark.asyncio
async def test_v1_migration_preserves_data_and_updates_to_safe_v2_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects = forbid_migration_runtime_work(monkeypatch)
    generated_key = bytes(range(32))
    generation_sizes: list[int] = []

    def generate_identity_key(size: int) -> bytes:
        generation_sizes.append(size)
        return generated_key

    monkeypatch.setattr(runtime_module.secrets, "token_bytes", generate_identity_key)
    original_data = {
        CONF_CONTRACT: "SYNTHETIC",
        CONF_PASSWORD: "password",
        "private_future_setting": {"preserve": [1, 2, 3]},
        CONF_IDENTITY_KEY: "legacy-value",
        CONF_TRUSTED_BINDINGS: {"legacy": "must-not-be-trusted"},
        CONF_REQUIRES_ACK: False,
    }
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = ConfigEntry(version=1, data=original_data.copy())

    assert await runtime_module.async_migrate_entry(hass, entry) is True
    assert len(config_entries.update_calls) == 1
    updated_entry, updates = config_entries.update_calls[0]
    assert updated_entry is entry
    assert updates["version"] == 2
    assert updates["minor_version"] == 2
    data = updates["data"]
    assert data[CONF_CONTRACT] == original_data[CONF_CONTRACT]
    assert data[CONF_PASSWORD] == original_data[CONF_PASSWORD]
    assert data["private_future_setting"] == original_data["private_future_setting"]
    assert data[CONF_IDENTITY_KEY] == ENCODED_IDENTITY_KEY
    assert runtime_module._decode_identity_key(data[CONF_IDENTITY_KEY]) == generated_key
    assert data[CONF_TRUSTED_BINDINGS] == {}
    assert data[CONF_REQUIRES_ACK] is True
    assert type(data[CONF_REQUIRES_ACK]) is bool
    assert set(data) == set(original_data)
    assert original_data[CONF_IDENTITY_KEY] == "legacy-value"
    assert original_data[CONF_TRUSTED_BINDINGS] == {"legacy": "must-not-be-trusted"}
    assert original_data[CONF_REQUIRES_ACK] is False
    assert generation_sizes == [32]
    assert effects == []

    assert await runtime_module.async_migrate_entry(hass, entry) is True
    assert len(config_entries.update_calls) == 1
    assert generation_sizes == [32]
    assert effects == []


@pytest.mark.asyncio
async def test_v2_lock_registry_rows_are_removed_before_button_recreation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The domain change leaves no stale lock row and keeps its opaque identity."""
    effects = forbid_migration_runtime_work(monkeypatch)
    registry_module = importlib.import_module("homeassistant.helpers.entity_registry")
    legacy = SimpleNamespace(
        domain="lock",
        platform=DOMAIN,
        entity_id="lock.synthetic_entry",
        unique_id="a" * 64,
        device_id="synthetic-device",
    )
    unrelated = SimpleNamespace(
        domain="lock",
        platform="other_integration",
        entity_id="lock.other",
        unique_id="b" * 64,
        device_id="other-device",
    )

    class Registry:
        def __init__(self) -> None:
            self.removed: list[str] = []

        def async_remove(self, entity_id: str) -> None:
            self.removed.append(entity_id)

    registry = Registry()
    monkeypatch.setattr(registry_module, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        registry_module,
        "async_entries_for_config_entry",
        lambda _registry, _entry_id: (legacy, unrelated),
    )
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = ConfigEntry(
        version=2,
        minor_version=1,
        data=config_data(),
    )

    assert await runtime_module.async_migrate_entry(hass, entry) is True
    assert registry.removed == ["lock.synthetic_entry"]
    assert config_entries.update_calls[0][1] == {"minor_version": 2}
    assert effects == []


@pytest.mark.asyncio
async def test_v2_migration_is_idempotent_and_does_not_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects = forbid_migration_runtime_work(monkeypatch)

    def forbidden_generation(_size: int) -> bytes:
        raise AssertionError("idempotent migration generated an identity key")

    monkeypatch.setattr(runtime_module.secrets, "token_bytes", forbidden_generation)
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    original_data = config_data(private_future_setting={"preserve": True})
    entry = ConfigEntry(version=2, minor_version=2, data=original_data.copy())

    assert await runtime_module.async_migrate_entry(hass, entry) is True
    assert config_entries.update_calls == []
    assert entry.data == original_data
    assert entry.minor_version == 2
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version",
    [True, False, "2", None, 0, -1, 3, 999],
    ids=["true", "false", "string", "none", "zero", "negative", "future", "far-future"],
)
async def test_migration_rejects_malformed_and_future_versions_without_effects(
    monkeypatch: pytest.MonkeyPatch,
    version: object,
) -> None:
    effects = forbid_migration_runtime_work(monkeypatch)
    generation_sizes: list[int] = []

    def forbidden_generation(size: int) -> bytes:
        generation_sizes.append(size)
        raise AssertionError("invalid migration generated an identity key")

    monkeypatch.setattr(runtime_module.secrets, "token_bytes", forbidden_generation)
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    original_data = config_data(private_future_setting={"preserve": True})
    entry = ConfigEntry(version=1, data=original_data.copy())
    entry.version = version

    assert await runtime_module.async_migrate_entry(hass, entry) is False
    assert config_entries.update_calls == []
    assert entry.data == original_data
    assert entry.version is version
    assert generation_sizes == []
    assert effects == []


class FakeRtspProxy:
    def __init__(self, *aliases: str, port: int = 18092) -> None:
        self.aliases = set(aliases)
        self.port = port

    def stream_url(self, alias: str) -> str | None:
        if alias not in self.aliases:
            return None
        return f"rtsp://127.0.0.1:{self.port}/{alias}"


@pytest.mark.asyncio
async def test_camera_uses_only_opaque_loopback_rtsp_and_tcp_stream() -> None:
    target = door("a", shared_id=987654, cctv_number="PRIVATE-CAMERA")
    coordinator = coordinator_snapshot({target.key: target})
    proxy = FakeRtspProxy(target.key)
    entity = camera_module.UfanetIntercomCamera(
        coordinator,
        proxy,
        target,
        device_name="Synthetic lobby",
    )

    source = await entity.stream_source()
    assert source == f"rtsp://127.0.0.1:18092/{target.key}"
    assert "PRIVATE-CAMERA" not in source
    assert "987654" not in source
    assert entity.stream_options == {"rtsp_transport": "tcp"}
    assert entity.available is True
    assert entity.use_stream_for_stills is True
    assert entity.entity_id == f"camera.{target.suggested_object_id}"

    proxy.aliases.clear()
    assert entity.available is False
    assert await entity.stream_source() is None


@pytest.mark.asyncio
async def test_camera_setup_adds_every_account_camera_and_new_discovery_once() -> None:
    first = door("a", shared_id=30001, cctv_number="CAMERA-A")
    second = door("b", shared_id=30002, cctv_number="CAMERA-B")
    no_camera = door("c", shared_id=30003)
    untrusted = door("d", shared_id=30004, trusted=False, cctv_number="CAMERA-D")
    later = door("e", shared_id=30005, cctv_number="CAMERA-E")
    entry = acknowledged_entry()
    initial = {door.key: door for door in (first, second, no_camera, untrusted)}
    coordinator = coordinator_snapshot(initial, entry=entry)
    proxy = FakeRtspProxy(first.key, second.key)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, rtsp_proxy=proxy)
    batches: list[list[Any]] = []

    await camera_module.async_setup_entry(
        None, entry, lambda entities: batches.append(list(entities))
    )
    assert {entity._target.key for entity in batches[0]} == {first.key, second.key}

    proxy.aliases.add(later.key)
    coordinator.async_set_updated_data(MappingProxyType({**initial, later.key: later}))
    coordinator.async_set_updated_data(MappingProxyType({**initial, later.key: later}))
    all_entities = [entity for batch in batches for entity in batch]
    assert [entity._target.key for entity in all_entities].count(later.key) == 1
    assert no_camera.key not in {entity._target.key for entity in all_entities}
    assert untrusted.key not in {entity._target.key for entity in all_entities}


@pytest.mark.asyncio
async def test_two_accounts_isolate_same_provider_camera_on_distinct_relays() -> None:
    account_a = door("a", shared_id=41001, cctv_number="SAME-CAMERA")
    account_b = door("b", shared_id=41001, cctv_number="SAME-CAMERA")
    entity_a = camera_module.UfanetIntercomCamera(
        coordinator_snapshot({account_a.key: account_a}),
        FakeRtspProxy(account_a.key, port=18091),
        account_a,
    )
    entity_b = camera_module.UfanetIntercomCamera(
        coordinator_snapshot({account_b.key: account_b}),
        FakeRtspProxy(account_b.key, port=18092),
        account_b,
    )

    source_a = await entity_a.stream_source()
    source_b = await entity_b.stream_source()
    assert source_a != source_b
    assert entity_a._attr_unique_id != entity_b._attr_unique_id
    assert source_a is not None and "SAME-CAMERA" not in source_a
    assert source_b is not None and "SAME-CAMERA" not in source_b


class FakeHistoryManager:
    def add_listener(self, _listener: Any) -> Any:
        return lambda: None

    def available_for(self, _key: str) -> bool:
        return True

    def is_on_for(self, _key: str) -> bool:
        return False


class FakeVoiceManager:
    def __init__(self, configured: set[tuple[str, str]]) -> None:
        self.configured = configured
        self.available: dict[str, bool] = {}
        self.on: dict[str, bool] = {}
        self.listeners: list[Any] = []

    def configured_for(self, key: object, binding: object) -> bool:
        return (key, binding) in self.configured

    def available_for(self, key: object) -> bool:
        return type(key) is str and self.available.get(key, False)

    def is_on_for(self, key: object) -> bool:
        return type(key) is str and self.on.get(key, False)

    def add_listener(self, listener: Any) -> Any:
        self.listeners.append(listener)

        def remove() -> None:
            self.listeners.remove(listener)

        return remove

    def notify(self) -> None:
        for listener in tuple(self.listeners):
            listener()


@pytest.mark.asyncio
async def test_code_phrase_sensor_uses_same_device_and_manager_only_state() -> None:
    configured = door("a", display_name="Configured entrance")
    stale = door("b", display_name="Stale entrance", binding_digit="c")
    untrusted = door("c", trusted=False, display_name="Ignored entrance")
    coordinator = coordinator_snapshot(
        {item.key: item for item in (configured, stale, untrusted)}
    )
    manager = FakeVoiceManager(
        {(configured.key, configured.binding), (stale.key, "2" * 64)}
    )
    entry = acknowledged_entry()
    entry.options = enabled_voice_options(configured)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator,
        history_manager=FakeHistoryManager(),
        voice_manager=manager,
    )
    batches: list[list[Any]] = []

    await binary_sensor_module.async_setup_entry(
        None, entry, lambda values: batches.append(list(values))
    )
    entities = [entity for batch in batches for entity in batch]
    phrase_entities = [
        entity
        for entity in entities
        if getattr(entity, "_attr_translation_key", None) == "code_phrase"
    ]
    assert len(phrase_entities) == 1
    entity = phrase_entities[0]
    assert entity._target is configured
    assert entity._attr_unique_id == f"{configured.key}_code_phrase"
    assert entity.entity_id == (
        f"binary_sensor.{configured.suggested_object_id}_code_phrase"
    )
    assert entity._attr_device_info == {
        "identifiers": {(DOMAIN, configured.key)},
        "name": "Configured entrance",
    }
    assert entity.available is False
    assert entity.is_on is False
    assert not hasattr(entity, "extra_state_attributes")

    await entity.async_added_to_hass()
    manager.available[configured.key] = True
    manager.on[configured.key] = True
    manager.notify()
    assert entity.state_writes == 1
    assert entity.available is True
    assert entity.is_on is True

    coordinator.data = MappingProxyType({configured.key: door("a", binding_digit="d")})
    assert entity.available is False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_code_phrase_sensor_is_added_once_when_exact_target_appears() -> None:
    configured = door("a")
    coordinator = coordinator_snapshot({})
    manager = FakeVoiceManager({(configured.key, configured.binding)})
    entry = acknowledged_entry()
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator,
        history_manager=FakeHistoryManager(),
        voice_manager=manager,
    )
    batches: list[list[Any]] = []

    await binary_sensor_module.async_setup_entry(
        None, entry, lambda values: batches.append(list(values))
    )
    coordinator.async_set_updated_data(MappingProxyType({configured.key: configured}))
    coordinator.async_set_updated_data(MappingProxyType({configured.key: configured}))

    phrase_entities = [
        entity
        for batch in batches
        for entity in batch
        if getattr(entity, "_attr_translation_key", None) == "code_phrase"
    ]
    assert len(phrase_entities) == 1


@pytest.mark.asyncio
async def test_voice_cleanup_precedes_provider_and_proxy_cleanup() -> None:
    events: list[str] = []

    class VoiceManager:
        async def async_stop(self) -> None:
            events.append("voice-stop")

    class HistoryPoller:
        async def async_stop(self) -> None:
            events.append("history-stop")

    runtime = SimpleNamespace(
        voice_manager=VoiceManager(),
        voice_session=LifecycleSession("voice-close", events),
        history_poller=HistoryPoller(),
        client=LifecycleClient(events),
        proxy_unsubscribe=lambda: events.append("proxy-unsubscribe"),
        rtsp_proxy=LifecycleProxy(events),
        read_session=LifecycleSession("read-close", events),
        open_session=LifecycleSession("open-close", events),
    )

    await runtime_module._async_drain_and_close(runtime)

    assert events[:3] == ["voice-stop", "history-stop", "drain"]
    assert events[3:5] == ["proxy-unsubscribe", "proxy-close"]
    assert set(events[5:]) == {"voice-close", "read-close", "open-close"}


@pytest.mark.asyncio
async def test_voice_stop_failure_still_closes_every_owned_resource() -> None:
    events: list[str] = []
    failure = RuntimeError("fixed synthetic stop failure")

    class VoiceManager:
        async def async_stop(self) -> None:
            events.append("voice-stop")
            raise failure

    class HistoryPoller:
        async def async_stop(self) -> None:
            events.append("history-stop")

    runtime = SimpleNamespace(
        voice_manager=VoiceManager(),
        voice_session=LifecycleSession("voice-close", events),
        history_poller=HistoryPoller(),
        client=LifecycleClient(events),
        proxy_unsubscribe=lambda: events.append("proxy-unsubscribe"),
        rtsp_proxy=LifecycleProxy(events),
        read_session=LifecycleSession("read-close", events),
        open_session=LifecycleSession("open-close", events),
    )

    with pytest.raises(RuntimeError) as raised:
        await runtime_module._async_drain_and_close(runtime)

    assert raised.value is failure
    assert events[:3] == ["voice-stop", "history-stop", "drain"]
    assert "proxy-close" in events
    assert {"voice-close", "read-close", "open-close"} <= set(events)


def test_optional_voice_options_fail_closed_without_runtime_resources() -> None:
    assert runtime_module._enabled_voice_config({}) is None
    assert (
        runtime_module._enabled_voice_config(
            {VOICE_PHRASE_OPTIONS_ROOT: {"version": 1, "enabled": False}}
        )
        is None
    )
    assert (
        runtime_module._enabled_voice_config(
            {VOICE_PHRASE_OPTIONS_ROOT: {"token": "PRIVATE-MALFORMED-TOKEN"}}
        )
        is None
    )


@pytest.mark.asyncio
async def test_enabled_voice_builds_inert_manager_and_dedicated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = door("a")
    config = runtime_module._enabled_voice_config(enabled_voice_options(target))
    assert config is not None
    setup_calls: list[tuple[Any, str, dict[str, object]]] = []

    async def setup_component(
        hass: Any, domain: str, config_data: dict[str, object]
    ) -> bool:
        setup_calls.append((hass, domain, config_data))
        return True

    monkeypatch.setattr(
        sys.modules["homeassistant.setup"], "async_setup_component", setup_component
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.components.ffmpeg"],
        "get_ffmpeg_manager",
        lambda _hass: SimpleNamespace(binary="ffmpeg"),
    )
    coordinator = coordinator_snapshot({target.key: target})
    proxy = FakeRtspProxy(target.key)

    async def executor(function: Any, value: object) -> object:
        return function(value)

    hass = SimpleNamespace(async_add_executor_job=executor)
    manager, created_session = await runtime_module._async_build_voice_manager(
        hass, config, coordinator, proxy
    )

    assert setup_calls == [(hass, "ffmpeg", {})]
    assert created_session is not None
    assert runtime_module._session_is_safe(created_session)
    assert manager is not None
    assert manager.configured_for(target.key, target.binding)
    assert manager.worker_count == 0
    await manager.async_stop()
    await created_session.close()


@pytest.mark.asyncio
async def test_ffmpeg_unavailable_creates_no_stt_session_or_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = door("a")
    config = runtime_module._enabled_voice_config(enabled_voice_options(target))
    assert config is not None

    async def setup_component(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(
        sys.modules["homeassistant.setup"], "async_setup_component", setup_component
    )
    monkeypatch.setattr(
        runtime_module,
        "_new_session",
        lambda: pytest.fail("STT session must not exist without FFmpeg"),
    )
    coordinator = coordinator_snapshot({target.key: target})
    manager, session = await runtime_module._async_build_voice_manager(
        SimpleNamespace(), config, coordinator, FakeRtspProxy(target.key)
    )

    assert session is None
    assert manager is not None
    await manager.async_start()
    await asyncio.sleep(0)
    assert manager.worker_count == 0
    assert manager.available_for(target.key) is False
    await manager.async_stop()


@pytest.mark.asyncio
async def test_setup_publishes_entities_before_voice_start_and_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = door("a", cctv_number="SYNTHETIC-CAMERA")
    discovered = MappingProxyType({target.key: target})
    events: list[str] = []

    class Client:
        async def async_update_inventory(self) -> Any:
            return discovered

        async def async_drain(self) -> None:
            events.append("drain")

    class Manager:
        started = False

        async def async_start(self) -> None:
            assert "forward" in events
            self.started = True
            events.append("voice-start")

        async def async_stop(self) -> None:
            events.append("voice-stop")

        def reconcile(self) -> None:
            events.append("voice-reconcile")

    manager = Manager()

    class Poller:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def async_start(self) -> None:
            events.append("history-start")

        async def async_stop(self) -> None:
            events.append("history-stop")

    class ConfigEntries(FakeConfigEntries):
        async def async_forward_entry_setups(
            self, entry: Any, platforms: tuple[str, ...]
        ) -> None:
            assert entry.runtime_data.voice_manager is manager
            assert manager.started is False
            events.append("forward")
            await super().async_forward_entry_setups(entry, platforms)

    class VoiceSession:
        closed = False

        async def close(self) -> None:
            self.closed = True
            events.append("voice-session-close")

    voice_session = VoiceSession()

    async def build_manager(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        events.append("voice-build")
        return manager, voice_session

    monkeypatch.setattr(
        runtime_module, "UfanetClient", lambda *_args, **_kwargs: Client()
    )
    monkeypatch.setattr(runtime_module, "CallHistoryPoller", Poller)
    monkeypatch.setattr(runtime_module, "_async_build_voice_manager", build_manager)
    config_entries = ConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = ConfigEntry(
        data=config_data(),
        options=enabled_voice_options(target),
        version=2,
        minor_version=2,
    )

    assert await runtime_module.async_setup_entry(hass, entry) is True
    assert events[:4] == [
        "voice-build",
        "forward",
        "voice-start",
        "history-start",
    ]
    assert len(entry.update_listeners) == 1
    await entry.update_listeners[0](hass, entry)
    assert config_entries.reload_calls == [entry.entry_id]
    entry.runtime_data.coordinator.async_set_updated_data(discovered)
    assert events[-1] == "voice-reconcile"

    await runtime_module._async_drain_and_close(entry.runtime_data)
    assert events.index("voice-stop") < events.index("drain")
    assert voice_session.closed is True


@pytest.mark.asyncio
async def test_setup_rollback_closes_all_resources_after_voice_stop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = door("a", cctv_number="SYNTHETIC-CAMERA")
    discovered = MappingProxyType({target.key: target})
    events: list[str] = []
    setup_failure = RuntimeError("fixed forwarding failure")

    class Client:
        async def async_update_inventory(self) -> Any:
            return discovered

    class Manager:
        async def async_stop(self) -> None:
            events.append("voice-stop")
            raise RuntimeError("fixed cleanup failure")

    class Poller:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def async_stop(self) -> None:
            events.append("history-stop")

    class Proxy(LifecycleProxy):
        def replace_bindings(self, _bindings: object) -> None:
            pass

    sessions = [
        LifecycleSession("read-close", events),
        LifecycleSession("open-close", events),
    ]
    voice_session = LifecycleSession("voice-close", events)
    proxy = Proxy(events)

    class ConfigEntries(FakeConfigEntries):
        async def async_forward_entry_setups(
            self, entry: Any, platforms: tuple[str, ...]
        ) -> None:
            del entry, platforms
            raise setup_failure

    async def build_manager(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return Manager(), voice_session

    monkeypatch.setattr(runtime_module, "_new_session", lambda: sessions.pop(0))
    monkeypatch.setattr(runtime_module, "_sessions_are_safe", lambda *_args: True)
    monkeypatch.setattr(
        runtime_module, "UfanetClient", lambda *_args, **_kwargs: Client()
    )
    monkeypatch.setattr(runtime_module, "CallHistoryPoller", Poller)
    monkeypatch.setattr(
        runtime_module,
        "_async_start_rtsp_proxy",
        lambda *_args: asyncio.sleep(0, result=proxy),
    )
    monkeypatch.setattr(runtime_module, "_async_build_voice_manager", build_manager)
    entry = ConfigEntry(
        data=config_data(),
        options=enabled_voice_options(target),
        version=2,
        minor_version=2,
    )

    with pytest.raises(RuntimeError) as raised:
        await runtime_module.async_setup_entry(
            SimpleNamespace(config_entries=ConfigEntries()), entry
        )

    assert raised.value is setup_failure
    assert "voice-stop" in events
    assert "history-stop" in events
    assert "proxy-close" in events
    assert {"voice-close", "read-close", "open-close"} <= set(events)
    assert entry.runtime_data is None
