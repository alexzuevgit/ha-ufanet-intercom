"""Strict call-history parsing and an account-level observed-call pulse state machine."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

HISTORY_PATH: Final = "/api/v1/skuds/call-history/?page=1&page_size=10"
HISTORY_POLL_INTERVAL_SECONDS: Final = 3.0
MAX_HISTORY_RESPONSE_BYTES: Final = 64 * 1024
MAX_HISTORY_RESULTS: Final = 10
MAX_HISTORY_COUNT: Final = (1 << 31) - 1
MAX_HISTORY_UUID_CHARS: Final = 128
MAX_HISTORY_CAMERA_CHARS: Final = 256
MAX_HISTORY_TIMESTAMP_CHARS: Final = 64
MAX_HISTORY_POINTER_CHARS: Final = 2048
MAX_CALL_AGE_SECONDS: Final = 15.0
MAX_FUTURE_SKEW_SECONDS: Final = 5.0
MAX_OBSERVATION_GAP_SECONDS: Final = 15.0
CALL_PULSE_SECONDS: Final = 5.0
MAX_RECENT_CALL_IDS: Final = 256

_REQUIRED_PAGE_KEYS: Final = {"count", "next", "previous", "results"}
_REQUIRED_ROW_KEYS: Final = {"uuid", "called_at", "camera_number", "house_id"}


class HistoryProtocolError(Exception):
    """The provider history response did not satisfy the bounded contract."""


@dataclass(frozen=True, slots=True, repr=False)
class CallHistoryRow:
    """One validated row; the raw provider ID is intentionally not printable."""

    uuid: str = field(repr=False)
    called_at: datetime
    camera_number: str = field(repr=False)
    house_id: int

    @property
    def routing_key(self) -> tuple[str, int]:
        return self.camera_number, self.house_id

    def __repr__(self) -> str:
        return f"{type(self).__name__}(called_at={self.called_at.isoformat()!r})"


@dataclass(frozen=True, slots=True, repr=False)
class CallHistoryPage:
    """Validated first-page rows without retaining provider pagination URLs."""

    rows: tuple[CallHistoryRow, ...]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(row_count={len(self.rows)})"


def _bad() -> HistoryProtocolError:
    return HistoryProtocolError("Invalid call history response.")


def _valid_pointer(value: object) -> bool:
    return value is None or (
        type(value) is str and 0 < len(value) <= MAX_HISTORY_POINTER_CHARS
    )


def _valid_bounded_string(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and bool(value.strip())
        and value == value.strip()
    )


def _parse_called_at(value: object) -> datetime:
    if type(value) is not str or not 0 < len(value) <= MAX_HISTORY_TIMESTAMP_CHARS:
        raise _bad()
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        raise _bad() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _bad()
    return parsed.astimezone(UTC)


def parse_call_history(payload: object) -> CallHistoryPage:
    """Validate the exact first-page envelope and retain only four row fields."""

    if type(payload) is not dict or set(payload) != _REQUIRED_PAGE_KEYS:
        raise _bad()
    count = payload["count"]
    results = payload["results"]
    if (
        type(count) is not int
        or not 0 <= count <= MAX_HISTORY_COUNT
        or type(results) is not list
        or len(results) > MAX_HISTORY_RESULTS
        or count < len(results)
        or not _valid_pointer(payload["next"])
        or not _valid_pointer(payload["previous"])
    ):
        raise _bad()

    rows: list[CallHistoryRow] = []
    for item in results:
        if type(item) is not dict or not _REQUIRED_ROW_KEYS <= set(item):
            raise _bad()
        uuid = item["uuid"]
        camera_number = item["camera_number"]
        house_id = item["house_id"]
        if (
            not _valid_bounded_string(uuid, MAX_HISTORY_UUID_CHARS)
            or not _valid_bounded_string(camera_number, MAX_HISTORY_CAMERA_CHARS)
            or type(house_id) is not int
            or not 0 < house_id <= (1 << 63) - 1
        ):
            raise _bad()
        rows.append(
            CallHistoryRow(
                uuid=uuid,
                called_at=_parse_called_at(item["called_at"]),
                camera_number=camera_number,
                house_id=house_id,
            )
        )
    return CallHistoryPage(tuple(rows))


def _opaque_call_id(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8", "strict"), hashlib.sha256).digest()


def _is_aware_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class HistoryProcessResult:
    """Sanitized state transition result for tests and the HA poller."""

    baseline: bool = False
    failed: bool = False
    pulses: tuple[str, ...] = ()


class CallHistoryManager:
    """Fail-closed, bounded account-level state for observed-call pulses."""

    def __init__(
        self,
        mapping: Iterable[object] = (),
        *,
        monotonic: Callable[[], float] | None = None,
        dedupe_key: bytes | None = None,
    ) -> None:
        if dedupe_key is not None and (type(dedupe_key) is not bytes or not dedupe_key):
            raise ValueError("Invalid history deduplication key.")
        self._dedupe_key = dedupe_key or secrets.token_bytes(32)
        self._monotonic = monotonic or time.monotonic
        self._routes: dict[tuple[str, int], str] = {}
        self._target_keys: frozenset[str] = frozenset()
        self._known_routes: tuple[tuple[str, str, int, bool], ...] = ()
        self._recent_ids: deque[bytes] = deque(maxlen=MAX_RECENT_CALL_IDS)
        self._recent_id_set: set[bytes] = set()
        self._anchor_id: bytes | None = None
        self._available = False
        self._baseline_required = True
        self._last_success_monotonic: float | None = None
        self._pulses: dict[str, float] = {}
        self._listeners: list[Callable[[], None]] = []
        self.set_mapping(mapping)

    def _build_routes(
        self, mapping: Iterable[object]
    ) -> tuple[
        dict[tuple[str, int], str],
        frozenset[str],
        tuple[tuple[str, str, int, bool], ...],
    ]:
        candidates: dict[tuple[str, int], list[str]] = {}
        target_keys: set[str] = set()
        for door in mapping:
            key = getattr(door, "key", None)
            camera = getattr(door, "cctv_number", None)
            house = getattr(door, "house", None)
            if (
                type(key) is not str
                or not key
                or getattr(door, "trusted", None) is not True
                or not _valid_bounded_string(camera, MAX_HISTORY_CAMERA_CHARS)
                or type(house) is not int
                or not 0 < house <= (1 << 63) - 1
            ):
                continue
            target_keys.add(key)
            candidates.setdefault((camera, house), []).append(key)
        routes = {
            route: keys[0] for route, keys in candidates.items() if len(keys) == 1
        }
        signature = tuple(
            sorted(
                (key, camera, house, len(keys) == 1)
                for (camera, house), keys in candidates.items()
                for key in keys
            )
        )
        return routes, frozenset(target_keys), signature

    def set_mapping(self, mapping: Iterable[object]) -> None:
        """Replace exact trusted routing and rebaseline on any topology change."""

        routes, target_keys, signature = self._build_routes(mapping)
        if signature != self._known_routes:
            self._routes = routes
            self._target_keys = target_keys
            self._known_routes = signature
            self.fail()
        else:
            self._routes = routes
            self._target_keys = target_keys

    @property
    def available(self) -> bool:
        """Whether a successful baseline currently makes the feed usable."""

        return self._available

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001, S112
                continue

    def _clear_recent(self) -> None:
        self._recent_ids.clear()
        self._recent_id_set.clear()
        self._anchor_id = None

    def _consume(self, opaque_id: bytes) -> None:
        if opaque_id in self._recent_id_set:
            return
        if len(self._recent_ids) == self._recent_ids.maxlen:
            expired = self._recent_ids.popleft()
            self._recent_id_set.discard(expired)
        self._recent_ids.append(opaque_id)
        self._recent_id_set.add(opaque_id)

    def fail(self) -> None:
        """Mark the feed unavailable and require a fresh no-pulse baseline."""

        self._available = False
        self._baseline_required = True
        self._last_success_monotonic = None
        self._pulses.clear()
        self._clear_recent()
        self._notify()

    def stop(self) -> None:
        """Clear all runtime state on unload."""

        self.fail()
        self._routes = {}
        self._target_keys = frozenset()
        self._known_routes = ()

    def process(
        self,
        page: CallHistoryPage | Mapping[str, object],
        *,
        now: datetime,
        monotonic: float | None = None,
    ) -> HistoryProcessResult:
        """Process one successful poll, or fail closed on continuity/freshness gaps."""

        if not isinstance(page, CallHistoryPage):
            page = parse_call_history(page)
        if not _is_aware_utc(now):
            raise ValueError("History comparison time must be timezone-aware.")
        current_mono = self._monotonic() if monotonic is None else monotonic
        if current_mono is None:
            raise ValueError("History monotonic time is required.")
        current_mono = float(current_mono)
        current_ids = tuple(
            _opaque_call_id(self._dedupe_key, row.uuid) for row in page.rows
        )

        if self._last_success_monotonic is not None and (
            current_mono - self._last_success_monotonic > MAX_OBSERVATION_GAP_SECONDS
        ):
            self.fail()
            return HistoryProcessResult(failed=True)
        if self._anchor_id is not None and self._anchor_id not in current_ids:
            self.fail()
            return HistoryProcessResult(failed=True)

        if self._baseline_required:
            self._clear_recent()
            for opaque_id in current_ids:
                self._consume(opaque_id)
            self._anchor_id = current_ids[-1] if current_ids else None
            self._baseline_required = False
            self._available = True
            self._last_success_monotonic = current_mono
            self._notify()
            return HistoryProcessResult(baseline=True)

        pulses: list[str] = []
        for row, opaque_id in zip(page.rows, current_ids, strict=True):
            if opaque_id in self._recent_id_set:
                continue
            # Consume before routing, freshness, or ambiguity decisions.
            self._consume(opaque_id)
            age = (now - row.called_at).total_seconds()
            if age < -MAX_FUTURE_SKEW_SECONDS or age > MAX_CALL_AGE_SECONDS:
                continue
            target_key = self._routes.get(row.routing_key)
            if target_key is None:
                continue
            self._pulses[target_key] = current_mono + CALL_PULSE_SECONDS
            pulses.append(target_key)

        self._anchor_id = current_ids[-1] if current_ids else self._anchor_id
        self._available = True
        self._last_success_monotonic = current_mono
        self._expire(current_mono)
        self._notify()
        return HistoryProcessResult(pulses=tuple(dict.fromkeys(pulses)))

    def _expire(self, monotonic: float) -> None:
        for key, deadline in tuple(self._pulses.items()):
            if monotonic >= deadline:
                del self._pulses[key]

    def available_for(self, target_key: str) -> bool:
        return self._available and target_key in self._target_keys

    def is_on_for(self, target_key: str, monotonic: float | None = None) -> bool:
        current = (
            self._monotonic() if monotonic is None and self._monotonic else monotonic
        )
        if current is None:
            return False
        self._expire(float(current))
        return self.available_for(target_key) and target_key in self._pulses


class CallHistoryPoller:
    """One serialized account poller; it never creates a task per door."""

    def __init__(
        self,
        client: Any,
        manager: CallHistoryManager,
        mapping_provider: Callable[[], Iterable[object]],
        *,
        interval: float = HISTORY_POLL_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._manager = manager
        self._mapping_provider = mapping_provider
        self._interval = interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._task: Any = None
        self._stopped = True

    async def async_start(self) -> None:
        """Start one background history loop; its first response is a baseline."""

        if self._task is not None and not self._task.done():
            return
        import asyncio

        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def async_stop(self) -> None:
        """Cancel the one poller and clear all pulse state."""

        import asyncio

        self._stopped = True
        task = self._task
        self._task = None
        self._manager.stop()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _run(self) -> None:
        import asyncio

        while not self._stopped:
            await self.async_poll_once()
            await asyncio.sleep(self._interval)

    async def async_poll_once(self) -> HistoryProcessResult | None:
        """Perform one read-only GET and convert all failures to unavailable state."""

        import asyncio

        try:
            self._manager.set_mapping(self._mapping_provider())
            page = await self._client.async_call_history()
            return self._manager.process(page, now=self._clock())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._manager.fail()
            return None
