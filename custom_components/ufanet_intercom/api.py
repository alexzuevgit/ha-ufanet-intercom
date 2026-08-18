"""Strict generic asynchronous client for Ufanet shared intercoms.

This pure module owns authentication, bounded read-only discovery, and the single
no-retry physical command. It never logs provider data, credentials, or tokens.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Final, Protocol
from weakref import WeakKeyDictionary

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from .const import (
    AUTH_PATH,
    BASE_URL,
    DISCOVERY_PATH,
    DOOR_SELECTOR,
    IDENTITY_KEY_BYTES,
    MAX_CCTV_NUMBER_CHARS,
    MAX_CONTRACT_CHARS,
    MAX_PROVIDER_INTEGER,
    REFRESH_PATH,
    USER_AGENT,
    DiscoveredDoor,
    discovered_door_binding,
    discovered_door_key,
)
from .history import (
    HISTORY_PATH,
    MAX_HISTORY_RESPONSE_BYTES,
    CallHistoryPage,
    HistoryProtocolError,
    parse_call_history,
)

MAX_AUTH_RESPONSE_BYTES: Final = 32 * 1024
MAX_DISCOVERY_RESPONSE_BYTES: Final = 256 * 1024
MAX_OPEN_RESPONSE_BYTES: Final = 8 * 1024
MAX_PASSWORD_CHARS: Final = 512
MAX_TOKEN_CHARS: Final = 8192
MAX_DISCOVERY_ITEMS: Final = 256
MAX_DISPLAY_NAME_CHARS: Final = 128
OPEN_COOLDOWN_SECONDS: Final = 3.0

_READ_TIMEOUT = ClientTimeout(total=15, connect=5, sock_read=10)
_OPEN_TIMEOUT = ClientTimeout(total=8, connect=3, sock_read=5)
_JSON_CONTENT_TYPE: Final = "application/json"
_JWT_RE: Final = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_REFRESH_MARGIN_SECONDS: Final = 30
_EMPTY_DOORS: Mapping[str, DiscoveredDoor] = MappingProxyType({})
_MAX_JSON_READ_CHUNKS: Final = 4096


class _GlobalOpenGate:
    __slots__ = ("last_attempt", "lock")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_attempt: float | None = None


_GLOBAL_OPEN_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, _GlobalOpenGate] = (
    WeakKeyDictionary()
)


def _current_open_gate() -> _GlobalOpenGate:
    loop = asyncio.get_running_loop()
    gate = _GLOBAL_OPEN_GATES.get(loop)
    if gate is None:
        gate = _GlobalOpenGate()
        _GLOBAL_OPEN_GATES[loop] = gate
    return gate


class _ResponseContent(Protocol):
    async def read(self, limit: int = -1) -> bytes: ...


class _Response(Protocol):
    status: int
    headers: Mapping[str, str]
    content: _ResponseContent

    def release(self) -> None: ...


class _Session(Protocol):
    _retry_connection: bool
    _middlewares: tuple[Any, ...] | list[Any]
    headers: Mapping[str, str]
    connector: object

    async def post(self, url: str, **kwargs: Any) -> _Response: ...

    async def get(self, url: str, **kwargs: Any) -> _Response: ...


class UfanetError(Exception):
    """Base class with deliberately sanitized public messages."""


class UfanetAuthenticationError(UfanetError):
    """Credentials or refresh authorization were rejected."""


class UfanetConnectionError(UfanetError):
    """A read-only provider operation could not be completed."""


class UfanetProtocolError(UfanetError):
    """An authentication response did not satisfy its strict schema."""


class UfanetDiscoveryError(UfanetError):
    """The shared-intercom inventory was unsafe or unsupported."""


class UfanetOpenError(UfanetError):
    """No provider confirmation of an opening was received."""


class UfanetOpenUnknownOutcome(UfanetOpenError):
    """A command may have reached the provider but no outcome is known."""


class UfanetConcurrentOpenError(UfanetOpenError):
    """A second physical command was rejected before transmission."""


def _detach_exception_context(error: BaseException) -> None:
    error.__context__ = None
    error.__cause__ = None


def _sanitize_cancellation(error: asyncio.CancelledError) -> None:
    error.args = ()
    error.__notes__ = []
    _detach_exception_context(error)


class _InventoryUnauthorized(Exception):
    """Internal signal permitting one read-only refresh and retry."""


def _reject_constant(_value: str) -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_json(data: bytes) -> Any:
    text = data.decode("utf-8", errors="strict")
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _safe_release(response: _Response) -> None:
    try:
        response.release()
    except Exception:  # noqa: BLE001
        return


async def _read_bounded_json(
    response: _Response,
    max_bytes: int,
    error_factory: Callable[[], UfanetError],
) -> Any:
    content_type = response.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != _JSON_CONTENT_TYPE:
        raise error_factory()

    chunks: list[bytes] = []
    total = 0
    for _ in range(_MAX_JSON_READ_CHUNKS):
        remaining = max_bytes + 1 - total
        if remaining <= 0:
            raise error_factory()
        chunk = await response.content.read(min(64 * 1024, remaining))
        if type(chunk) is not bytes:
            raise error_factory()
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise error_factory()
        chunks.append(chunk)
    else:
        raise error_factory()

    data = b"".join(chunks)
    try:
        return _decode_json(data)
    except (UnicodeDecodeError, ValueError, RecursionError, TypeError):
        raise error_factory() from None


def _valid_nonempty_bounded_string(value: Any, limit: int) -> bool:
    return type(value) is str and 0 < len(value) <= limit


def _raw_jwt(value: Any, message: str) -> str:
    if type(value) is not str:
        raise UfanetProtocolError(message)
    raw = value.removeprefix("JWT ")
    if not raw or len(raw) > MAX_TOKEN_CHARS or _JWT_RE.fullmatch(raw) is None:
        raise UfanetProtocolError(message)
    return raw


def _parse_tokens(payload: Any, *, nested: bool) -> tuple[str, str, int]:
    message = (
        "Invalid authentication response." if nested else "Invalid refresh response."
    )
    if nested:
        if type(payload) is not dict or set(payload) != {"token"}:
            raise UfanetProtocolError(message)
        token = payload["token"]
    else:
        token = payload

    if type(token) is not dict or set(token) != {"access", "refresh", "exp"}:
        raise UfanetProtocolError(message)
    access = _raw_jwt(token["access"], message)
    refresh = _raw_jwt(token["refresh"], message)
    exp = token["exp"]
    if type(exp) is not int or exp <= int(time.time()) or exp > MAX_PROVIDER_INTEGER:
        raise UfanetProtocolError(message)
    return access, refresh, exp


def _display_name(item: Mapping[str, Any]) -> str:
    for field_name in ("custom_name", "string_view", "address"):
        value = item.get(field_name)
        if type(value) is str and len(value) <= MAX_DISPLAY_NAME_CHARS:
            stripped = value.strip()
            if stripped:
                return stripped
    return "Ufanet intercom"


def _discovery_failure() -> UfanetDiscoveryError:
    return UfanetDiscoveryError("No supported intercoms found.")


def _session_transport_is_bounded(session: object) -> bool:
    sentinel = object()
    try:
        retry_connection = getattr(session, "_retry_connection", sentinel)
        middlewares = getattr(session, "_middlewares", sentinel)
        default_headers = getattr(session, "headers", sentinel)
        connector = getattr(session, "connector", sentinel)
        if retry_connection is not False:
            return False
        if type(middlewares) not in (tuple, list):
            return False
        if middlewares:
            return False
        if not isinstance(default_headers, Mapping):
            return False
        if connector is sentinel or connector is None:
            return False
        return not any(str(name).lower() == "authorization" for name in default_headers)
    except Exception:  # noqa: BLE001
        return False


class UfanetClient:
    """Credential client exposing only opaque keys for discovered shared doors."""

    __slots__ = (
        "_access_token",
        "_closing",
        "_contract",
        "_doors",
        "_identity_key",
        "_open_session",
        "_operation_lock",
        "_password",
        "_physical_outcome",
        "_refresh_token",
        "_session",
        "_token_exp",
        "_trusted_bindings",
    )

    def __init__(
        self,
        session: _Session,
        contract: str,
        password: str,
        *,
        identity_key: bytes,
        open_session: _Session | None = None,
        trusted_bindings: Mapping[str, str] | None = None,
    ) -> None:
        if not _valid_nonempty_bounded_string(contract, MAX_CONTRACT_CHARS):
            raise UfanetAuthenticationError("Invalid credential format.")
        if not _valid_nonempty_bounded_string(password, MAX_PASSWORD_CHARS):
            raise UfanetAuthenticationError("Invalid credential format.")
        if contract != contract.strip():
            raise UfanetAuthenticationError("Invalid credential format.")
        if type(identity_key) is not bytes or len(identity_key) != IDENTITY_KEY_BYTES:
            raise ValueError("Invalid identity key.")

        if trusted_bindings is None:
            bindings: dict[str, str] = {}
        else:
            bindings_error = False
            try:
                bindings = dict(trusted_bindings)
            except Exception:  # noqa: BLE001
                bindings = {}
                bindings_error = True
            if bindings_error:
                raise ValueError("Invalid trusted bindings.")
            if any(
                type(key) is not str
                or re.fullmatch(r"[0-9a-f]{64}", key) is None
                or type(binding) is not str
                or re.fullmatch(r"[0-9a-f]{64}", binding) is None
                for key, binding in bindings.items()
            ):
                raise ValueError("Invalid trusted bindings.")
        if not _session_transport_is_bounded(session):
            raise ValueError("Read transport is not safe.")
        if open_session is session:
            raise ValueError("Physical transport must be separate.")

        self._session = session
        self._open_session = open_session
        self._contract = contract
        self._password = password
        self._identity_key = identity_key
        self._trusted_bindings: Mapping[str, str] = MappingProxyType(bindings)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_exp: int | None = None
        self._physical_outcome: ContextVar[str | None] = ContextVar(
            "ufanet_physical_outcome", default=None
        )
        self._doors: Mapping[str, DiscoveredDoor] = _EMPTY_DOORS
        self._operation_lock = asyncio.Lock()
        self._closing = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(authenticated={self._access_token is not None}, "
            f"discovered={len(self._doors)})"
        )

    @property
    def doors(self) -> Mapping[str, DiscoveredDoor]:
        """Return the latest immutable, privacy-safe-keyed inventory."""

        return self._doors

    @property
    def last_physical_outcome(self) -> str | None:
        """Return this task's fixed privacy-safe outcome for its current invocation."""

        return self._physical_outcome.get()

    def begin_close(self) -> None:
        """Synchronously reject new physical actions before session teardown."""

        self._closing = True

    async def async_drain(self) -> None:
        """Wait until the serialized auth/discovery/physical operation is idle."""

        async with self._operation_lock:
            return

    def cancel_close(self) -> None:
        """Resume actions after the owner reports that unload failed."""

        self._closing = False

    def _clear_tokens_locked(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._token_exp = None

    def _require_read_transport_safe(self) -> None:
        if not _session_transport_is_bounded(self._session):
            raise UfanetConnectionError("Read transport is not safe.")

    async def async_login(self) -> None:
        """Perform a fresh login while serializing all mutable client state."""

        try:
            async with self._operation_lock:
                await self._login_locked()
        except asyncio.CancelledError as error:
            _sanitize_cancellation(error)
            raise
        except UfanetError as error:
            _detach_exception_context(error)
            raise

    async def _login_locked(self) -> None:
        self._require_read_transport_safe()
        response: _Response | None = None
        try:
            response = await self._session.post(
                f"{BASE_URL}{AUTH_PATH}",
                json={
                    "contract": self._contract.upper(),
                    "password": self._password,
                },
                headers={
                    "Accept": _JSON_CONTENT_TYPE,
                    "User-Agent": USER_AGENT,
                },
                allow_redirects=False,
                timeout=_READ_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            raise UfanetConnectionError("Unable to contact Ufanet.") from None

        try:
            if response.status in (401, 403):
                raise UfanetAuthenticationError("Authentication failed.")
            if response.status != 200:
                raise UfanetConnectionError("Unable to contact Ufanet.")
            payload = await _read_bounded_json(
                response,
                MAX_AUTH_RESPONSE_BYTES,
                lambda: UfanetProtocolError("Invalid authentication response."),
            )
            access, refresh, exp = _parse_tokens(payload, nested=True)
        except (UfanetAuthenticationError, UfanetConnectionError, UfanetProtocolError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            raise UfanetConnectionError("Unable to contact Ufanet.") from None
        finally:
            _safe_release(response)

        # Replace the complete token tuple only after the whole response is valid.
        self._access_token, self._refresh_token, self._token_exp = (
            access,
            refresh,
            exp,
        )
        self._doors = _EMPTY_DOORS

    async def _refresh_locked(self) -> None:
        self._require_read_transport_safe()
        refresh_token = self._refresh_token
        if refresh_token is None:
            raise UfanetAuthenticationError("Authentication required.")

        response: _Response | None = None
        try:
            try:
                response = await self._session.post(
                    f"{BASE_URL}{REFRESH_PATH}",
                    json={"token": refresh_token},
                    headers={
                        "Accept": _JSON_CONTENT_TYPE,
                        "User-Agent": USER_AGENT,
                    },
                    allow_redirects=False,
                    timeout=_READ_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                raise UfanetConnectionError("Unable to contact Ufanet.") from None

            try:
                if response.status in (401, 403):
                    raise UfanetAuthenticationError("Authentication failed.")
                if response.status != 200:
                    raise UfanetConnectionError("Unable to contact Ufanet.")
                payload = await _read_bounded_json(
                    response,
                    MAX_AUTH_RESPONSE_BYTES,
                    lambda: UfanetProtocolError("Invalid refresh response."),
                )
                access, refresh, exp = _parse_tokens(payload, nested=False)
            except (
                UfanetAuthenticationError,
                UfanetConnectionError,
                UfanetProtocolError,
            ):
                raise
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                raise UfanetConnectionError("Unable to contact Ufanet.") from None
            finally:
                _safe_release(response)

            # Replace only after the complete response is valid.
            self._access_token, self._refresh_token, self._token_exp = (
                access,
                refresh,
                exp,
            )
        except asyncio.CancelledError:
            self._clear_tokens_locked()
            raise
        except (UfanetAuthenticationError, UfanetConnectionError, UfanetProtocolError):
            self._clear_tokens_locked()
            raise
        except Exception:  # noqa: BLE001
            self._clear_tokens_locked()
            raise UfanetConnectionError("Unable to contact Ufanet.") from None

    async def _ensure_auth_locked(self) -> None:
        if (
            self._access_token is None
            or self._refresh_token is None
            or self._token_exp is None
        ):
            await self._login_locked()
            return
        if self._token_exp <= int(time.time()) + _REFRESH_MARGIN_SECONDS:
            await self._refresh_locked()

    async def async_discover(self) -> Mapping[str, DiscoveredDoor]:
        """Fetch a fresh inventory using current or newly ensured authorization."""

        try:
            async with self._operation_lock:
                await self._ensure_auth_locked()
                return await self._inventory_with_retry_locked()
        except asyncio.CancelledError as error:
            _sanitize_cancellation(error)
            raise
        except UfanetError as error:
            _detach_exception_context(error)
            raise

    async def async_login_and_discover(self) -> Mapping[str, DiscoveredDoor]:
        """Perform a fresh login followed by bounded read-only inventory."""

        try:
            async with self._operation_lock:
                await self._login_locked()
                return await self._inventory_with_retry_locked()
        except asyncio.CancelledError as error:
            _sanitize_cancellation(error)
            raise
        except UfanetError as error:
            _detach_exception_context(error)
            raise

    async def async_update_inventory(self) -> Mapping[str, DiscoveredDoor]:
        """Ensure existing auth and return a fresh immutable inventory."""

        try:
            async with self._operation_lock:
                await self._ensure_auth_locked()
                return await self._inventory_with_retry_locked()
        except asyncio.CancelledError as error:
            _sanitize_cancellation(error)
            raise
        except UfanetError as error:
            _detach_exception_context(error)
            raise

    async def _inventory_with_retry_locked(self) -> Mapping[str, DiscoveredDoor]:
        try:
            doors = await self._inventory_once_locked()
        except _InventoryUnauthorized:
            await self._refresh_locked()
            try:
                doors = await self._inventory_once_locked()
            except _InventoryUnauthorized:
                raise UfanetAuthenticationError("Authentication failed.") from None
        self._doors = doors
        return doors

    async def _inventory_once_locked(self) -> Mapping[str, DiscoveredDoor]:
        self._require_read_transport_safe()
        token = self._access_token
        if token is None:
            raise UfanetAuthenticationError("Authentication required.")

        response: _Response | None = None
        try:
            response = await self._session.get(
                f"{BASE_URL}{DISCOVERY_PATH}",
                headers={
                    "Authorization": f"JWT {token}",
                    "Accept": _JSON_CONTENT_TYPE,
                    "User-Agent": USER_AGENT,
                },
                allow_redirects=False,
                timeout=_READ_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            raise UfanetConnectionError("Unable to contact Ufanet.") from None

        try:
            if response.status in (401, 403):
                raise _InventoryUnauthorized
            if response.status != 200:
                raise UfanetConnectionError("Unable to contact Ufanet.")
            payload = await _read_bounded_json(
                response,
                MAX_DISCOVERY_RESPONSE_BYTES,
                _discovery_failure,
            )
            return self._parse_discovery(payload)
        except (_InventoryUnauthorized, UfanetConnectionError, UfanetDiscoveryError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            raise UfanetConnectionError("Unable to contact Ufanet.") from None
        finally:
            _safe_release(response)

    def _parse_discovery(self, payload: Any) -> Mapping[str, DiscoveredDoor]:
        if type(payload) is not list or len(payload) > MAX_DISCOVERY_ITEMS:
            raise _discovery_failure()
        if any(type(item) is not dict for item in payload):
            raise _discovery_failure()

        id_counts = Counter(
            item.get("id")
            for item in payload
            if type(item.get("id")) is int and 0 < item["id"] <= MAX_PROVIDER_INTEGER
        )
        discovered: dict[str, DiscoveredDoor] = {}
        for item in payload:
            shared_id = item.get("id")
            model = item.get("model")
            house = item.get("house")
            contract = item.get("contract")
            disable_button = item.get("disable_button")
            is_blocked = item.get("is_blocked")
            role = item.get("role")
            open_type = item.get("open_type")
            cctv_number = item.get("cctv_number")
            relays = item.get("relays")

            # Malformed or ambiguous candidates are quarantined, never guessed.
            if (
                type(shared_id) is not int
                or not 0 < shared_id <= MAX_PROVIDER_INTEGER
                or id_counts[shared_id] != 1
                or type(model) is not int
                or not 0 < model <= MAX_PROVIDER_INTEGER
                or (
                    house is not None
                    and (
                        type(house) is not int or not 0 < house <= MAX_PROVIDER_INTEGER
                    )
                )
                or (
                    contract is not None
                    and (
                        type(contract) is not int
                        or not 0 < contract <= MAX_PROVIDER_INTEGER
                    )
                )
                or type(disable_button) is not bool
                or type(is_blocked) is not bool
                or type(role) is not dict
                or type(role.get("id")) is not int
                or type(open_type) is not str
                or type(cctv_number) is not str
                or len(cctv_number) > MAX_CCTV_NUMBER_CHARS
                or type(relays) is not list
            ):
                continue
            if role["id"] != 2 or open_type != "http" or relays != []:
                continue

            try:
                key = discovered_door_key(self._identity_key, shared_id, DOOR_SELECTOR)
                binding = discovered_door_binding(
                    self._identity_key,
                    shared_id=shared_id,
                    door=DOOR_SELECTOR,
                    model=model,
                    house=house,
                    contract=contract,
                    cctv_number=cctv_number,
                    house_present="house" in item,
                    contract_present="contract" in item,
                )
            except (TypeError, ValueError):
                continue
            if key in discovered:
                raise _discovery_failure()

            trusted_binding = self._trusted_bindings.get(key)
            trusted = type(trusted_binding) is str and hmac.compare_digest(
                trusted_binding, binding
            )
            discovered[key] = DiscoveredDoor(
                key=key,
                shared_id=shared_id,
                door=DOOR_SELECTOR,
                model=model,
                display_name=_display_name(item),
                binding=binding,
                openable=(
                    not disable_button and not is_blocked and bool(cctv_number.strip())
                ),
                trusted=trusted,
                cctv_number=cctv_number,
                house=house,
            )

        if not discovered:
            raise _discovery_failure()
        return MappingProxyType(discovered)

    async def async_call_history(self) -> CallHistoryPage:
        """Fetch one bounded first page through the read-only JWT session."""

        try:
            async with self._operation_lock:
                await self._ensure_auth_locked()
                try:
                    return await self._history_once_locked()
                except _InventoryUnauthorized:
                    await self._refresh_locked()
                    try:
                        return await self._history_once_locked()
                    except _InventoryUnauthorized:
                        raise UfanetAuthenticationError(
                            "Authentication failed."
                        ) from None
        except asyncio.CancelledError as error:
            _sanitize_cancellation(error)
            raise
        except (UfanetError, HistoryProtocolError) as error:
            _detach_exception_context(error)
            raise

    async def _history_once_locked(self) -> CallHistoryPage:
        self._require_read_transport_safe()
        token = self._access_token
        if token is None:
            raise UfanetAuthenticationError("Authentication required.")
        response: _Response | None = None
        try:
            try:
                response = await self._session.get(
                    f"{BASE_URL}{HISTORY_PATH}",
                    headers={
                        "Authorization": f"JWT {token}",
                        "Accept": _JSON_CONTENT_TYPE,
                        "User-Agent": USER_AGENT,
                    },
                    allow_redirects=False,
                    timeout=_READ_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                raise UfanetConnectionError("Unable to contact Ufanet.") from None
            if response.status in (401, 403):
                raise _InventoryUnauthorized
            if response.status != 200:
                raise UfanetConnectionError("Unable to contact Ufanet.")
            payload = await _read_bounded_json(
                response,
                MAX_HISTORY_RESPONSE_BYTES,
                lambda: UfanetProtocolError("Invalid call history response."),
            )
            try:
                return parse_call_history(payload)
            except HistoryProtocolError:
                raise UfanetProtocolError("Invalid call history response.") from None
        finally:
            if response is not None:
                _safe_release(response)

    def _open_transport_is_safe(self) -> bool:
        open_session = self._open_session
        read_session = self._session
        if (
            type(read_session) is not ClientSession
            or type(open_session) is not ClientSession
        ):
            return False
        if not _session_transport_is_bounded(open_session):
            return False
        # Sessions are code-owned. Concrete-type and connector separation reject
        # accidental wrappers/delegation and connection-pool reuse across read/write.
        try:
            read_connector = read_session.connector
            open_connector = open_session.connector
            return (
                type(read_connector) is TCPConnector
                and type(open_connector) is TCPConnector
                and open_connector is not read_connector
            )
        except Exception:  # noqa: BLE001
            return False

    async def async_open(self, target_key: str) -> None:
        """Preflight and transmit with a privacy-sanitized public error boundary."""

        self._physical_outcome.set(None)
        try:
            await self._async_open(target_key)
        except asyncio.CancelledError as error:
            _sanitize_cancellation(error)
            raise
        except UfanetError as error:
            _detach_exception_context(error)
            raise

    async def _async_open(self, target_key: str) -> None:
        """Freshly preflight one trusted target and transmit one no-retry GET."""

        if self._closing:
            raise UfanetOpenError("Client is closing.")
        target = self._doors.get(target_key) if type(target_key) is str else None
        if target is None or not target.openable or not target.trusted:
            raise UfanetOpenError("Entrance is not available.")
        gate = _current_open_gate()
        loop_time = asyncio.get_running_loop().time
        if gate.lock.locked():
            raise UfanetConcurrentOpenError("Another opening command is in progress.")
        if not self._open_transport_is_safe():
            raise UfanetOpenError("Opening transport is not safe.")
        last_attempt = gate.last_attempt
        if (
            last_attempt is not None
            and loop_time() - last_attempt < OPEN_COOLDOWN_SECONDS
        ):
            raise UfanetOpenError("Opening cooldown is active.")

        async with gate.lock:
            if self._closing:
                raise UfanetOpenError("Client is closing.")
            if not self._open_transport_is_safe():
                raise UfanetOpenError("Opening transport is not safe.")
            last_attempt = gate.last_attempt
            if (
                last_attempt is not None
                and loop_time() - last_attempt < OPEN_COOLDOWN_SECONDS
            ):
                raise UfanetOpenError("Opening cooldown is active.")
            async with self._operation_lock:
                if self._closing:
                    raise UfanetOpenError("Client is closing.")
                if not self._open_transport_is_safe():
                    raise UfanetOpenError("Opening transport is not safe.")
                try:
                    await self._ensure_auth_locked()
                    fresh_doors = await self._inventory_with_retry_locked()
                except asyncio.CancelledError:
                    raise
                except UfanetError:
                    raise UfanetOpenError("Entrance preflight failed.") from None

                fresh = fresh_doors.get(target_key)
                token = self._access_token
                if (
                    fresh is None
                    or token is None
                    or not fresh.openable
                    or not fresh.trusted
                    or not target.matches_command_identity(fresh)
                ):
                    raise UfanetOpenError("Entrance preflight failed.")

                if not self._open_transport_is_safe():
                    raise UfanetOpenError("Opening transport is not safe.")
                path = f"/api/v0/skud/shared/{fresh.shared_id}/open/?door={fresh.door}"
                authorization = f"JWT {token}"
                try:
                    await self._open_once_locked(path, authorization)
                finally:
                    if self._physical_outcome.get() is not None:
                        gate.last_attempt = loop_time()

    async def _open_once_locked(self, path: str, authorization: str) -> None:
        session = self._open_session
        if session is None:
            raise UfanetOpenError("Opening transport is not safe.")

        self._physical_outcome.set("unknown")
        response: _Response | None = None
        try:
            response = await session.get(
                f"{BASE_URL}{path}",
                headers={
                    "Authorization": authorization,
                    "Accept": _JSON_CONTENT_TYPE,
                    "User-Agent": USER_AGENT,
                },
                allow_redirects=False,
                timeout=_OPEN_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            raise UfanetOpenUnknownOutcome(
                "Opening outcome is unknown; do not retry."
            ) from None

        try:
            if response.status != 200:
                raise UfanetOpenError("Opening was not confirmed.")
            try:
                payload = await _read_bounded_json(
                    response,
                    MAX_OPEN_RESPONSE_BYTES,
                    lambda: UfanetProtocolError("Invalid opening response."),
                )
            except UfanetProtocolError:
                raise UfanetOpenError("Opening was not confirmed.") from None
            if (
                type(payload) is not dict
                or set(payload) != {"result"}
                or payload["result"] is not True
            ):
                raise UfanetOpenError("Opening was not confirmed.")
            self._physical_outcome.set("confirmed")
        except UfanetOpenError:
            self._physical_outcome.set("not_confirmed")
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            raise UfanetOpenUnknownOutcome(
                "Opening outcome is unknown; do not retry."
            ) from None
        finally:
            _safe_release(response)
