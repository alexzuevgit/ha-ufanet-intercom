"""RED synthetic contract tests for safe config, reauth, and adoption flows.

Every provider object in this module is local and fake.  In particular, these tests
never instantiate the real client and make every session request method fail.
"""

# Imports must follow the dynamic sys.modules/sys.path Home Assistant stub bootstrap.
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

for module_name in tuple(sys.modules):
    if module_name == "custom_components.ufanet_intercom" or module_name.startswith(
        "custom_components.ufanet_intercom."
    ):
        del sys.modules[module_name]
sys.path.insert(0, str(Path(__file__).parent))
import ha_stub_import  # noqa: F401

config_flow_module = importlib.import_module(
    "custom_components.ufanet_intercom.config_flow"
)
from homeassistant.config_entries import ConfigEntry, FlowResultType

from custom_components.ufanet_intercom.api import (
    UfanetAuthenticationError,
    UfanetConnectionError,
    UfanetDiscoveryError,
    UfanetError,
    UfanetProtocolError,
)
from custom_components.ufanet_intercom.const import (
    CONF_CONTRACT,
    CONF_IDENTITY_KEY,
    CONF_PASSWORD,
    CONF_REQUIRES_ACK,
    CONF_TRUSTED_BINDINGS,
    DiscoveredDoor,
    contract_fingerprint,
    discovered_door_binding,
    discovered_door_key,
)

ACKNOWLEDGE = "acknowledge"
IDENTITY_KEY = bytes(range(32))
ENCODED_IDENTITY_KEY = base64.urlsafe_b64encode(IDENTITY_KEY).decode("ascii")
HMAC_RE = re.compile(r"^[0-9a-f]{64}$")
ACTIVE_FLOW_ID_RE = re.compile(r"^active-ufanet-[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DoorSpec:
    """Synthetic private provider fields used only to build a DiscoveredDoor."""

    shared_id: int
    display_name: str
    model: int = 21
    house: int = 7001
    provider_contract: int = 8001
    cctv_number: str = "synthetic-camera"


def binding_for(identity_key: bytes, spec: DoorSpec) -> str:
    return discovered_door_binding(
        identity_key,
        shared_id=spec.shared_id,
        door=0,
        model=spec.model,
        house=spec.house,
        contract=spec.provider_contract,
        cctv_number=spec.cctv_number,
    )


def bindings_for(identity_key: bytes, specs: list[DoorSpec]) -> dict[str, str]:
    return {
        discovered_door_key(identity_key, spec.shared_id): binding_for(
            identity_key, spec
        )
        for spec in specs
    }


class FakeOwnedSession:
    """Inspectable code-owned read session on which no request is permitted."""

    def __init__(self, owner: SyntheticProvider) -> None:
        self.owner = owner
        self._retry_connection = False
        self._middlewares: tuple[Any, ...] = ()
        self.headers: dict[str, str] = {}
        self.connector = object()
        self.timeout = SimpleNamespace(total=15)
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    async def post(self, url: str, **kwargs: Any) -> None:
        self.owner.session_requests.append(("POST", url, kwargs))
        raise AssertionError("flow must use only the synthetic client discovery method")

    async def get(self, url: str, **kwargs: Any) -> None:
        self.owner.session_requests.append(("GET", url, kwargs))
        raise AssertionError("no provider path is reachable from a flow test")


@dataclass(slots=True)
class ClientConstruction:
    session: FakeOwnedSession
    contract: str
    password: str
    identity_key: bytes
    trusted_bindings: dict[str, str]
    kwargs: dict[str, Any]
    calls: list[str]


class SyntheticProvider:
    """Factory and recorder for fully synthetic client/session behavior."""

    def __init__(self) -> None:
        self.specs: list[DoorSpec] = [
            DoorSpec(4101, "Synthetic lobby"),
            DoorSpec(4102, "Synthetic side", model=22),
        ]
        self.error: BaseException | None = None
        self.constructor_error: BaseException | None = None
        self.sessions: list[FakeOwnedSession] = []
        self.clients: list[ClientConstruction] = []
        self.shared_session_calls = 0
        self.session_requests: list[tuple[str, str, dict[str, Any]]] = []
        self.open_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.open_transport_arguments: list[Any] = []

    def new_session(self) -> FakeOwnedSession:
        session = FakeOwnedSession(self)
        self.sessions.append(session)
        return session

    def direct_session_factory(self, **_kwargs: Any) -> FakeOwnedSession:
        return self.new_session()

    def shared_session(self, _hass: object) -> object:
        self.shared_session_calls += 1
        raise AssertionError("Home Assistant's shared session is forbidden")

    def client_type(self) -> type[object]:
        owner = self

        class FakeUfanetClient:
            def __init__(
                self,
                session: FakeOwnedSession,
                contract: str,
                password: str,
                *args: Any,
                identity_key: bytes,
                trusted_bindings: dict[str, str] | None = None,
                **kwargs: Any,
            ) -> None:
                if owner.constructor_error is not None:
                    raise owner.constructor_error
                if args:
                    raise AssertionError(
                        "client security state must use named arguments"
                    )
                if "open_session" in kwargs:
                    owner.open_transport_arguments.append(kwargs["open_session"])
                self.record = ClientConstruction(
                    session=session,
                    contract=contract,
                    password=password,
                    identity_key=identity_key,
                    trusted_bindings=(
                        {} if trusted_bindings is None else dict(trusted_bindings)
                    ),
                    kwargs=dict(kwargs),
                    calls=[],
                )
                owner.clients.append(self.record)

            async def async_login_and_discover(
                self,
            ) -> MappingProxyType[str, DiscoveredDoor]:
                self.record.calls.append("async_login_and_discover")
                if owner.error is not None:
                    raise owner.error
                discovered: dict[str, DiscoveredDoor] = {}
                for spec in owner.specs:
                    key = discovered_door_key(self.record.identity_key, spec.shared_id)
                    binding = binding_for(self.record.identity_key, spec)
                    discovered[key] = DiscoveredDoor(
                        key=key,
                        shared_id=spec.shared_id,
                        door=0,
                        model=spec.model,
                        display_name=spec.display_name,
                        binding=binding,
                        openable=True,
                        trusted=self.record.trusted_bindings.get(key) == binding,
                    )
                return MappingProxyType(discovered)

            async def async_update_inventory(self) -> None:
                self.record.calls.append("async_update_inventory")
                raise AssertionError("a flow must perform an explicit fresh login")

            async def async_open(self, *args: Any, **kwargs: Any) -> None:
                owner.open_calls.append((args, kwargs))
                raise AssertionError("a physical action is forbidden in every flow")

        return FakeUfanetClient


class FakeConfigEntries:
    def __init__(self, entry: ConfigEntry | None = None) -> None:
        self.entry = entry
        self.update_calls: list[tuple[ConfigEntry, dict[str, Any]]] = []
        self.reload_calls: list[str] = []

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        if self.entry is not None and self.entry.entry_id == entry_id:
            return self.entry
        return None

    def async_entries(self, _domain: str | None = None) -> list[ConfigEntry]:
        """Return the currently configured synthetic domain entries."""

        return [] if self.entry is None else [self.entry]

    def async_update_entry(self, entry: ConfigEntry, **updates: Any) -> None:
        self.update_calls.append((entry, updates))
        if "data" in updates:
            entry.data = updates["data"]

    async def async_reload(self, entry_id: str) -> bool:
        self.reload_calls.append(entry_id)
        return True


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[SyntheticProvider]:
    synthetic = SyntheticProvider()
    monkeypatch.setattr(
        config_flow_module, "_new_session", synthetic.new_session, raising=False
    )
    monkeypatch.setattr(
        config_flow_module,
        "ClientSession",
        synthetic.direct_session_factory,
        raising=False,
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_get_clientsession",
        synthetic.shared_session,
        raising=False,
    )
    monkeypatch.setattr(
        config_flow_module,
        "async_create_clientsession",
        synthetic.shared_session,
        raising=False,
    )
    monkeypatch.setattr(config_flow_module, "UfanetClient", synthetic.client_type())
    yield synthetic
    assert synthetic.open_calls == []
    assert synthetic.open_transport_arguments == []
    assert synthetic.session_requests == []


def flow_for(entry: ConfigEntry | None = None) -> tuple[Any, FakeConfigEntries]:
    flow = config_flow_module.UfanetIntercomConfigFlow()
    entries = FakeConfigEntries(entry)
    flow.hass = SimpleNamespace(config_entries=entries)
    if entry is not None:
        flow.context = {"entry_id": entry.entry_id}
    return flow, entries


def entry_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_CONTRACT: "SYNTHETIC-ACCOUNT-42",
        CONF_PASSWORD: "old-password",
        CONF_IDENTITY_KEY: ENCODED_IDENTITY_KEY,
        CONF_TRUSTED_BINDINGS: {},
        CONF_REQUIRES_ACK: False,
        "future_field": {"preserve": [1, 2, 3]},
    }
    data.update(updates)
    return data


def assert_form(result: dict[str, Any], step_id: str) -> None:
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id


def assert_exact_schema(result: dict[str, Any], keys: set[str]) -> None:
    schema = result["data_schema"]
    assert isinstance(schema, dict)
    assert set(schema) == keys


def assert_safe_count_placeholders(
    result: dict[str, Any], *, expected_counts: list[int]
) -> None:
    placeholders = result["description_placeholders"]
    assert type(placeholders) is dict
    assert Counter(placeholders.values()) == Counter(map(str, expected_counts))
    assert all(
        type(value) is str and value.isdecimal() for value in placeholders.values()
    )


async def submit_form_step(
    handler: Any, result: dict[str, Any], user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit through the method named by the modern flow result."""

    step = getattr(handler, f"async_step_{result['step_id']}")
    return await step(user_input)


def assert_canonical_identity(encoded: object) -> bytes:
    assert type(encoded) is str
    raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    assert len(raw) == 32
    assert encoded == base64.urlsafe_b64encode(raw).decode("ascii")
    return raw


async def prepare_acknowledgement(
    provider: SyntheticProvider,
    *,
    contract: str = "synthetic-account-42",
    password: str = " edge-sensitive-password ",
) -> tuple[Any, dict[str, Any]]:
    flow, _entries = flow_for()
    result = await flow.async_step_user(
        {CONF_CONTRACT: contract, CONF_PASSWORD: password}
    )
    assert_form(result, "acknowledge")
    return flow, result


def options_flow_for(entry: ConfigEntry) -> tuple[Any, FakeConfigEntries]:
    handler = config_flow_module.UfanetIntercomConfigFlow.async_get_options_flow(entry)
    entries = FakeConfigEntries(entry)
    handler.hass = SimpleNamespace(config_entries=entries)
    handler.context = {"entry_id": entry.entry_id}
    return handler, entries


def test_flow_version_is_2() -> None:
    assert config_flow_module.UfanetIntercomConfigFlow.VERSION == 2


@pytest.mark.asyncio
async def test_same_contract_is_rejected_before_discovery_and_rechecked_at_ack(
    provider: SyntheticProvider,
) -> None:
    existing = ConfigEntry(
        entry_id="existing-entry",
        data=entry_data(**{CONF_CONTRACT: "SYNTHETIC-ACCOUNT-42"}),
        version=2,
    )

    duplicate_flow, _entries = flow_for(existing)
    duplicate_flow.context = {}
    result = await duplicate_flow.async_step_user(
        {
            CONF_CONTRACT: "synthetic-account-42",
            CONF_PASSWORD: "must-not-reach-provider",
        }
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert provider.sessions == []
    assert provider.clients == []

    candidate_flow, candidate_entries = flow_for()
    result = await candidate_flow.async_step_user(
        {
            CONF_CONTRACT: "synthetic-account-42",
            CONF_PASSWORD: "synthetic-password",
        }
    )
    assert_form(result, "acknowledge")

    # Another flow may create the entry while this user reviews the warning.
    candidate_entries.entry = existing
    result = await candidate_flow.async_step_acknowledge({ACKNOWLEDGE: True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_active_contract_gate_precedes_discovery_and_discloses_no_contract(
    provider: SyntheticProvider,
) -> None:
    flow, _entries = flow_for()
    observed: list[tuple[str | None, bool]] = []

    async def reject_as_concurrent(
        unique_id: str | None, *, raise_on_progress: bool = True
    ) -> None:
        observed.append((unique_id, raise_on_progress))
        raise RuntimeError("synthetic already_in_progress")

    flow.async_set_unique_id = reject_as_concurrent
    with pytest.raises(RuntimeError, match="already_in_progress"):
        await flow.async_step_user(
            {
                CONF_CONTRACT: "private-concurrent-contract",
                CONF_PASSWORD: "must-not-reach-provider",
            }
        )

    assert len(observed) == 1
    active_id, raise_on_progress = observed[0]
    assert type(active_id) is str
    assert ACTIVE_FLOW_ID_RE.fullmatch(active_id)
    assert "private-concurrent-contract" not in active_id
    assert raise_on_progress is True
    assert provider.sessions == []
    assert provider.clients == []


def test_initial_acknowledgement_describes_actuation_and_name_risks() -> None:
    component = (
        Path(__file__).resolve().parents[1] / "custom_components" / "ufanet_intercom"
    )

    def description(filename: str) -> str:
        document = json.loads((component / filename).read_text(encoding="utf-8"))
        value = document["config"]["step"]["acknowledge"]["description"]
        assert type(value) is str
        return value

    source = description("strings.json")
    english = description("translations/en.json")
    russian = description("translations/ru.json")
    assert source == english
    for text in (source, russian):
        assert Counter(re.findall(r"\{[^{}]+\}", text)) == Counter(
            {
                "{discovered_count}": 1,
                "{distinct_name_count}": 1,
                "{duplicate_name_count}": 1,
            }
        )

    def assert_semantic_concepts(
        text: str, concepts: dict[str, tuple[str, ...]]
    ) -> None:
        normalized = text.casefold()
        for concept, alternatives in concepts.items():
            assert any(value.casefold() in normalized for value in alternatives), (
                concept
            )

    assert_semantic_concepts(
        english,
        {
            "lock.open service call": ("lock.open",),
            "real physical actuation": ("physical actuation", "physically open"),
            "remote UI exposure": ("remote ui", "remote user interface"),
            "automation exposure": ("automation",),
            "voice-assistant exposure": ("voice assistant", "voice-assistant"),
            "remote exposure": ("expos",),
            "exposure can actuate": (
                "can actuate",
                "can also actuate",
                "can physically open",
            ),
            "provider display names": (
                "provider-supplied display name",
                "provider display name",
                "display names from the provider",
            ),
            "names may resemble addresses": ("address-like", "address like"),
            "display-name persistence": ("persist",),
            "entity registry persistence": ("entity registr",),
            "device registry persistence": ("device registr",),
            "backup persistence": ("backup",),
            "Recorder persistence": ("recorder",),
            "history persistence": ("history",),
            "remote UI persistence": ("remote ui", "remote user interface"),
            "voice-assistant metadata persistence": ("voice-assistant metadata",),
            "duplicate names": ("duplicate name",),
            "careful identification": ("careful identification", "carefully identify"),
        },
    )
    assert_semantic_concepts(
        russian,
        {
            "вызов службы lock.open": ("lock.open",),
            "реальное физическое срабатывание": ("физическ",),
            "удалённый интерфейс": ("удалённ",),
            "автоматизации": ("автоматизац",),
            "голосовые ассистенты": ("голосов",),
            "предоставление удалённого доступа": ("предоставление доступа",),
            "доступ может открыть вход": ("может открыть", "может физически открыть"),
            "отображаемые имена провайдера": ("отображаемые имена",),
            "провайдер": ("провайдер",),
            "имена могут быть похожи на адреса": ("могут быть похож",),
            "адреса": ("адрес",),
            "сохранение отображаемых имён": ("сохраня",),
            "реестр сущностей": ("реестре сущност", "реестр сущност"),
            "реестр устройств": ("реестре устройств", "реестр устройств"),
            "резервные копии": ("резервн",),
            "Recorder": ("recorder",),
            "история": ("истори",),
            "метаданные голосовых ассистентов": ("метадан",),
            "повторяющиеся имена": ("повторяющиеся имена",),
            "тщательная идентификация": ("тщательн",),
            "определение входа": ("определения входа", "идентификац"),
        },
    )


@pytest.mark.asyncio
async def test_user_schema_is_credentials_only() -> None:
    flow, _entries = flow_for()
    result = await flow.async_step_user()

    assert_form(result, "user")
    assert_exact_schema(result, {CONF_CONTRACT, CONF_PASSWORD})
    forbidden = {
        "target",
        "url",
        "id",
        "selector",
        "token",
        "door",
        "shared_id",
        "binding",
        "title",
    }
    assert set(result["data_schema"]).isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_user_discovery_owns_and_closes_one_safe_read_session(
    provider: SyntheticProvider,
) -> None:
    provider.specs = [
        DoorSpec(4101, "Private duplicate title"),
        DoorSpec(4102, "Private duplicate title", model=22),
    ]
    flow, result = await prepare_acknowledgement(provider)

    assert_safe_count_placeholders(
        result,
        expected_counts=[2, 1, 1],
    )
    assert len(provider.sessions) == len(provider.clients) == 1
    session = provider.sessions[0]
    construction = provider.clients[0]
    assert construction.session is session
    assert session.closed is True
    assert session.close_calls == 1
    assert session._retry_connection is False
    assert session._middlewares == ()
    assert session.connector is not None
    assert session.timeout.total == 15
    assert not any(key.lower() == "authorization" for key in session.headers)
    assert provider.shared_session_calls == 0
    assert construction.calls == ["async_login_and_discover"]
    assert construction.contract == "synthetic-account-42"
    assert construction.password == " edge-sensitive-password "
    assert type(construction.identity_key) is bytes
    assert len(construction.identity_key) == 32
    assert construction.trusted_bindings == {}
    assert construction.kwargs == {}
    assert type(flow._unique_id) is str
    assert ACTIVE_FLOW_ID_RE.fullmatch(flow._unique_id)
    assert "synthetic-account-42" not in flow._unique_id

    rendered = repr(result)
    for private in (
        "4101",
        "4102",
        "Private duplicate title",
        binding_for(construction.identity_key, provider.specs[0]),
    ):
        assert private not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookalike",
    [False, 1, "true"],
)
async def test_initial_acknowledgement_requires_exact_true(
    provider: SyntheticProvider, lookalike: Any
) -> None:
    flow, _result = await prepare_acknowledgement(provider)

    result = await flow.async_step_acknowledge({ACKNOWLEDGE: lookalike})

    assert_form(result, "acknowledge")
    assert_exact_schema(result, {ACKNOWLEDGE})
    assert result.get("data") is None


@pytest.mark.asyncio
async def test_initial_acknowledgement_rejects_extra_or_target_input(
    provider: SyntheticProvider,
) -> None:
    flow, _result = await prepare_acknowledgement(provider)

    result = await flow.async_step_acknowledge(
        {ACKNOWLEDGE: True, "target": 4101, "url": "https://private.invalid"}
    )

    assert_form(result, "acknowledge")
    assert_exact_schema(result, {ACKNOWLEDGE})


@pytest.mark.asyncio
async def test_exact_ack_creates_only_safe_persistent_candidate_data(
    provider: SyntheticProvider,
) -> None:
    provider.specs = [
        DoorSpec(4101, "Synthetic lobby"),
        DoorSpec(4102, "Synthetic side", model=22),
    ]
    password = " byte-for-byte password \n"
    flow, acknowledgement = await prepare_acknowledgement(
        provider,
        contract="mixed-case-contract",
        password=password,
    )
    active_flow_id = flow._unique_id

    result = await flow.async_step_acknowledge({ACKNOWLEDGE: True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert set(data) == {
        CONF_CONTRACT,
        CONF_PASSWORD,
        CONF_IDENTITY_KEY,
        CONF_TRUSTED_BINDINGS,
        CONF_REQUIRES_ACK,
    }
    assert data[CONF_CONTRACT] == "MIXED-CASE-CONTRACT"
    assert data[CONF_PASSWORD] == password
    identity_key = assert_canonical_identity(data[CONF_IDENTITY_KEY])
    expected_bindings = bindings_for(identity_key, provider.specs)
    assert data[CONF_TRUSTED_BINDINGS] == expected_bindings
    assert data[CONF_REQUIRES_ACK] is False
    assert all(HMAC_RE.fullmatch(key) for key in expected_bindings)
    assert all(HMAC_RE.fullmatch(value) for value in expected_bindings.values())
    assert flow._unique_id == contract_fingerprint(identity_key, "mixed-case-contract")
    assert flow._unique_id != active_flow_id
    assert flow._raise_on_progress is False
    assert provider.clients[0].identity_key == identity_key
    assert provider.sessions[0].closed is True
    assert_safe_count_placeholders(
        acknowledgement,
        expected_counts=[2, 2, 0],
    )

    rendered = repr(result)
    for spec in provider.specs:
        assert str(spec.shared_id) not in rendered
        assert spec.display_name not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "error_code"),
    [
        (UfanetAuthenticationError("private-auth-detail"), "invalid_auth"),
        (UfanetDiscoveryError("private-target-detail"), "invalid_targets"),
        (UfanetConnectionError("private-network-detail"), "cannot_connect"),
        (UfanetProtocolError("private-protocol-detail"), "unknown"),
        (UfanetError("private-generic-detail"), "unknown"),
        (RuntimeError("private-unexpected-detail"), "unknown"),
    ],
)
async def test_user_errors_are_fixed_private_and_close_session(
    provider: SyntheticProvider,
    provider_error: BaseException,
    error_code: str,
) -> None:
    private_context = RuntimeError("private-cause-and-context")
    provider_error.__cause__ = private_context
    provider_error.__context__ = private_context
    provider.error = provider_error
    flow, _entries = flow_for()

    result = await flow.async_step_user(
        {CONF_CONTRACT: "synthetic-account", CONF_PASSWORD: "synthetic-password"}
    )

    assert_form(result, "user")
    assert result["errors"] == {"base": error_code}
    assert "description_placeholders" not in result
    assert "private" not in repr(result).lower()
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True
    assert provider.sessions[0].close_calls == 1
    assert provider.clients[0].calls == ["async_login_and_discover"]


@pytest.mark.asyncio
async def test_constructor_failure_is_private_and_still_closes_session(
    provider: SyntheticProvider,
) -> None:
    provider.constructor_error = RuntimeError("private-constructor-detail")
    flow, _entries = flow_for()

    result = await flow.async_step_user(
        {CONF_CONTRACT: "synthetic-account", CONF_PASSWORD: "synthetic-password"}
    )

    assert_form(result, "user")
    assert result["errors"] == {"base": "unknown"}
    assert "private" not in repr(result).lower()
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True
    assert provider.sessions[0].close_calls == 1


@pytest.mark.asyncio
async def test_user_cancellation_identity_and_structure_propagate_after_close(
    provider: SyntheticProvider,
) -> None:
    cancellation = asyncio.CancelledError("structured-cancellation-detail", {"code": 7})
    cancellation.add_note("structured-note")
    provider.error = cancellation
    flow, _entries = flow_for()

    with pytest.raises(asyncio.CancelledError) as raised:
        await flow.async_step_user(
            {CONF_CONTRACT: "synthetic-account", CONF_PASSWORD: "synthetic-password"}
        )

    assert raised.value is cancellation
    assert raised.value.args == ("structured-cancellation-detail", {"code": 7})
    assert raised.value.__notes__ == ["structured-note"]
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True
    assert provider.sessions[0].close_calls == 1


@pytest.mark.asyncio
async def test_reauth_uses_stored_identity_and_bindings_and_updates_only_password(
    provider: SyntheticProvider,
) -> None:
    provider.specs = [DoorSpec(4101, "Synthetic lobby")]
    trusted = bindings_for(IDENTITY_KEY, provider.specs)
    original = entry_data(**{CONF_TRUSTED_BINDINGS: trusted, CONF_REQUIRES_ACK: True})
    entry = ConfigEntry(
        entry_id="reauth-entry", data=copy.deepcopy(original), version=2
    )
    flow, entries = flow_for(entry)

    start = await flow.async_step_reauth(dict(entry.data))
    assert_form(start, "reauth_confirm")
    assert_exact_schema(start, {CONF_PASSWORD})
    result = await flow.async_step_reauth_confirm(
        {CONF_PASSWORD: " new edge-sensitive password "}
    )

    assert result == {
        "type": FlowResultType.ABORT,
        "reason": "reauth_successful",
    }
    assert flow.update_reload_calls == [
        {
            "entry": entry,
            "data_updates": {CONF_PASSWORD: " new edge-sensitive password "},
            "reason": "reauth_successful",
        }
    ]
    assert entry.data == {
        **original,
        CONF_PASSWORD: " new edge-sensitive password ",
    }
    assert entries.update_calls == []
    assert entries.reload_calls == []
    assert len(provider.clients) == len(provider.sessions) == 1
    construction = provider.clients[0]
    assert construction.identity_key == IDENTITY_KEY
    assert construction.trusted_bindings == trusted
    assert construction.calls == ["async_login_and_discover"]
    assert provider.sessions[0].closed is True
    assert provider.shared_session_calls == 0


@pytest.mark.asyncio
async def test_reauth_failure_is_fixed_preserves_entry_and_closes(
    provider: SyntheticProvider,
) -> None:
    provider.error = UfanetConnectionError("private-reauth-detail")
    original = entry_data(
        **{CONF_TRUSTED_BINDINGS: {"a" * 64: "b" * 64}, "future": object()}
    )
    entry = ConfigEntry(entry_id="reauth-entry", data=copy.copy(original), version=2)
    flow, _entries = flow_for(entry)
    await flow.async_step_reauth(dict(entry.data))

    result = await flow.async_step_reauth_confirm({CONF_PASSWORD: "replacement"})

    assert_form(result, "reauth_confirm")
    assert result["errors"] == {"base": "cannot_connect"}
    assert "private" not in repr(result).lower()
    assert entry.data == original
    assert flow.update_reload_calls == []
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True


@pytest.mark.asyncio
async def test_reauth_cancel_propagates_same_object_after_close(
    provider: SyntheticProvider,
) -> None:
    cancellation = asyncio.CancelledError("structured-reauth-cancel")
    cancellation.add_note("reauth-note")
    provider.error = cancellation
    entry = ConfigEntry(entry_id="reauth-entry", data=entry_data(), version=2)
    flow, _entries = flow_for(entry)
    await flow.async_step_reauth(dict(entry.data))

    with pytest.raises(asyncio.CancelledError) as raised:
        await flow.async_step_reauth_confirm({CONF_PASSWORD: "replacement"})

    assert raised.value is cancellation
    assert raised.value.args == ("structured-reauth-cancel",)
    assert raised.value.__notes__ == ["reauth-note"]
    assert provider.sessions[0].closed is True
    assert flow.update_reload_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("specs", [[], [DoorSpec(4101, "Synthetic lobby")]])
async def test_options_with_no_pending_targets_aborts_safely(
    provider: SyntheticProvider,
    specs: list[DoorSpec],
) -> None:
    provider.specs = specs
    trusted = bindings_for(IDENTITY_KEY, specs)
    entry = ConfigEntry(
        entry_id="options-entry",
        data=entry_data(**{CONF_TRUSTED_BINDINGS: trusted}),
        version=2,
    )
    options, entries = options_flow_for(entry)

    result = await options.async_step_init()

    assert result["type"] is FlowResultType.ABORT
    assert re.fullmatch(r"[a-z0-9_]+", result["reason"])
    assert "private" not in repr(result).lower()
    assert entries.update_calls == []
    assert entries.reload_calls == []
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True
    assert provider.clients[0].identity_key == IDENTITY_KEY
    assert provider.clients[0].trusted_bindings == trusted
    assert provider.clients[0].calls == ["async_login_and_discover"]
    assert provider.shared_session_calls == 0


@pytest.mark.asyncio
async def test_options_adoption_confirms_only_aggregates_then_merges_and_reloads(
    provider: SyntheticProvider,
) -> None:
    unchanged = DoorSpec(4101, "Private unchanged title")
    new = DoorSpec(4102, "Private new title", model=22)
    old_changed = DoorSpec(
        4103, "Private old changed title", model=23, cctv_number="old-camera"
    )
    changed = DoorSpec(
        4103, "Private changed title", model=23, cctv_number="new-camera"
    )
    stored = bindings_for(IDENTITY_KEY, [unchanged, old_changed])
    provider.specs = [unchanged, new, changed]
    original = entry_data(
        **{
            CONF_TRUSTED_BINDINGS: stored,
            CONF_REQUIRES_ACK: True,
            "future": {"preserve": "exactly"},
        }
    )
    entry = ConfigEntry(
        entry_id="options-entry", data=copy.deepcopy(original), version=2
    )
    options, entries = options_flow_for(entry)

    confirmation = await options.async_step_init()

    assert confirmation["type"] is FlowResultType.FORM
    assert_exact_schema(confirmation, {ACKNOWLEDGE})
    assert_safe_count_placeholders(
        confirmation,
        expected_counts=[2, 1, 1],
    )
    rendered = repr(confirmation)
    for private in (
        "4101",
        "4102",
        "4103",
        "Private unchanged title",
        "Private new title",
        "Private changed title",
        binding_for(IDENTITY_KEY, changed),
    ):
        assert private not in rendered
    assert len(provider.sessions) == 1
    assert provider.sessions[0].closed is True
    assert provider.clients[0].calls == ["async_login_and_discover"]

    for rejected in (
        {ACKNOWLEDGE: False},
        {ACKNOWLEDGE: 1},
        {ACKNOWLEDGE: "true"},
        {ACKNOWLEDGE: True, "target": 4102},
    ):
        rejected_result = await submit_form_step(options, confirmation, rejected)
        assert_form(rejected_result, confirmation["step_id"])
        assert_exact_schema(rejected_result, {ACKNOWLEDGE})
        assert entry.data == original
        assert entries.update_calls == []
        assert entries.reload_calls == []

    result = await submit_form_step(options, confirmation, {ACKNOWLEDGE: True})

    expected_bindings = {
        **stored,
        **bindings_for(IDENTITY_KEY, [new, changed]),
    }
    assert result == {
        "type": FlowResultType.ABORT,
        "reason": "adoption_successful",
    }
    assert entry.data == {
        **original,
        CONF_TRUSTED_BINDINGS: expected_bindings,
        CONF_REQUIRES_ACK: False,
    }
    assert len(entries.update_calls) == 1
    updated_entry, updates = entries.update_calls[0]
    assert updated_entry is entry
    assert updates == {"data": entry.data}
    assert entries.reload_calls == [entry.entry_id]
    assert len(provider.sessions) == 1


@pytest.mark.asyncio
async def test_options_failure_and_cancel_both_close_owned_session(
    provider: SyntheticProvider,
) -> None:
    entry = ConfigEntry(entry_id="options-entry", data=entry_data(), version=2)

    provider.error = UfanetDiscoveryError("private-options-detail")
    options, _entries = options_flow_for(entry)
    failed = await options.async_step_init()
    assert_form(failed, "init")
    assert failed["errors"] == {"base": "invalid_targets"}
    assert "private" not in repr(failed).lower()
    assert provider.sessions[-1].closed is True

    cancellation = asyncio.CancelledError("structured-options-cancel")
    cancellation.add_note("options-note")
    provider.error = cancellation
    options, _entries = options_flow_for(entry)
    with pytest.raises(asyncio.CancelledError) as raised:
        await options.async_step_init()
    assert raised.value is cancellation
    assert raised.value.args == ("structured-options-cancel",)
    assert raised.value.__notes__ == ["options-note"]
    assert provider.sessions[-1].closed is True
    assert len(provider.sessions) == 2
