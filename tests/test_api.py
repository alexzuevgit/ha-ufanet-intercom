"""Pure contract tests for the generic Ufanet API client.

All provider data is synthetic and no test performs network I/O.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass
from typing import Any
from uuid import uuid4

import pytest
from aiohttp import ClientSession as AiohttpClientSession
from aiohttp import TCPConnector as AiohttpTCPConnector

import custom_components.ufanet_intercom.api as api_module
from custom_components.ufanet_intercom.api import (
    MAX_AUTH_RESPONSE_BYTES,
    MAX_DISCOVERY_ITEMS,
    OPEN_COOLDOWN_SECONDS,
    UfanetAuthenticationError,
    UfanetClient,
    UfanetConcurrentOpenError,
    UfanetConnectionError,
    UfanetDiscoveryError,
    UfanetOpenError,
    UfanetOpenUnknownOutcome,
    UfanetProtocolError,
    _current_open_gate,
)
from custom_components.ufanet_intercom.const import (
    AUTH_PATH,
    BASE_URL,
    DISCOVERY_PATH,
    REFRESH_PATH,
    USER_AGENT,
    DiscoveredDoor,
    contract_fingerprint,
    discovered_door_binding,
    discovered_door_key,
)

IDENTITY_KEY = bytes(range(32))
OTHER_IDENTITY_KEY = bytes(range(1, 33))
_UNSET_CONNECTOR = object()


class FakeConnector:
    """Concrete test connector used as the expected unit-test transport type."""


@dataclass(slots=True)
class RecordedRequest:
    method: str
    url: str
    kwargs: dict[str, Any]


class FakeContent:
    def __init__(self, body: bytes, *, chunk_size: int | None = None) -> None:
        self._body = body
        self._offset = 0
        self._chunk_size = chunk_size
        self.read_limits: list[int] = []

    async def read(self, limit: int = -1) -> bytes:
        self.read_limits.append(limit)
        remaining = len(self._body) - self._offset
        if remaining <= 0:
            return b""
        count = remaining if limit < 0 else min(remaining, limit)
        if self._chunk_size is not None:
            count = min(count, self._chunk_size)
        start = self._offset
        self._offset += count
        return self._body[start : start + count]


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = FakeContent(body)
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1


class HostileReleaseResponse(FakeResponse):
    def __init__(self, status: int, body: bytes, hostile: str) -> None:
        super().__init__(status, body)
        self._hostile = hostile

    def release(self) -> None:
        self.release_calls += 1
        raise RuntimeError(self._hostile)


Queued = FakeResponse | BaseException | Callable[[], Awaitable[FakeResponse]]


class FakeSession:
    def __init__(
        self,
        *queued: Queued,
        retry_connection: bool = False,
        middlewares: tuple[Any, ...] | list[Any] = (),
        default_headers: Mapping[str, str] | None = None,
        connector: object = _UNSET_CONNECTOR,
    ) -> None:
        self._retry_connection = retry_connection
        self._middlewares = middlewares
        self.headers = {} if default_headers is None else default_headers
        self.connector = FakeConnector() if connector is _UNSET_CONNECTOR else connector
        self.queue = list(queued)
        self.requests: list[RecordedRequest] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self._request("POST", url, kwargs)

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self._request("GET", url, kwargs)

    async def _request(
        self, method: str, url: str, kwargs: dict[str, Any]
    ) -> FakeResponse:
        self.requests.append(RecordedRequest(method, url, kwargs))
        if not self.queue:
            raise AssertionError("Unexpected request")
        queued = self.queue.pop(0)
        if isinstance(queued, BaseException):
            raise queued
        if callable(queued):
            return await queued()
        return queued


class MissingRetrySession(FakeSession):
    def __init__(self, *queued: Queued) -> None:
        super().__init__(*queued)
        del self._retry_connection


class MissingMiddlewaresSession(FakeSession):
    def __init__(self, *queued: Queued) -> None:
        super().__init__(*queued)
        del self._middlewares


class MissingDefaultHeadersSession(FakeSession):
    def __init__(self, *queued: Queued) -> None:
        super().__init__(*queued)
        del self.headers


class MissingConnectorSession(FakeSession):
    def __init__(self, *queued: Queued) -> None:
        super().__init__(*queued)
        del self.connector


class DelegatingSession(FakeSession):
    """Different concrete wrapper type rejected by the physical transport gate."""


@pytest.fixture(autouse=True)
def expected_test_transport_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind pure unit tests to explicit concrete test transport types."""

    monkeypatch.setattr(api_module, "ClientSession", FakeSession, raising=False)
    monkeypatch.setattr(api_module, "TCPConnector", FakeConnector, raising=False)


class ExplodingBindings(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("private-binding-detail")

    def __len__(self) -> int:
        return 1


def runtime_secret() -> str:
    return f"{uuid4().hex}{uuid4().hex}"


def json_response(status: int, value: Any) -> FakeResponse:
    return FakeResponse(
        status,
        json.dumps(value, separators=(",", ":"), allow_nan=False).encode(),
    )


def future_exp() -> int:
    return int(time.time()) + 3600


def valid_login(
    access: str = "aaa.bbb.ccc",
    refresh: str = "ddd.eee.fff",
    *,
    exp: int | None = None,
) -> dict[str, Any]:
    return {
        "token": {
            "access": access,
            "refresh": refresh,
            "exp": future_exp() if exp is None else exp,
        }
    }


def valid_refresh(
    access: str = "aaa.bbb.ccc",
    refresh: str = "ddd.eee.fff",
    *,
    exp: int | None = None,
) -> dict[str, Any]:
    return {
        "access": access,
        "refresh": refresh,
        "exp": future_exp() if exp is None else exp,
    }


def shared_item(
    shared_id: int,
    *,
    model: int = 21,
    custom_name: Any = "  Front door  ",
    string_view: Any = "Fallback door",
    open_type: Any = "http",
    disable_button: Any = False,
    is_blocked: Any = False,
    role: Any = None,
    cctv_number: Any = "synthetic-camera",
    relays: Any = None,
    house: Any = 7001,
    contract: Any = 8001,
    **extra: Any,
) -> dict[str, Any]:
    item = {
        "id": shared_id,
        "model": model,
        "custom_name": custom_name,
        "string_view": string_view,
        "open_type": open_type,
        "disable_button": disable_button,
        "is_blocked": is_blocked,
        "role": {"id": 2, "name": "synthetic-role"} if role is None else role,
        "cctv_number": cctv_number,
        "relays": [] if relays is None else relays,
        "address": "synthetic-private-address",
        "contract": contract,
        "house": house,
    }
    item.update(extra)
    return item


def trusted_bindings_for(
    inventory: list[dict[str, Any]], identity_key: bytes = IDENTITY_KEY
) -> dict[str, str]:
    trusted: dict[str, str] = {}
    for item in inventory:
        try:
            key = discovered_door_key(identity_key, item["id"], 0)
            trusted[key] = discovered_door_binding(
                identity_key,
                shared_id=item["id"],
                door=0,
                model=item["model"],
                house=item.get("house"),
                contract=item.get("contract"),
                cctv_number=item["cctv_number"],
                house_present="house" in item,
                contract_present="contract" in item,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return trusted


def four_item_inventory() -> list[dict[str, Any]]:
    return [
        shared_item(1101, model=31, custom_name="  Lobby  "),
        shared_item(1102, model=32, custom_name="", string_view="  Side door  "),
        shared_item(
            1103, model=33, custom_name=None, string_view=None, is_blocked=True
        ),
        shared_item(
            1104,
            model=34,
            custom_name=None,
            string_view=None,
            disable_button=True,
            cctv_number="",
        ),
    ]


async def logged_in_client(
    inventory: list[dict[str, Any]] | None = None,
    *,
    read_after: tuple[Queued, ...] = (),
    open_after: tuple[Queued, ...] = (),
    open_session: FakeSession | None = None,
    trusted: bool = True,
) -> tuple[UfanetClient, FakeSession, FakeSession]:
    selected_inventory = inventory if inventory is not None else four_item_inventory()
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, selected_inventory),
        *read_after,
    )
    physical_session = open_session or FakeSession(*open_after)
    client = UfanetClient(
        read_session,
        "contract-42",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        open_session=physical_session,
        trusted_bindings=(
            trusted_bindings_for(selected_inventory) if trusted else None
        ),
    )
    discovered = await client.async_login_and_discover()
    assert discovered is client.doors
    return client, read_session, physical_session


def key_for_shared_id(doors: Mapping[str, DiscoveredDoor], shared_id: int) -> str:
    return next(key for key, door in doors.items() if door.shared_id == shared_id)


def test_account_fingerprint_is_full_keyed_hmac() -> None:
    contract = "Synthetic-Contract-42"
    first = contract_fingerprint(IDENTITY_KEY, contract)
    second = contract_fingerprint(OTHER_IDENTITY_KEY, contract.lower())

    assert first.startswith("ufanet-")
    assert len(first) == len("ufanet-") + 64
    assert len(second) == len("ufanet-") + 64
    assert first != second
    assert contract.lower() not in first.lower()
    assert first == contract_fingerprint(IDENTITY_KEY, contract.lower())
    with pytest.raises(ValueError, match=r"^invalid contract$"):
        contract_fingerprint(IDENTITY_KEY, "x" * 513)


def test_full_hmac_keys_are_keyed_and_discovered_repr_is_private() -> None:
    first_key = discovered_door_key(IDENTITY_KEY, 1001, 0)
    second_key = discovered_door_key(OTHER_IDENTITY_KEY, 1001, 0)
    binding = discovered_door_binding(
        IDENTITY_KEY,
        shared_id=1001,
        door=0,
        model=21,
        house=7001,
        contract=None,
        cctv_number="private-camera-association",
    )
    door = DiscoveredDoor(
        key=first_key,
        shared_id=1001,
        door=0,
        model=21,
        display_name="Private display name",
        binding=binding,
        openable=True,
        trusted=True,
    )

    assert len(first_key) == len(second_key) == len(binding) == 64
    assert bytes.fromhex(first_key) != bytes.fromhex(second_key)
    rendered = repr(door)
    for private in ("1001", "Private display name", binding):
        assert private not in rendered
    for provider_label in ("model=", "openable=", "trusted="):
        assert provider_label not in rendered


@pytest.mark.asyncio
async def test_provider_name_precedence_is_bounded_and_numeric_id_is_never_name() -> (
    None
):
    inventory = [
        shared_item(1008, custom_name=" Custom ", string_view="View"),
        shared_item(1009, custom_name=" ", string_view=" View "),
        shared_item(1010, custom_name=None, string_view=None, address=None),
        shared_item(1011, custom_name=7, string_view=8, address=" Address "),
        shared_item(
            1012,
            custom_name="x" * 513,
            string_view="y" * 513,
            address=" Address fallback ",
        ),
    ]
    client, _, _ = await logged_in_client(inventory)
    assert [door.display_name for door in client.doors.values()] == [
        "Custom",
        "View",
        "Ufanet intercom",
        "Address",
        "Address fallback",
    ]
    assert all(
        str(item["id"]) not in door.display_name
        for item, door in zip(inventory, client.doors.values(), strict=True)
    )


def test_binding_presence_markers_are_canonical_and_strict() -> None:
    kwargs = {
        "shared_id": 1002,
        "door": 0,
        "model": 21,
        "house": None,
        "contract": None,
        "cctv_number": "synthetic-camera",
    }
    present = discovered_door_binding(IDENTITY_KEY, **kwargs)
    missing_house = discovered_door_binding(
        IDENTITY_KEY,
        **kwargs,
        house_present=False,
    )
    missing_contract = discovered_door_binding(
        IDENTITY_KEY,
        **kwargs,
        contract_present=False,
    )

    assert len({present, missing_house, missing_contract}) == 3
    with pytest.raises(ValueError, match="invalid door binding"):
        discovered_door_binding(
            IDENTITY_KEY,
            **{**kwargs, "house": 7001},
            house_present=False,
        )
    with pytest.raises(ValueError, match="invalid door binding"):
        discovered_door_binding(
            IDENTITY_KEY,
            **kwargs,
            contract_present=0,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "identity_key",
    [b"", b"x" * 31, b"x" * 33, bytearray(b"x" * 32)],
)
def test_client_requires_exact_32_byte_identity_key(identity_key: Any) -> None:
    with pytest.raises(ValueError, match=r"^Invalid identity key\.$"):
        UfanetClient(FakeSession(), "c", runtime_secret(), identity_key=identity_key)


def test_hostile_trusted_mapping_context_is_detached() -> None:
    with pytest.raises(ValueError, match=r"^Invalid trusted bindings\.$") as raised:
        UfanetClient(
            FakeSession(),
            "c",
            runtime_secret(),
            identity_key=IDENTITY_KEY,
            trusted_bindings=ExplodingBindings(),
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_login_uses_exact_endpoint_schema_and_secret_safe_repr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = runtime_secret()
    access = f"aaa.{uuid4().hex}.ccc"
    refresh = f"ddd.{uuid4().hex}.fff"
    response = json_response(200, valid_login(access, refresh))
    session = FakeSession(response)
    client = UfanetClient(session, "contract-42", secret, identity_key=IDENTITY_KEY)

    await client.async_login()

    request = session.requests[0]
    assert (request.method, request.url) == ("POST", f"{BASE_URL}{AUTH_PATH}")
    assert request.kwargs["json"] == {
        "contract": "CONTRACT-42",
        "password": secret,
    }
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["headers"]["User-Agent"] == USER_AGENT
    assert response.content.read_limits[0] == MAX_AUTH_RESPONSE_BYTES + 1
    assert len(response.content.read_limits) >= 2
    assert response.release_calls == 1
    rendered = repr(client)
    for private in (secret, access, refresh, "contract-42"):
        assert private.lower() not in rendered.lower()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.asyncio
async def test_password_is_preserved_exactly_including_edge_whitespace() -> None:
    password = f" {uuid4().hex} "
    session = FakeSession(json_response(200, valid_login()))
    client = UfanetClient(session, "contract", password, identity_key=IDENTITY_KEY)

    await client.async_login()

    assert session.requests[0].kwargs["json"]["password"] == password


@pytest.mark.asyncio
async def test_login_normalizes_optional_jwt_prefix() -> None:
    session = FakeSession(
        json_response(
            200,
            valid_login("JWT aaa.bbb.ccc", "JWT ddd.eee.fff"),
        ),
        json_response(200, [shared_item(1201)]),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    await client.async_login_and_discover()

    assert session.requests[1].kwargs["headers"]["Authorization"] == "JWT aaa.bbb.ccc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": {"access": "aaa.bbb.ccc", "refresh": "ddd.eee.fff"}},
        {
            "token": {
                "access": "aaa.bbb.ccc",
                "refresh": "ddd.eee.fff",
                "exp": True,
            }
        },
        {
            "token": {
                "access": "aaa.bbb.ccc",
                "refresh": "ddd.eee.fff",
                "exp": 1,
            }
        },
        {
            "token": {
                "access": "aaa.bbb.ccc",
                "refresh": "ddd.eee.fff",
                "exp": 2**63,
            }
        },
        {
            "token": {
                "access": "aaa.bbb.ccc",
                "refresh": "ddd.eee.fff",
                "exp": future_exp(),
                "extra": 1,
            }
        },
        {"token": valid_refresh(), "extra": 1},
    ],
)
async def test_login_rejects_non_exact_or_nonfuture_schema(payload: Any) -> None:
    client = UfanetClient(
        FakeSession(json_response(200, payload)),
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
    )

    with pytest.raises(
        UfanetProtocolError, match=r"^Invalid authentication response\.$"
    ):
        await client.async_login()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"token":{"access":"aaa.bbb.ccc","access":"xxx.yyy.zzz","refresh":"ddd.eee.fff","exp":4102444800}}',
        b'{"token":{"access":"aaa.bbb.ccc","refresh":"ddd.eee.fff","exp":NaN}}',
        b"not-json",
        b"\xff",
    ],
)
async def test_login_rejects_duplicate_nan_malformed_and_non_utf8(body: bytes) -> None:
    client = UfanetClient(
        FakeSession(FakeResponse(200, body)),
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
    )

    with pytest.raises(UfanetProtocolError) as raised:
        await client.async_login()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_login_rejects_oversize_with_bounded_read() -> None:
    response = FakeResponse(200, b"{" + b"x" * (MAX_AUTH_RESPONSE_BYTES + 5))
    client = UfanetClient(
        FakeSession(response), "c", runtime_secret(), identity_key=IDENTITY_KEY
    )

    with pytest.raises(UfanetProtocolError) as raised:
        await client.async_login()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None

    assert response.content.read_limits[0] == MAX_AUTH_RESPONSE_BYTES + 1
    assert len(response.content.read_limits) == 1


@pytest.mark.asyncio
async def test_auth_failure_does_not_expose_provider_body_or_secrets() -> None:
    secret = runtime_secret()
    hostile = runtime_secret()
    client = UfanetClient(
        FakeSession(FakeResponse(403, hostile.encode(), content_type="text/plain")),
        hostile,
        secret,
        identity_key=IDENTITY_KEY,
    )

    with pytest.raises(UfanetAuthenticationError) as raised:
        await client.async_login()

    assert str(raised.value) == "Authentication failed."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert secret not in repr(raised.value)
    assert hostile not in repr(raised.value)


@pytest.mark.asyncio
async def test_hostile_read_transport_context_is_detached() -> None:
    hostile = runtime_secret()
    client = UfanetClient(
        FakeSession(OSError(f"private URL/header/body {hostile}")),
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
    )

    with pytest.raises(UfanetConnectionError) as raised:
        await client.async_login()

    assert str(raised.value) == "Unable to contact Ufanet."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert hostile not in repr(raised.value)


@pytest.mark.asyncio
async def test_hostile_release_exception_is_suppressed_and_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile = runtime_secret()
    response = HostileReleaseResponse(
        200,
        json.dumps(valid_login(), separators=(",", ":")).encode(),
        hostile,
    )
    client = UfanetClient(
        FakeSession(response),
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
    )

    await client.async_login()

    assert response.release_calls == 1
    assert hostile not in repr(client)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.asyncio
async def test_inventory_401_refreshes_at_exact_endpoint_then_retries_once() -> None:
    refresh_response = json_response(200, valid_refresh())
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(401, {"detail": "expired"}),
        refresh_response,
        json_response(200, [shared_item(1301)]),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    result = await client.async_login_and_discover()

    assert len(result) == 1
    assert [(request.method, request.url) for request in session.requests] == [
        ("POST", f"{BASE_URL}{AUTH_PATH}"),
        ("GET", f"{BASE_URL}{DISCOVERY_PATH}"),
        ("POST", f"{BASE_URL}{REFRESH_PATH}"),
        ("GET", f"{BASE_URL}{DISCOVERY_PATH}"),
    ]
    assert session.requests[2].kwargs["json"] == {"token": "ddd.eee.fff"}
    assert session.requests[3].kwargs["headers"]["Authorization"] == "JWT aaa.bbb.ccc"
    assert refresh_response.release_calls == 1


@pytest.mark.asyncio
async def test_refresh_accepts_unchanged_tokens_and_flat_schema_only() -> None:
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(403, {}),
        json_response(200, valid_refresh()),
        json_response(200, [shared_item(1302)]),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    result = await client.async_login_and_discover()

    assert len(result) == 1
    assert session.requests[2].kwargs["json"] == {"token": "ddd.eee.fff"}


@pytest.mark.asyncio
async def test_invalid_refresh_clears_tokens_and_next_update_fresh_logs_in() -> None:
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, [shared_item(1303)]),
        json_response(401, {}),
        json_response(
            200,
            {
                "access": "new.access.token",
                "refresh": "new.refresh.token",
                "exp": True,
            },
        ),
        json_response(
            200,
            valid_login("fresh.access.token", "fresh.refresh.token"),
        ),
        json_response(200, [shared_item(1303)]),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)
    await client.async_login_and_discover()

    with pytest.raises(UfanetProtocolError, match=r"^Invalid refresh response\.$"):
        await client.async_update_inventory()
    await client.async_update_inventory()

    assert session.requests[-2].url == f"{BASE_URL}{AUTH_PATH}"
    assert session.requests[-1].kwargs["headers"]["Authorization"] == (
        "JWT fresh.access.token"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh_failure", "raised_type"),
    [
        (TimeoutError("private-refresh-timeout"), UfanetConnectionError),
        (OSError("private-refresh-disconnect"), UfanetConnectionError),
        (asyncio.CancelledError("private-refresh-cancel"), asyncio.CancelledError),
    ],
)
async def test_ambiguous_refresh_clears_state_before_next_fresh_login(
    refresh_failure: BaseException, raised_type: type[BaseException]
) -> None:
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, [shared_item(1305)]),
        json_response(401, {}),
        refresh_failure,
        json_response(
            200,
            valid_login("fresh.access.token", "fresh.refresh.token"),
        ),
        json_response(200, [shared_item(1305)]),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)
    await client.async_login_and_discover()

    with pytest.raises(raised_type):
        await client.async_update_inventory()
    await client.async_update_inventory()

    assert [request.url for request in session.requests[-2:]] == [
        f"{BASE_URL}{AUTH_PATH}",
        f"{BASE_URL}{DISCOVERY_PATH}",
    ]
    assert session.requests[-1].kwargs["headers"]["Authorization"] == (
        "JWT fresh.access.token"
    )


@pytest.mark.asyncio
async def test_second_inventory_401_does_not_loop() -> None:
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(401, {}),
        json_response(200, valid_refresh("new.access.token", "new.refresh.token")),
        json_response(403, {}),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    with pytest.raises(UfanetAuthenticationError, match=r"^Authentication failed\.$"):
        await client.async_login_and_discover()

    assert len(session.requests) == 4
    assert [request.method for request in session.requests] == [
        "POST",
        "GET",
        "POST",
        "GET",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"token": valid_refresh()},
        {"access": "aaa.bbb.ccc", "refresh": "ddd.eee.fff"},
        {
            "access": "aaa.bbb.ccc",
            "refresh": "ddd.eee.fff",
            "exp": True,
        },
        {
            "access": "aaa.bbb.ccc",
            "refresh": "ddd.eee.fff",
            "exp": 1,
        },
        {
            "access": "aaa.bbb.ccc",
            "refresh": "ddd.eee.fff",
            "exp": 2**63,
        },
        {
            "access": "aaa.bbb.ccc",
            "refresh": "ddd.eee.fff",
            "exp": future_exp(),
            "extra": 1,
        },
    ],
)
async def test_refresh_rejects_non_exact_flat_schema(payload: Any) -> None:
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, [shared_item(1304)]),
        json_response(401, {}),
        json_response(200, payload),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)
    await client.async_login_and_discover()

    with pytest.raises(UfanetProtocolError, match=r"^Invalid refresh response\.$"):
        await client.async_update_inventory()


@pytest.mark.asyncio
async def test_dynamic_four_item_discovery_is_immutable_opaque_and_private() -> None:
    inventory = four_item_inventory()
    client, read_session, _ = await logged_in_client(inventory)

    doors = client.doors

    assert not hasattr(client, "discovered_doors")
    assert not hasattr(client, "validated_targets")
    assert isinstance(doors, Mapping)
    assert len(doors) == 4
    assert tuple(door.display_name for door in doors.values()) == (
        "Lobby",
        "Side door",
        "synthetic-private-address",
        "synthetic-private-address",
    )
    assert tuple(door.openable for door in doors.values()) == (True, True, False, False)
    assert all(door.trusted for door in doors.values())
    assert len(set(doors)) == 4
    for key, door in doors.items():
        assert key == door.key
        assert str(door.shared_id) not in key
        assert door.door == 0
        assert door.unique_id.endswith(key)
        assert door.suggested_object_id.endswith(key)
        assert door.display_name not in repr(door)
        assert str(door.shared_id) not in repr(door)
        assert door.binding not in repr(door)
    with pytest.raises(TypeError):
        doors["arbitrary"] = next(iter(doors.values()))  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        next(iter(doors.values())).openable = False  # type: ignore[misc]

    rendered = repr(doors) + repr(client)
    for private in (
        "synthetic-camera",
        "synthetic-private-address",
        "synthetic-role",
        "Lobby",
        "Side door",
        "1101",
        "1102",
        "1103",
        "1104",
    ):
        assert private not in rendered
    assert read_session.requests[1].url == f"{BASE_URL}{DISCOVERY_PATH}"


@pytest.mark.asyncio
async def test_stable_keys_and_command_identity_ignore_name_and_openable() -> None:
    first_inventory = [shared_item(1401, model=44, custom_name="First")]
    second_inventory = [
        shared_item(1401, model=44, custom_name="Second", disable_button=True)
    ]
    client, _, _ = await logged_in_client(
        first_inventory,
        read_after=(json_response(200, second_inventory),),
    )
    first = next(iter(client.doors.values()))

    await client.async_update_inventory()
    second = next(iter(client.doors.values()))

    assert first.key == second.key
    assert first == second
    assert first.matches_command_identity(second)
    different_model = DiscoveredDoor(
        key=first.key,
        shared_id=first.shared_id,
        door=first.door,
        model=first.model + 1,
        display_name=first.display_name,
        binding=first.binding,
        openable=first.openable,
        trusted=first.trusted,
    )
    assert first != different_model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inventory",
    [
        "not-a-list",
        [None],
        [shared_item(True)],
        [shared_item(1501, model=True)],
        [shared_item(1501, disable_button=0)],
        [shared_item(1501, is_blocked=0)],
        [shared_item(1501, role={"id": True})],
        [shared_item(1501, open_type=1)],
        [shared_item(1501, cctv_number=1)],
        [shared_item(1501, relays="")],
        [shared_item(1501), shared_item(1501, model=22)],
        [shared_item(0)],
        [shared_item(1501, model=0)],
        [shared_item(2**63)],
        [shared_item(1501, model=2**63)],
    ],
)
async def test_discovery_rejects_malformed_duplicate_and_bool_as_int(
    inventory: Any,
) -> None:
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    with pytest.raises(
        UfanetDiscoveryError, match=r"^No supported intercoms found\.$"
    ) as raised:
        await client.async_login_and_discover()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert len(client.doors) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["house", "contract"])
async def test_discovery_binding_distinguishes_missing_from_present_null(
    missing_field: str,
) -> None:
    present = shared_item(1502, house=None, contract=None)
    missing = dict(present)
    del missing[missing_field]
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, [present]),
        json_response(200, [missing]),
    )
    client = UfanetClient(
        read_session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
    )

    first = next(iter((await client.async_login_and_discover()).values()))
    second = next(iter((await client.async_update_inventory()).values()))

    assert first.key == second.key
    assert first.binding != second.binding
    assert not first.matches_command_identity(second)


@pytest.mark.asyncio
async def test_binding_presence_change_fails_preflight_before_physical_io() -> None:
    initial = shared_item(1503, house=None, contract=None)
    changed = dict(initial)
    del changed["house"]
    client, _, open_session = await logged_in_client(
        [initial],
        read_after=(json_response(200, [changed]),),
    )

    with pytest.raises(UfanetOpenError, match=r"^Entrance preflight failed\.$"):
        await client.async_open(next(iter(client.doors)))

    assert open_session.requests == []


@pytest.mark.asyncio
async def test_discovery_rejects_oversized_list() -> None:
    inventory = [shared_item(1600 + index) for index in range(MAX_DISCOVERY_ITEMS + 1)]
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    with pytest.raises(UfanetDiscoveryError):
        await client.async_login_and_discover()


@pytest.mark.asyncio
async def test_discovery_rejects_duplicate_json_keys_nan_and_oversized_body() -> None:
    bodies = (
        b'[{"id":1701,"id":1702}]',
        b'[{"id":NaN}]',
        b"[" + b" " * (256 * 1024 + 1),
    )
    for body in bodies:
        session = FakeSession(
            json_response(200, valid_login()), FakeResponse(200, body)
        )
        client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)
        with pytest.raises(UfanetDiscoveryError):
            await client.async_login_and_discover()


@pytest.mark.asyncio
async def test_call_history_get_is_strict_bounded_and_read_only() -> None:
    history = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [
            {
                "uuid": "synthetic-history-id",
                "called_at": "2026-08-13T12:00:00Z",
                "camera_number": "synthetic-camera",
                "house_id": 7001,
                "private": "discarded",
            }
        ],
    }
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, [shared_item(1901)]),
        json_response(200, history),
    )
    client = UfanetClient(
        session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        trusted_bindings=trusted_bindings_for([shared_item(1901)]),
    )
    await client.async_login_and_discover()
    page = await client.async_call_history()
    assert len(page.rows) == 1
    request = session.requests[-1]
    assert request.method == "GET"
    assert request.url.endswith("/api/v1/skuds/call-history/?page=1&page_size=10")
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["headers"]["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_short_chunk_stream_cannot_hide_oversized_inventory() -> None:
    body = b"[" + b" " * (256 * 1024 + 100)
    response = FakeResponse(200, body)
    response.content = FakeContent(body, chunk_size=100)
    session = FakeSession(json_response(200, valid_login()), response)
    client = UfanetClient(
        session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
    )

    with pytest.raises(UfanetDiscoveryError):
        await client.async_login_and_discover()

    assert len(response.content.read_limits) > 1
    assert response.content._offset == 256 * 1024 + 1


@pytest.mark.asyncio
async def test_unsupported_families_and_nonempty_relays_are_skipped() -> None:
    inventory = [
        shared_item(1801, role={"id": 1}),
        shared_item(1802, open_type="relay"),
        shared_item(1803, relays=[{"id": 1}]),
        shared_item(1804, custom_name="Supported"),
    ]
    client, _, _ = await logged_in_client(inventory)

    assert len(client.doors) == 1
    assert next(iter(client.doors.values())).shared_id == 1804


@pytest.mark.asyncio
async def test_duplicate_ids_are_all_quarantined_but_unique_candidate_survives() -> (
    None
):
    inventory = [
        shared_item(1811, model=30),
        shared_item(1811, model=31),
        shared_item(1812, model=32),
    ]
    client, _, _ = await logged_in_client(inventory)

    assert [door.shared_id for door in client.doors.values()] == [1812]


@pytest.mark.asyncio
async def test_duplicate_only_inventory_has_fixed_discovery_failure() -> None:
    inventory = [shared_item(1821), shared_item(1821, model=99)]
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    with pytest.raises(UfanetDiscoveryError, match=r"^No supported intercoms found\.$"):
        await client.async_login_and_discover()


@pytest.mark.asyncio
@pytest.mark.parametrize("relay_case", ["missing", "malformed", "nonempty"])
async def test_missing_malformed_and_nonempty_relays_are_skipped(
    relay_case: str,
) -> None:
    unsupported = shared_item(1831)
    if relay_case == "missing":
        unsupported.pop("relays")
    elif relay_case == "malformed":
        unsupported["relays"] = {"malformed": True}
    else:
        unsupported["relays"] = [0]
    inventory = [unsupported, shared_item(1832)]

    client, _, _ = await logged_in_client(inventory)

    assert [door.shared_id for door in client.doors.values()] == [1832]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("house", "7001"),
        ("house", True),
        ("contract", "8001"),
        ("contract", False),
        ("disable_button", 0),
        ("is_blocked", 0),
        ("cctv_number", None),
    ],
)
async def test_malformed_identity_or_security_candidate_is_skipped(
    field: str, value: Any
) -> None:
    inventory = [shared_item(1841, **{field: value}), shared_item(1842)]

    client, _, _ = await logged_in_client(inventory)

    assert [door.shared_id for door in client.doors.values()] == [1842]


@pytest.mark.asyncio
async def test_disabled_blocked_and_empty_cctv_are_visible_but_not_openable() -> None:
    inventory = [
        shared_item(1901, disable_button=True),
        shared_item(1902, is_blocked=True),
        shared_item(1903, cctv_number=""),
    ]
    client, _, _ = await logged_in_client(inventory)

    assert len(client.doors) == 3
    assert all(not door.openable for door in client.doors.values())


@pytest.mark.asyncio
async def test_empty_supported_inventory_fails() -> None:
    inventory = [
        shared_item(1951, role={"id": 9}),
        shared_item(1952, open_type="unsupported"),
        shared_item(1953, relays=[1]),
    ]
    session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    client = UfanetClient(session, "c", runtime_secret(), identity_key=IDENTITY_KEY)

    with pytest.raises(UfanetDiscoveryError, match=r"^No supported intercoms found\.$"):
        await client.async_login_and_discover()


@pytest.mark.asyncio
async def test_open_uses_exact_one_synthetic_url_from_opaque_key() -> None:
    inventory = [shared_item(2001, model=51)]
    response = json_response(200, {"result": True})
    client, _, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(response,),
    )
    key = key_for_shared_id(client.doors, 2001)

    await client.async_open(key)

    assert len(open_session.requests) == 1
    request = open_session.requests[0]
    assert (request.method, request.url) == (
        "GET",
        f"{BASE_URL}/api/v0/skud/shared/2001/open/?door=0",
    )
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["headers"]["Authorization"] == "JWT aaa.bbb.ccc"
    assert response.release_calls == 1
    assert client.doors[key].trusted is True
    assert client.last_physical_outcome == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_case", ["absent", "mismatch"])
async def test_absent_or_mismatched_binding_rejects_before_preflight_and_open(
    trust_case: str,
) -> None:
    inventory = [shared_item(2051)]
    key = discovered_door_key(IDENTITY_KEY, 2051, 0)
    trusted_bindings = None if trust_case == "absent" else {key: "0" * 64}
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    open_session = FakeSession(json_response(200, {"result": True}))
    client = UfanetClient(
        read_session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        trusted_bindings=trusted_bindings,
        open_session=open_session,
    )
    await client.async_login_and_discover()
    read_count = len(read_session.requests)

    assert client.doors[key].openable is True
    assert client.doors[key].trusted is False
    with pytest.raises(UfanetOpenError, match=r"^Entrance is not available\.$"):
        await client.async_open(key)

    assert len(read_session.requests) == read_count
    assert open_session.requests == []


@pytest.mark.asyncio
async def test_arbitrary_or_nonopenable_key_fails_before_any_preflight_or_open() -> (
    None
):
    inventory = [shared_item(2101), shared_item(2102, is_blocked=True)]
    client, read_session, open_session = await logged_in_client(inventory)
    blocked_key = key_for_shared_id(client.doors, 2102)
    request_count = len(read_session.requests)

    for key in ("arbitrary", blocked_key):
        with pytest.raises(UfanetOpenError, match=r"^Entrance is not available\.$"):
            await client.async_open(key)

    assert len(read_session.requests) == request_count
    assert open_session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "model", "binding", "blocked"])
async def test_fresh_preflight_change_prevents_physical_request(change: str) -> None:
    initial = [shared_item(2201, model=61), shared_item(2202, model=62)]
    if change == "missing":
        fresh = [shared_item(2202, model=62)]
    elif change == "model":
        fresh = [shared_item(2201, model=99), shared_item(2202, model=62)]
    elif change == "binding":
        fresh = [
            shared_item(2201, model=61, cctv_number="different-camera"),
            shared_item(2202, model=62),
        ]
    else:
        fresh = [
            shared_item(2201, model=61, is_blocked=True),
            shared_item(2202, model=62),
        ]
    client, _, open_session = await logged_in_client(
        initial,
        read_after=(json_response(200, fresh),),
    )
    key = key_for_shared_id(client.doors, 2201)

    with pytest.raises(UfanetOpenError, match=r"^Entrance preflight failed\.$"):
        await client.async_open(key)

    assert open_session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("private-timeout"),
        OSError("private-transport"),
    ],
)
async def test_physical_timeout_or_transport_is_unknown_and_never_retried(
    outcome: BaseException,
) -> None:
    inventory = [shared_item(2301)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(outcome,),
    )
    key = next(iter(client.doors))

    with pytest.raises(UfanetOpenUnknownOutcome) as raised:
        await client.async_open(key)

    assert str(raised.value) == "Opening outcome is unknown; do not retry."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert client.last_physical_outcome == "unknown"
    assert len(open_session.requests) == 1
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await client.async_open(key)

    assert len(read_session.requests) == read_count
    assert len(open_session.requests) == 1


@pytest.mark.asyncio
async def test_live_physical_task_cancellation_propagates_with_unknown_outcome() -> (
    None
):
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_response() -> FakeResponse:
        started.set()
        await never.wait()
        raise AssertionError("unreachable")

    inventory = [shared_item(2351)]
    client, _, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(blocked_response,),
    )
    task_outcomes: list[str | None] = []

    async def open_in_task() -> None:
        try:
            await client.async_open(next(iter(client.doors)))
        finally:
            task_outcomes.append(client.last_physical_outcome)

    opening = asyncio.create_task(open_in_task())
    await asyncio.wait_for(started.wait(), timeout=1)

    opening.cancel("private-cancellation-detail")
    with pytest.raises(asyncio.CancelledError) as raised:
        await opening

    assert str(raised.value) == ""
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert task_outcomes == ["unknown"]
    assert client.last_physical_outcome is None
    assert len(open_session.requests) == 1


@pytest.mark.asyncio
async def test_physical_cancellation_preserves_exception_identity() -> None:
    cancellation = asyncio.CancelledError("private-cancellation-detail")
    cancellation.add_note("private-cancellation-note")
    inventory = [shared_item(2352)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(cancellation,),
    )
    key = next(iter(client.doors))

    with pytest.raises(asyncio.CancelledError) as raised:
        await client.async_open(key)

    assert raised.value is cancellation
    assert str(raised.value) == ""
    assert getattr(raised.value, "__notes__", []) == []
    assert client.last_physical_outcome == "unknown"
    assert len(open_session.requests) == 1
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await client.async_open(key)

    assert len(read_session.requests) == read_count
    assert len(open_session.requests) == 1


@pytest.mark.asyncio
async def test_preflight_cancellation_clears_prior_unknown_before_operation_lock() -> (
    None
):
    read_started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_inventory() -> FakeResponse:
        read_started.set()
        await never.wait()
        raise AssertionError("unreachable")

    inventory = [shared_item(2353)]
    client, _, _ = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory), blocked_inventory),
        open_after=(TimeoutError("private-timeout"),),
    )
    key = next(iter(client.doors))
    with pytest.raises(UfanetOpenUnknownOutcome):
        await client.async_open(key)
    assert client.last_physical_outcome == "unknown"

    gate = _current_open_gate()
    assert gate.last_attempt is not None
    gate.last_attempt -= OPEN_COOLDOWN_SECONDS
    updating = asyncio.create_task(client.async_update_inventory())
    await asyncio.wait_for(read_started.wait(), timeout=1)

    task_outcomes: list[str | None] = []

    async def open_in_task() -> None:
        try:
            await client.async_open(key)
        finally:
            task_outcomes.append(client.last_physical_outcome)

    opening = asyncio.create_task(open_in_task())
    await asyncio.sleep(0)
    opening.cancel("private-preflight-cancellation")
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert task_outcomes == [None]
    assert client.last_physical_outcome == "unknown"
    updating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await updating


@pytest.mark.asyncio
async def test_physical_outcome_is_isolated_between_concurrent_tasks() -> None:
    inspect_task_outcome = asyncio.Event()
    task_outcome_inspected = asyncio.Event()
    inventory = [shared_item(2371), shared_item(2372)]
    client, _, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(json_response(500, {"result": False}),),
    )
    first_key, second_key = client.doors

    async def transmit_in_task() -> None:
        with pytest.raises(UfanetOpenError, match=r"^Opening was not confirmed\.$"):
            await client.async_open(second_key)
        assert client.last_physical_outcome == "not_confirmed"
        inspect_task_outcome.set()
        await task_outcome_inspected.wait()
        assert client.last_physical_outcome == "not_confirmed"

    transmitted = asyncio.create_task(transmit_in_task())
    await asyncio.wait_for(inspect_task_outcome.wait(), timeout=1)

    assert client.last_physical_outcome is None
    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await client.async_open(first_key)
    assert client.last_physical_outcome is None

    task_outcome_inspected.set()
    await transmitted
    assert len(open_session.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_second_open_is_rejected_immediately() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_success() -> FakeResponse:
        started.set()
        await release.wait()
        return json_response(200, {"result": True})

    inventory = [shared_item(2401), shared_item(2402)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(blocked_success,),
    )
    first_key, second_key = client.doors
    first = asyncio.create_task(client.async_open(first_key))
    await asyncio.wait_for(started.wait(), timeout=1)
    read_count = len(read_session.requests)

    with pytest.raises(
        UfanetConcurrentOpenError,
        match=r"^Another opening command is in progress\.$",
    ):
        await client.async_open(second_key)

    assert len(read_session.requests) == read_count
    assert len(open_session.requests) == 1
    release.set()
    await first


@pytest.mark.asyncio
async def test_global_gate_and_cooldown_span_distinct_clients() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_success() -> FakeResponse:
        started.set()
        await release.wait()
        return json_response(200, {"result": True})

    inventory = [shared_item(2451)]
    first, _, first_open = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(blocked_success,),
    )
    second, second_read, second_open = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(json_response(200, {"result": True}),),
    )
    key = next(iter(first.doors))
    opening = asyncio.create_task(first.async_open(key))
    await asyncio.wait_for(started.wait(), timeout=1)
    second_read_count = len(second_read.requests)

    with pytest.raises(
        UfanetConcurrentOpenError,
        match=r"^Another opening command is in progress\.$",
    ):
        await second.async_open(key)
    assert len(second_read.requests) == second_read_count
    assert second_open.requests == []

    release.set()
    await opening
    assert len(first_open.requests) == 1

    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await second.async_open(key)
    assert len(second_read.requests) == second_read_count

    gate = _current_open_gate()
    assert gate.last_attempt is not None
    gate.last_attempt -= OPEN_COOLDOWN_SECONDS
    await second.async_open(key)
    assert len(second_open.requests) == 1


@pytest.mark.asyncio
async def test_update_cannot_mutate_auth_or_inventory_during_preflight_to_open() -> (
    None
):
    physical_started = asyncio.Event()
    release_physical = asyncio.Event()

    async def blocked_success() -> FakeResponse:
        physical_started.set()
        await release_physical.wait()
        return json_response(200, {"result": True})

    inventory = [shared_item(2501)]
    refreshed_inventory = [shared_item(2501, custom_name="Updated")]
    client, read_session, _ = await logged_in_client(
        inventory,
        read_after=(
            json_response(200, inventory),
            json_response(401, {}),
            json_response(
                200,
                valid_refresh("rotated.access.token", "rotated.refresh.token"),
            ),
            json_response(200, refreshed_inventory),
        ),
        open_after=(blocked_success,),
    )
    key = next(iter(client.doors))
    opening = asyncio.create_task(client.async_open(key))
    await asyncio.wait_for(physical_started.wait(), timeout=1)

    update = asyncio.create_task(client.async_update_inventory())
    await asyncio.sleep(0)
    assert len(read_session.requests) == 3

    release_physical.set()
    await opening
    updated = await update
    assert next(iter(updated.values())).display_name == "Updated"
    assert [(request.method, request.url) for request in read_session.requests[3:]] == [
        ("GET", f"{BASE_URL}{DISCOVERY_PATH}"),
        ("POST", f"{BASE_URL}{REFRESH_PATH}"),
        ("GET", f"{BASE_URL}{DISCOVERY_PATH}"),
    ]


@pytest.mark.asyncio
async def test_mutated_read_transport_is_rejected_before_next_request() -> None:
    inventory = [shared_item(2581)]
    client, read_session, _ = await logged_in_client(inventory)
    request_count = len(read_session.requests)
    read_session._retry_connection = True

    with pytest.raises(
        UfanetConnectionError,
        match=r"^Read transport is not safe\.$",
    ):
        await client.async_update_inventory()

    assert len(read_session.requests) == request_count


def test_same_read_and_physical_session_is_rejected() -> None:
    session = FakeSession()
    with pytest.raises(
        ValueError,
        match=r"^Physical transport must be separate\.$",
    ):
        UfanetClient(
            session,
            "c",
            runtime_secret(),
            identity_key=IDENTITY_KEY,
            open_session=session,
        )


@pytest.mark.parametrize(
    "read_session",
    [
        FakeSession(retry_connection=True),
        FakeSession(middlewares=(object(),)),
        FakeSession(default_headers={"Authorization": "JWT hostile.default.token"}),
        MissingRetrySession(),
        MissingMiddlewaresSession(),
        MissingDefaultHeadersSession(),
        MissingConnectorSession(),
    ],
    ids=[
        "implicit-retry",
        "middleware",
        "default-authorization",
        "missing-retry-proof",
        "missing-middleware-proof",
        "missing-header-proof",
        "missing-connector-proof",
    ],
)
def test_read_transport_must_prove_no_implicit_retry(
    read_session: FakeSession,
) -> None:
    with pytest.raises(ValueError, match=r"^Read transport is not safe\.$"):
        UfanetClient(
            read_session,
            "c",
            runtime_secret(),
            identity_key=IDENTITY_KEY,
        )


@pytest.mark.asyncio
async def test_missing_dedicated_open_session_fails_before_preflight() -> None:
    inventory = [shared_item(2591)]
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    client = UfanetClient(
        read_session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        trusted_bindings=trusted_bindings_for(inventory),
    )
    await client.async_login_and_discover()
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening transport is not safe\.$"):
        await client.async_open(next(iter(client.doors)))

    assert len(read_session.requests) == read_count


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared-connector", "delegating-wrapper"])
async def test_physical_transport_must_prove_distinct_concrete_session(
    mode: str,
) -> None:
    inventory = [shared_item(2595)]
    shared_connector = object()
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
        connector=shared_connector,
    )
    if mode == "shared-connector":
        open_session: FakeSession = FakeSession(connector=shared_connector)
    else:
        open_session = DelegatingSession()
    client = UfanetClient(
        read_session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        open_session=open_session,
        trusted_bindings=trusted_bindings_for(inventory),
    )
    await client.async_login_and_discover()
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening transport is not safe\.$"):
        await client.async_open(next(iter(client.doors)))

    assert len(read_session.requests) == read_count
    assert open_session.requests == []


@pytest.mark.asyncio
async def test_physical_transport_rejects_same_class_protocol_lookalikes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production validation must require exact aiohttp concrete types."""

    monkeypatch.setattr(api_module, "ClientSession", AiohttpClientSession)
    monkeypatch.setattr(api_module, "TCPConnector", AiohttpTCPConnector)
    inventory = [shared_item(2596)]
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
    )
    open_session = FakeSession(json_response(200, {"result": True}))
    client = UfanetClient(
        read_session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        open_session=open_session,
        trusted_bindings=trusted_bindings_for(inventory),
    )
    await client.async_login_and_discover()
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening transport is not safe\.$"):
        await client.async_open(next(iter(client.doors)))

    assert len(read_session.requests) == read_count
    assert open_session.requests == []


@pytest.mark.asyncio
async def test_physical_transport_rejects_wrong_connector_types() -> None:
    inventory = [shared_item(2597)]
    read_session = FakeSession(
        json_response(200, valid_login()),
        json_response(200, inventory),
        connector=object(),
    )
    open_session = FakeSession(
        json_response(200, {"result": True}),
        connector=object(),
    )
    client = UfanetClient(
        read_session,
        "c",
        runtime_secret(),
        identity_key=IDENTITY_KEY,
        open_session=open_session,
        trusted_bindings=trusted_bindings_for(inventory),
    )
    await client.async_login_and_discover()
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening transport is not safe\.$"):
        await client.async_open(next(iter(client.doors)))

    assert len(read_session.requests) == read_count
    assert open_session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["true", "missing"])
async def test_unsafe_or_missing_retry_flag_fails_before_preflight(unsafe: str) -> None:
    inventory = [shared_item(2601)]
    open_session: FakeSession
    if unsafe == "true":
        open_session = FakeSession(retry_connection=True)
    else:
        open_session = MissingRetrySession()
    client, read_session, _ = await logged_in_client(
        inventory,
        open_session=open_session,
    )
    key = next(iter(client.doors))
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening transport is not safe\.$"):
        await client.async_open(key)

    assert len(read_session.requests) == read_count
    assert open_session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "open_session",
    [
        FakeSession(middlewares=(object(),)),
        MissingMiddlewaresSession(),
        FakeSession(default_headers={"Authorization": "JWT hostile.default.token"}),
        MissingDefaultHeadersSession(),
    ],
    ids=[
        "nonempty-middleware",
        "missing-middleware",
        "default-authorization",
        "missing-default-headers",
    ],
)
async def test_middleware_or_default_auth_ambiguity_fails_before_preflight(
    open_session: FakeSession,
) -> None:
    inventory = [shared_item(2651)]
    client, read_session, _ = await logged_in_client(
        inventory,
        open_session=open_session,
    )
    read_count = len(read_session.requests)

    with pytest.raises(UfanetOpenError, match=r"^Opening transport is not safe\.$"):
        await client.async_open(next(iter(client.doors)))

    assert len(read_session.requests) == read_count
    assert open_session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        json_response(201, {"result": True}),
        json_response(200, {"result": False}),
        json_response(200, {"result": 1}),
        json_response(200, {"result": True, "extra": 1}),
        FakeResponse(200, b'{"result":true,"result":true}'),
        FakeResponse(200, b'{"result":NaN}'),
        FakeResponse(200, b"not-json"),
        FakeResponse(200, b'{"result":true}', content_type="text/plain"),
    ],
)
async def test_open_requires_exact_200_exact_json_success(
    response: FakeResponse,
) -> None:
    inventory = [shared_item(2701)]
    client, _, _ = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(response,),
    )
    key = next(iter(client.doors))

    with pytest.raises(UfanetOpenError, match=r"^Opening was not confirmed\.$"):
        await client.async_open(key)

    assert client.last_physical_outcome == "not_confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 302])
async def test_physical_401_or_redirect_is_one_request_without_refresh_or_retry(
    status: int,
) -> None:
    inventory = [shared_item(2751)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(json_response(status, {"detail": "private-provider-detail"}),),
    )
    key = next(iter(client.doors))

    with pytest.raises(UfanetOpenError, match=r"^Opening was not confirmed\.$"):
        await client.async_open(key)

    assert len(open_session.requests) == 1
    assert client.last_physical_outcome == "not_confirmed"
    read_urls = [request.url for request in read_session.requests]
    assert read_urls == [
        f"{BASE_URL}{AUTH_PATH}",
        f"{BASE_URL}{DISCOVERY_PATH}",
        f"{BASE_URL}{DISCOVERY_PATH}",
    ]

    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await client.async_open(key)

    assert [request.url for request in read_session.requests] == read_urls
    assert len(open_session.requests) == 1


@pytest.mark.asyncio
async def test_hostile_transport_exception_is_sanitized_and_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile = runtime_secret()
    exception = OSError(
        f"GET https://private.invalid/{hostile} "
        f"Authorization: JWT {hostile} body={hostile}"
    )
    inventory = [shared_item(2761)]
    client, _, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(exception,),
    )

    with pytest.raises(UfanetOpenUnknownOutcome) as raised:
        await client.async_open(next(iter(client.doors)))

    assert str(raised.value) == "Opening outcome is unknown; do not retry."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert hostile not in str(raised.value)
    assert hostile not in repr(raised.value)
    assert len(open_session.requests) == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.asyncio
async def test_cooldown_rejects_replay_until_deterministic_time_advance() -> None:
    assert OPEN_COOLDOWN_SECONDS >= 3
    inventory = [shared_item(2771)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(
            json_response(200, inventory),
            json_response(200, inventory),
        ),
        open_after=(
            json_response(200, {"result": True}),
            json_response(200, {"result": True}),
        ),
    )
    key = next(iter(client.doors))

    await client.async_open(key)
    read_count = len(read_session.requests)
    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await client.async_open(key)
    assert len(read_session.requests) == read_count
    assert len(open_session.requests) == 1

    gate = _current_open_gate()
    assert gate.last_attempt is not None
    gate.last_attempt -= OPEN_COOLDOWN_SECONDS
    await client.async_open(key)
    assert len(open_session.requests) == 2


@pytest.mark.asyncio
async def test_cooldown_starts_after_slow_physical_attempt_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "OPEN_COOLDOWN_SECONDS", 0.01)

    async def slow_success() -> FakeResponse:
        await asyncio.sleep(0.03)
        return json_response(200, {"result": True})

    inventory = [shared_item(2772)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(json_response(200, inventory),),
        open_after=(slow_success, json_response(200, {"result": True})),
    )
    key = next(iter(client.doors))

    await client.async_open(key)
    read_count = len(read_session.requests)
    with pytest.raises(UfanetOpenError, match=r"^Opening cooldown is active\.$"):
        await client.async_open(key)

    assert len(read_session.requests) == read_count
    assert len(open_session.requests) == 1


@pytest.mark.asyncio
async def test_closing_rejects_and_drain_waits_for_inflight_operation() -> None:
    physical_started = asyncio.Event()
    release_physical = asyncio.Event()

    async def blocked_success() -> FakeResponse:
        physical_started.set()
        await release_physical.wait()
        return json_response(200, {"result": True})

    inventory = [shared_item(2781)]
    client, read_session, open_session = await logged_in_client(
        inventory,
        read_after=(
            json_response(200, inventory),
            json_response(200, inventory),
        ),
        open_after=(blocked_success, json_response(200, {"result": True})),
    )
    key = next(iter(client.doors))
    opening = asyncio.create_task(client.async_open(key))
    await asyncio.wait_for(physical_started.wait(), timeout=1)

    client.begin_close()
    read_count = len(read_session.requests)
    with pytest.raises(UfanetOpenError, match=r"^Client is closing\.$"):
        await client.async_open(key)
    assert len(read_session.requests) == read_count
    assert len(open_session.requests) == 1

    draining = asyncio.create_task(client.async_drain())
    await asyncio.sleep(0)
    assert not draining.done()
    release_physical.set()
    await opening
    await draining

    client.cancel_close()
    gate = _current_open_gate()
    assert gate.last_attempt is not None
    gate.last_attempt -= OPEN_COOLDOWN_SECONDS
    await client.async_open(key)
    assert len(open_session.requests) == 2
