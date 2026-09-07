"""Pure bounded per-device runtime for protected voice phrase matching.

The module deliberately has no Home Assistant dependency.  An adapter supplies an
immutable discovered-door snapshot, loopback proxy URLs, an STT client, and an
executor suitable for the CPU-bound protected phrase matcher.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from types import CoroutineType, MappingProxyType
from typing import Final, NoReturn, Protocol, cast
from urllib.parse import urlsplit

from .const import DiscoveredDoor
from .voice_audio import FfmpegPcmSource, MicroVadAdapter, PcmSegmenter
from .voice_phrase import PhraseConfigError, PhraseMatcher, validate_entered_phrases
from .voice_stt import MAX_TRANSCRIPT_UTF8_BYTES, SttConfig

VOICE_PHRASE_OPTIONS_ROOT: Final = "voice_phrase"
VOICE_PHRASE_STORAGE_VERSION: Final = 1
MAX_VOICE_TARGETS: Final = 8
MAX_STT_CONCURRENCY: Final = 2
DEFAULT_FRAME_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_PULSE_SECONDS: Final = 5.0
DEFAULT_CLEANUP_RETRY_SECONDS: Final = 1.0
# Budget begins when a bounded (at most eight-second) utterance is complete.
DEFAULT_UTTERANCE_FRESHNESS_SECONDS: Final = 10.0
DEFAULT_MIN_STT_INTERVAL_SECONDS: Final = 5.0
MAX_RECONNECT_SECONDS: Final = 30.0
VOICE_PHRASE_CONFIG_ERROR: Final = "Invalid voice phrase runtime configuration."

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_ENABLED_KEYS = {
    "version",
    "enabled",
    "endpoint",
    "token",
    "model",
    "allow_insecure_http",
    "targets",
}
_DISABLED_KEYS = {"version", "enabled"}
_TARGET_KEYS = {"binding", "phrases"}
_EDITABLE_TARGET_KEYS = _TARGET_KEYS | {"entered_phrases"}
_LOOP_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.Semaphore]
] = weakref.WeakKeyDictionary()
_BUILTIN_TASK_TYPE: Final = asyncio.Task
_BUILTIN_FUTURE_TYPE: Final = asyncio.Future
_TASK_GET_LOOP: Final = _BUILTIN_FUTURE_TYPE.get_loop
_TASK_GET_CORO: Final = _BUILTIN_TASK_TYPE.get_coro
_TASK_DONE: Final = _BUILTIN_TASK_TYPE.done
_TASK_CANCELLED: Final = _BUILTIN_TASK_TYPE.cancelled
_TASK_CANCELLING: Final = _BUILTIN_TASK_TYPE.cancelling
_TASK_CANCEL: Final = _BUILTIN_TASK_TYPE.cancel
_TASK_UNCANCEL: Final = _BUILTIN_TASK_TYPE.uncancel
_TASK_ADD_DONE_CALLBACK: Final = _BUILTIN_TASK_TYPE.add_done_callback
_FUTURE_CANCELLED: Final = _BUILTIN_FUTURE_TYPE.cancelled
_FUTURE_EXCEPTION: Final = _BUILTIN_FUTURE_TYPE.exception
# Private test seam.  Production always holds the exact constructor captured above.
_OWNED_TASK_CONSTRUCTOR = _BUILTIN_TASK_TYPE


class VoicePhraseConfigError(ValueError):
    """Fixed-detail storage validation error safe for public presentation."""

    def __init__(self, *_private: object) -> None:
        super().__init__(VOICE_PHRASE_CONFIG_ERROR)


def _fail_config() -> NoReturn:
    """Raise the sole context-free public storage error."""

    try:
        raise VoicePhraseConfigError()
    except VoicePhraseConfigError as error:
        error.args = (VOICE_PHRASE_CONFIG_ERROR,)
        error.__cause__ = None
        error.__context__ = None
        error.__notes__ = []
        raise


def _valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and _HEX_DIGEST.fullmatch(value) is not None
    )


def _finite_positive_seconds(value: float) -> float:
    """Convert an exact numeric duration without leaking conversion failures."""

    if type(value) not in (int, float):
        raise ValueError("Invalid voice phrase manager configuration.")
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Invalid voice phrase manager configuration.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Invalid voice phrase manager configuration.")
    return seconds


def _sanitize_exception_chain(error: BaseException) -> None:
    """Scrub private chaining metadata without invoking subclass behavior."""

    for attribute, value in (
        ("__traceback__", None),
        ("__context__", None),
        ("__cause__", None),
        ("__suppress_context__", False),
    ):
        try:
            BaseException.__dict__[attribute].__set__(error, value)
        except BaseException:  # noqa: BLE001, S110 - best-effort safe scrub
            pass
    try:
        namespace = BaseException.__dict__["__dict__"].__get__(error, BaseException)
        dict.pop(namespace, "__notes__", None)
    except BaseException:  # noqa: BLE001, S110 - best-effort safe scrub
        pass


def _raise_sanitized(error: BaseException) -> NoReturn:
    """Re-raise one exact control-flow object without inherited private chains."""

    _sanitize_exception_chain(error)
    raise error


def _is_ordinary_exception(error: BaseException) -> bool:
    """Classify an exception without consulting a hostile ``__class__`` hook."""

    return issubclass(type(error), Exception)


def _loop_limiter() -> asyncio.Semaphore:
    """Return the lazily allocated process-shared limiter for this running loop."""

    loop = asyncio.get_running_loop()
    limiter_reference = _LOOP_LIMITERS.get(loop)
    limiter = limiter_reference() if limiter_reference is not None else None
    if limiter is None:
        limiter = asyncio.Semaphore(MAX_STT_CONCURRENCY)
        _LOOP_LIMITERS[loop] = weakref.ref(limiter)
    return limiter


class _OwnedTaskIdentity:
    """Private exact identity and publication gate for one owned boundary."""

    __slots__ = (
        "aborted",
        "armed",
        "cancellation_baseline",
        "coroutine",
        "publication",
        "published",
        "task",
    )

    def __init__(self) -> None:
        self.aborted = False
        self.armed = False
        self.cancellation_baseline: int | None = None
        self.coroutine: CoroutineType | None = None
        self.publication: asyncio.Future[None] | None = (
            asyncio.get_running_loop().create_future()
        )
        self.published = False
        self.task: asyncio.Task[object] | None = None


def _is_task_instance(value: object) -> bool:
    """Recognize the exact import-captured Task without instance hooks."""

    return type(value) is _BUILTIN_TASK_TYPE


def _task_loop(task: asyncio.Task[object]) -> asyncio.AbstractEventLoop:
    """Read a Task's physical loop through the built-in Future API."""

    return _TASK_GET_LOOP(task)


def _task_coroutine(task: asyncio.Task[object]) -> object:
    """Read a Task's physical coroutine without subclass dispatch."""

    return _TASK_GET_CORO(task)


def _task_done(task: asyncio.Task[object]) -> bool:
    """Read a Task's physical terminal state without subclass dispatch."""

    return _TASK_DONE(task)


def _task_cancelled(task: asyncio.Task[object]) -> bool:
    """Read a Task's physical cancelled state without subclass dispatch."""

    return _TASK_CANCELLED(task)


def _task_cancelling(task: asyncio.Task[object]) -> int:
    """Read a Task's physical cancellation count without subclass dispatch."""

    return _TASK_CANCELLING(task)


def _task_cancel_message(task: asyncio.Task[object]) -> object:
    """Read the physical Future cancellation payload on supported CPython."""

    descriptor = _BUILTIN_FUTURE_TYPE.__dict__.get("_cancel_message")
    if descriptor is None:
        raise RuntimeError
    return descriptor.__get__(task, _BUILTIN_FUTURE_TYPE)


def _task_must_cancel(task: asyncio.Task[object]) -> object:
    """Read the physical pending-delivery bit on supported CPython."""

    descriptor = _BUILTIN_TASK_TYPE.__dict__.get("_must_cancel")
    if descriptor is None:
        raise RuntimeError
    return descriptor.__get__(task, _BUILTIN_TASK_TYPE)


def _task_waiter(task: asyncio.Task[object]) -> object:
    """Read the physical delegated waiter on supported CPython."""

    descriptor = _BUILTIN_TASK_TYPE.__dict__.get("_fut_waiter")
    if descriptor is None:
        raise RuntimeError
    return descriptor.__get__(task, _BUILTIN_TASK_TYPE)


def _task_state(task: asyncio.Task[object]) -> object:
    """Read the physical Future state on supported CPython."""

    descriptor = _BUILTIN_FUTURE_TYPE.__dict__.get("_state")
    if descriptor is None:
        raise RuntimeError
    return descriptor.__get__(task, _BUILTIN_FUTURE_TYPE)


def _is_pristine_live_task(
    task: asyncio.Task[object],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    coroutine: CoroutineType | None = None,
) -> bool:
    """Require a pending owner with no cancellation history or delivery state."""

    try:
        return (
            _is_task_instance(task)
            and (loop is None or _task_loop(task) is loop)
            and (coroutine is None or _task_coroutine(task) is coroutine)
            and not _task_done(task)
            and not _task_cancelled(task)
            and _task_cancelling(task) == 0
            and _task_must_cancel(task) is False
            and _task_cancel_message(task) is None
            and _task_state(task) == "PENDING"
        )
    except BaseException as error:  # noqa: BLE001 - ambiguous state fails closed
        _sanitize_exception_chain(error)
        return False


def _abort_owned_task_identity(identity: _OwnedTaskIdentity) -> None:
    """Fail one marker closed before waking or cancelling its exact owner."""

    identity.aborted = True
    publication = identity.publication
    if publication is not None and not publication.done():
        publication.cancel()


def _release_owned_task_identity(identity: _OwnedTaskIdentity) -> None:
    """Break terminal marker, gate, coroutine, and Task reference cycles."""

    publication = identity.publication
    if publication is not None and not publication.done():
        publication.cancel()
    identity.armed = False
    identity.cancellation_baseline = None
    identity.coroutine = None
    identity.publication = None
    identity.task = None


def _release_completed_owned_task_identity(
    _completed: asyncio.Task[object], identity: _OwnedTaskIdentity
) -> None:
    """Release publication identity after its unique Task is terminal."""

    _release_owned_task_identity(identity)


def _running_owned_boundary_task(
    identity: _OwnedTaskIdentity,
) -> asyncio.Task[object] | None:
    """Validate the exact Task executing the armed owned coroutine."""

    current = asyncio.current_task()
    if identity.aborted:
        return None
    if (
        not identity.armed
        or identity.coroutine is None
        or not _is_task_instance(current)
    ):
        _abort_owned_task_identity(identity)
        return None
    current = cast("asyncio.Task[object]", current)
    if (
        current is not identity.task
        or _task_coroutine(current) is not identity.coroutine
    ):
        _abort_owned_task_identity(identity)
        return None
    if identity.cancellation_baseline is None:
        identity.cancellation_baseline = _task_cancelling(current)
    return current


def _sanitize_prepublication_cancellation(
    identity: _OwnedTaskIdentity, error: asyncio.CancelledError
) -> None:
    """Sanitize cancellation only when delivered to the exact published owner."""

    current = asyncio.current_task()
    if not _is_task_instance(current):
        _abort_owned_task_identity(identity)
        _sanitize_exception_chain(error)
        return
    current = cast("asyncio.Task[object]", current)
    if (
        current is identity.task
        and identity.coroutine is not None
        and _task_coroutine(current) is identity.coroutine
    ):
        baseline = identity.cancellation_baseline
        _sanitize_current_task_cancellation(
            error, baseline if baseline is not None else 0
        )
        return
    _abort_owned_task_identity(identity)
    _sanitize_exception_chain(error)


async def _await_owned_task_publication(
    identity: _OwnedTaskIdentity, *, resist_published_cancellation: bool = False
) -> tuple[asyncio.Task[object], int] | None:
    """Validate the exact owner and await its creator's publication gate."""

    owner = _running_owned_boundary_task(identity)
    if owner is None or identity.aborted:
        return None
    baseline = identity.cancellation_baseline
    if baseline is None:
        _abort_owned_task_identity(identity)
        return None
    publication = identity.publication
    if publication is None:
        _abort_owned_task_identity(identity)
        return None
    try:
        await publication
    except GeneratorExit:
        raise
    except asyncio.CancelledError as error:
        _sanitize_current_task_cancellation(error, baseline)
        if (
            resist_published_cancellation
            and not identity.aborted
            and identity.published
            and identity.task is owner
            and publication.done()
            and not publication.cancelled()
        ):
            return owner, baseline
        return None
    except BaseException as error:  # noqa: BLE001 - a bad resume fails closed
        _abort_owned_task_identity(identity)
        _sanitize_exception_chain(error)
        return None
    if (
        _running_owned_boundary_task(identity) is not owner
        or identity.aborted
        or not identity.published
    ):
        _abort_owned_task_identity(identity)
        return None
    return owner, baseline


def _clear_owned_task_cancellation_payload(current: asyncio.Task[object]) -> None:
    """Drop an owned Task's physical payload without changing its cancel count."""

    descriptor = _BUILTIN_FUTURE_TYPE.__dict__.get("_cancel_message")
    if descriptor is None:
        return
    try:
        descriptor.__set__(current, None)
    except BaseException as error:  # noqa: BLE001 - best-effort physical scrub
        _sanitize_exception_chain(error)


def _cancel_owned_task_for_lifecycle(task: asyncio.Task[object]) -> bool:
    """Request one physical cancel unless delivery is already genuinely pending."""

    if not _is_task_instance(task):
        return False
    try:
        if _task_done(task):
            return False
        if _task_must_cancel(task) is True:
            return False
        waiter = _task_waiter(task)
        if waiter is not None:
            if not issubclass(type(waiter), _BUILTIN_FUTURE_TYPE):
                raise RuntimeError
            if _FUTURE_CANCELLED(cast("asyncio.Future[object]", waiter)):
                return False
    except BaseException as error:  # noqa: BLE001 - ambiguous live state cancels safe
        _sanitize_exception_chain(error)
    try:
        requested = _TASK_CANCEL(task)
    except BaseException as error:  # noqa: BLE001 - lifecycle must not crash
        _sanitize_exception_chain(error)
        return False
    if requested:
        # ``Task.cancel(None)`` delegates to a waiter without necessarily replacing
        # an older physical Task payload, so scrub that stale value explicitly.
        _clear_owned_task_cancellation_payload(task)
    return requested


async def _quarantine_task(
    operation: Callable[..., Awaitable[object]], arguments: tuple[object, ...]
) -> None:
    """Run owned work to completion without retaining its terminal failure in Task."""

    _owned_task_identity = _OwnedTaskIdentity()
    owned_task: asyncio.Task[object] | None = None
    entry_cancellation_baseline = 0
    try:
        # Manual priming reaches only this marker yield.  Every Task owner must
        # then stop on the code-created publication Future before private work.
        await asyncio.sleep(0)
        publication = await _await_owned_task_publication(_owned_task_identity)
        if publication is None or _owned_task_identity.aborted:
            return
        owned_task, entry_cancellation_baseline = publication
        if _owned_task_identity.aborted:
            return
        await operation(*arguments)
        if _owned_task_identity.aborted:
            return
        await _checkpoint_owned_task_cancellation(entry_cancellation_baseline)
    except GeneratorExit:
        raise
    except asyncio.CancelledError as error:
        if owned_task is None:
            _sanitize_prepublication_cancellation(_owned_task_identity, error)
        else:
            _sanitize_current_task_cancellation(error, entry_cancellation_baseline)
    except BaseException as error:  # noqa: BLE001 - task boundary quarantines all
        _sanitize_exception_chain(error)
    finally:
        if owned_task is not None:
            _clear_owned_task_cancellation_payload(owned_task)
        entry_cancellation_baseline = 0
        owned_task = None
        del operation, arguments


async def _capture_task_outcome(
    operation: Callable[..., Awaitable[object]],
    arguments: tuple[object, ...],
    outcome: list[tuple[bool, object]],
) -> None:
    """Capture an owned operation outcome while keeping its Task terminal-safe."""

    _owned_task_identity = _OwnedTaskIdentity()
    owned_task: asyncio.Task[object] | None = None
    entry_cancellation_baseline = 0
    try:
        await asyncio.sleep(0)
        publication = await _await_owned_task_publication(_owned_task_identity)
        if publication is None or _owned_task_identity.aborted:
            return
        owned_task, entry_cancellation_baseline = publication
        if _owned_task_identity.aborted:
            return
        value = await operation(*arguments)
        if _owned_task_identity.aborted:
            return
        await _checkpoint_owned_task_cancellation(entry_cancellation_baseline)
        if _owned_task_identity.aborted:
            return
    except GeneratorExit:
        raise
    except asyncio.CancelledError as error:
        if owned_task is None:
            _sanitize_prepublication_cancellation(_owned_task_identity, error)
        else:
            _sanitize_current_task_cancellation(error, entry_cancellation_baseline)
            if not _owned_task_identity.aborted:
                outcome.append((False, error))
    except BaseException as error:  # noqa: BLE001 - exact outcome owned by caller
        _sanitize_exception_chain(error)
        if owned_task is not None and not _owned_task_identity.aborted:
            outcome.append((False, error))
    else:
        if not _owned_task_identity.aborted:
            outcome.append((True, value))
    finally:
        if owned_task is not None:
            _clear_owned_task_cancellation_payload(owned_task)
        entry_cancellation_baseline = 0
        owned_task = None
        del operation, arguments, outcome


def _sanitize_owned_cancellation(
    error: asyncio.CancelledError | None,
    current: asyncio.Task[object],
    cancellation_count: int,
) -> None:
    """Consume an exact private Task-cancellation delta and its payload."""

    if error is not None:
        _sanitize_exception_chain(error)
        try:
            BaseException.__dict__["args"].__set__(error, ())
        except BaseException:  # noqa: BLE001, S110 - best-effort safe scrub
            pass
    for _ in range(cancellation_count):
        _TASK_UNCANCEL(current)
    _clear_owned_task_cancellation_payload(current)


def _sanitize_current_task_cancellation(
    error: asyncio.CancelledError | None, entry_cancellation_baseline: int
) -> bool:
    """Scrub only cancellation directed at this owned Task after its entry."""

    current = asyncio.current_task()
    cancellation_count = (
        _task_cancelling(cast("asyncio.Task[object]", current))
        if _is_task_instance(current)
        else 0
    )
    cancellation_delta = max(0, cancellation_count - entry_cancellation_baseline)
    if current is not None and cancellation_delta > 0:
        _sanitize_owned_cancellation(error, current, cancellation_delta)
        return True
    if error is not None:
        _sanitize_exception_chain(error)
    return False


def _complete_inline_stop_checkpoint(checkpoint: asyncio.Future[None]) -> None:
    """Complete one code-owned checkpoint unless cancellation already won."""

    if not checkpoint.done():
        checkpoint.set_result(None)


async def _inline_stop_checkpoint() -> None:
    """Yield through a real Future so the first burst cancellation stays exact."""

    loop = asyncio.get_running_loop()
    checkpoint = loop.create_future()
    completion = loop.call_soon(_complete_inline_stop_checkpoint, checkpoint)
    try:
        await checkpoint
    finally:
        completion.cancel()
        if not checkpoint.done():
            checkpoint.cancel()


async def _checkpoint_owned_task_cancellation(
    entry_cancellation_baseline: int,
) -> None:
    """Deliver and scrub a pending owned cancellation without a clean-path yield."""

    current = asyncio.current_task()
    if (
        not _is_task_instance(current)
        or _task_cancelling(cast("asyncio.Task[object]", current))
        <= entry_cancellation_baseline
    ):
        return
    try:
        await _inline_stop_checkpoint()
    except asyncio.CancelledError as error:
        _sanitize_current_task_cancellation(error, entry_cancellation_baseline)
    else:
        # A dependency may already have swallowed delivery while leaving the
        # Task's count and private cancellation message behind.
        _sanitize_current_task_cancellation(None, entry_cancellation_baseline)


async def _cleanup_owned_task(
    operation: Callable[..., Awaitable[object]],
    arguments: tuple[object, ...],
    outcome: list[BaseException],
) -> None:
    """Run terminal cleanup despite cancellation of this private owned Task."""

    _owned_task_identity = _OwnedTaskIdentity()
    owned_task: asyncio.Task[object] | None = None
    entry_cancellation_baseline = 0
    try:
        try:
            await asyncio.sleep(0)
            publication = await _await_owned_task_publication(
                _owned_task_identity, resist_published_cancellation=True
            )
        except GeneratorExit:
            raise
        except asyncio.CancelledError as error:
            _sanitize_prepublication_cancellation(_owned_task_identity, error)
            if (
                _owned_task_identity.aborted
                or not _owned_task_identity.published
                or _owned_task_identity.task is not asyncio.current_task()
                or _owned_task_identity.cancellation_baseline is None
            ):
                return
            publication = (
                _owned_task_identity.task,
                _owned_task_identity.cancellation_baseline,
            )
        except BaseException as error:  # noqa: BLE001 - publication fails closed
            _abort_owned_task_identity(_owned_task_identity)
            _sanitize_exception_chain(error)
            return
        if publication is None or _owned_task_identity.aborted:
            return
        owned_task, entry_cancellation_baseline = publication

        while not _owned_task_identity.aborted:
            try:
                if _owned_task_identity.aborted:
                    return
                await operation(*arguments)
            except GeneratorExit:
                raise
            except asyncio.CancelledError as error:
                owned_cancellation = _sanitize_current_task_cancellation(
                    error, entry_cancellation_baseline
                )
                if _owned_task_identity.aborted:
                    return
                if not owned_cancellation and not outcome:
                    outcome.append(error)
                if _owned_task_identity.aborted:
                    return
                continue
            except BaseException as error:  # noqa: BLE001 - retry to invariants
                _sanitize_exception_chain(error)
                if _owned_task_identity.aborted:
                    return
                if not outcome:
                    outcome.append(error)
                if _owned_task_identity.aborted:
                    return
                continue
            if _owned_task_identity.aborted:
                return
            await _checkpoint_owned_task_cancellation(entry_cancellation_baseline)
            if _owned_task_identity.aborted:
                return
            return
    finally:
        if owned_task is not None:
            _clear_owned_task_cancellation_payload(owned_task)
        entry_cancellation_baseline = 0
        owned_task = None
        del operation, arguments, outcome


_OWNED_BOUNDARY_CODES: Final = frozenset(
    (
        _quarantine_task.__code__,
        _capture_task_outcome.__code__,
        _cleanup_owned_task.__code__,
    )
)


def _consume_failed_owned_task(completed: asyncio.Task[object]) -> None:
    """Consume and scrub one exact manager-owned Task after fail-closed cancel."""

    if not _is_task_instance(completed):
        return
    terminal_error: BaseException | None = None
    try:
        terminal_error = _FUTURE_EXCEPTION(completed)
    except BaseException as error:  # noqa: BLE001 - consume every terminal outcome
        terminal_error = error
    if terminal_error is not None:
        _sanitize_exception_chain(terminal_error)
        if issubclass(type(terminal_error), asyncio.CancelledError):
            try:
                BaseException.__dict__["args"].__set__(terminal_error, ())
            except BaseException:  # noqa: BLE001, S110 - best-effort safe scrub
                pass
    cancellation_count = _task_cancelling(completed)
    for _ in range(cancellation_count):
        _TASK_UNCANCEL(completed)
    _clear_owned_task_cancellation_payload(completed)


def _fail_closed_owned_task(task: asyncio.Task[object]) -> None:
    """Cancel and arrange terminal consumption for one exact owned Task."""

    if not _is_task_instance(task):
        return
    if _task_done(task):
        _consume_failed_owned_task(task)
        return
    _TASK_ADD_DONE_CALLBACK(task, _consume_failed_owned_task)
    _cancel_owned_task_for_lifecycle(task)


def _publish_owned_task(
    identity: _OwnedTaskIdentity,
    task: asyncio.Task[object],
    loop: asyncio.AbstractEventLoop,
) -> bool:
    """Bind one exact Task and open its gate without running it synchronously."""

    if identity.aborted or (identity.task is not None and identity.task is not task):
        return False
    if not _is_pristine_live_task(task, loop=loop, coroutine=identity.coroutine):
        _abort_owned_task_identity(identity)
        return False
    identity.task = task
    if identity.cancellation_baseline is None:
        identity.cancellation_baseline = 0
    if identity.cancellation_baseline != 0:
        _abort_owned_task_identity(identity)
        return False
    publication = identity.publication
    if publication is None or publication.done():
        _abort_owned_task_identity(identity)
        return False

    identity.published = True
    _TASK_ADD_DONE_CALLBACK(
        task,
        lambda completed: _release_completed_owned_task_identity(completed, identity),
    )
    publication.set_result(None)
    return True


def _create_owned_task(
    coroutine: Coroutine[object, object, None],
) -> asyncio.Task[None] | None:
    """Prime one known boundary and construct its exact manager-owned Task."""

    owned_boundary: CoroutineType | None = None
    exact_marker: _OwnedTaskIdentity | None = None
    loop: asyncio.AbstractEventLoop | None = None
    preparation_error: BaseException | None = None
    try:
        if type(coroutine) is not CoroutineType:
            raise TypeError
        owned_boundary = cast(CoroutineType, coroutine)
        if (
            owned_boundary.cr_code not in _OWNED_BOUNDARY_CODES
            or inspect.getcoroutinestate(owned_boundary) != inspect.CORO_CREATED
        ):
            raise TypeError
        if (
            owned_boundary.send(None) is not None
            or inspect.getcoroutinestate(owned_boundary) != inspect.CORO_SUSPENDED
        ):
            raise RuntimeError
        frame = owned_boundary.cr_frame
        if frame is None:
            raise RuntimeError
        identity = frame.f_locals.get("_owned_task_identity")
        if (
            type(identity) is not _OwnedTaskIdentity
            or identity.armed is not False
            or identity.coroutine is not None
            or identity.task is not None
            or identity.published is not False
            or identity.aborted is not False
            or identity.cancellation_baseline is not None
            or identity.publication is None
            or identity.publication.done()
        ):
            raise RuntimeError
        exact_marker = identity
        identity.coroutine = owned_boundary
        identity.armed = True
        loop = asyncio.get_running_loop()
    except BaseException as error:  # noqa: BLE001 - preparation fails closed
        preparation_error = error

    if preparation_error is not None:
        if exact_marker is not None:
            _abort_owned_task_identity(exact_marker)
            _release_owned_task_identity(exact_marker)
        try:
            coroutine.close()
        except BaseException as close_error:  # noqa: BLE001 - own exact coroutine
            _sanitize_exception_chain(close_error)
        _sanitize_exception_chain(preparation_error)
        return None

    if owned_boundary is None or exact_marker is None or loop is None:
        try:
            coroutine.close()
        except BaseException as close_error:  # noqa: BLE001 - own exact coroutine
            _sanitize_exception_chain(close_error)
        return None

    created: asyncio.Task[object] | None = None
    creation_error: BaseException | None = None
    try:
        candidate = _OWNED_TASK_CONSTRUCTOR(
            owned_boundary, loop=loop, eager_start=False
        )
        if not _is_task_instance(candidate):
            raise TypeError
        created = candidate
    except BaseException as error:  # noqa: BLE001 - construction fails closed
        creation_error = error
        captured = exact_marker.task
        if _is_task_instance(captured):
            captured = cast("asyncio.Task[object]", captured)
            try:
                if (
                    _task_loop(captured) is loop
                    and _task_coroutine(captured) is owned_boundary
                ):
                    created = captured
            except BaseException as inspection_error:  # noqa: BLE001 - fail closed
                _sanitize_exception_chain(inspection_error)

    if creation_error is None and created is not None:
        try:
            if _publish_owned_task(exact_marker, created, loop):
                return cast("asyncio.Task[None]", created)
        except BaseException as error:  # noqa: BLE001 - publication fails closed
            _sanitize_exception_chain(error)

    _abort_owned_task_identity(exact_marker)
    if created is not None:
        _fail_closed_owned_task(created)
    else:
        try:
            owned_boundary.close()
        except BaseException as close_error:  # noqa: BLE001 - own exact coroutine
            _sanitize_exception_chain(close_error)
    _release_owned_task_identity(exact_marker)
    if creation_error is not None:
        _sanitize_exception_chain(creation_error)
    return None


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class VoiceTargetConfig:
    """One immutable exact target binding and its protected matcher."""

    key: str = field(repr=False)
    binding: str = field(repr=False)
    _matcher: PhraseMatcher = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not _valid_digest(self.key)
            or not _valid_digest(self.binding)
            or type(self._matcher) is not PhraseMatcher
        ):
            _fail_config()

    @property
    def phrase_count(self) -> int:
        return self._matcher.count

    def matches(self, value: object) -> bool:
        """Match one transcript without exposing mutable protected matcher state."""

        return self._matcher.matches(value)

    def __repr__(self) -> str:
        return f"VoiceTargetConfig(phrase_count={self.phrase_count})"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class VoiceRuntimeConfig:
    """Immutable validated runtime configuration with a secret-safe repr."""

    enabled: bool
    _stt_config: SttConfig | None = field(default=None, repr=False)
    _targets: tuple[VoiceTargetConfig, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        valid_disabled = (
            self.enabled is False
            and self._stt_config is None
            and type(self._targets) is tuple
            and not self._targets
        )
        valid_enabled = (
            self.enabled is True
            and type(self._stt_config) is SttConfig
            and type(self._targets) is tuple
            and 1 <= len(self._targets) <= MAX_VOICE_TARGETS
            and all(type(target) is VoiceTargetConfig for target in self._targets)
            and tuple(target.key for target in self._targets)
            == tuple(sorted(target.key for target in self._targets))
            and len({target.key for target in self._targets}) == len(self._targets)
        )
        if not (valid_disabled or valid_enabled):
            _fail_config()

    @property
    def stt_config(self) -> SttConfig | None:
        """Return the immutable STT configuration for an explicit adapter."""

        return self._stt_config

    @property
    def targets(self) -> tuple[VoiceTargetConfig, ...]:
        """Return targets in deterministic key order."""

        return self._targets

    @property
    def target_count(self) -> int:
        return len(self._targets)

    @property
    def target_keys(self) -> tuple[str, ...]:
        return tuple(target.key for target in self._targets)

    @property
    def configured_targets(self) -> tuple[tuple[str, str], ...]:
        return tuple((target.key, target.binding) for target in self._targets)

    @property
    def phrase_count(self) -> int:
        return sum(target.phrase_count for target in self._targets)

    def target_for(self, key: object) -> VoiceTargetConfig | None:
        """Find one exact configured key using the bounded target tuple."""

        if type(key) is not str:
            return None
        for target in self._targets:
            if target.key == key:
                return target
        return None

    def __repr__(self) -> str:
        return (
            "VoiceRuntimeConfig("
            f"enabled={self.enabled}, target_count={self.target_count}, "
            f"phrase_count={self.phrase_count})"
        )


def parse_voice_phrase_options(options: object) -> VoiceRuntimeConfig:
    """Parse options without retaining private input in a public error traceback."""

    try:
        return _parse_voice_phrase_options(options)
    except VoicePhraseConfigError:
        pass
    finally:
        options = None
        del options
    _fail_config()


def _parse_voice_phrase_options(options: object) -> VoiceRuntimeConfig:
    """Parse the strict version-one ``voice_phrase`` options subtree.

    Other top-level integration options are intentionally ignored.  The subtree,
    when present, is exact: empty disabled storage has ``version`` and ``enabled``;
    paused storage retains validated STT fields and zero to eight protected targets.
    Targets may also retain bounded entered phrases for the admin Options editor;
    those lines are validated here but never copied into runtime configuration.
    Disabled runtime objects never retain STT settings or allocate target workers.
    """

    if type(options) is not dict and type(options) is not MappingProxyType:
        _fail_config()
    if VOICE_PHRASE_OPTIONS_ROOT not in options:
        return VoiceRuntimeConfig(False)

    root = options[VOICE_PHRASE_OPTIONS_ROOT]
    if (
        type(root) is not dict
        or len(root) not in (2, 7)
        or any(type(key) is not str for key in root)
    ):
        _fail_config()
    version = root.get("version")
    enabled = root.get("enabled")
    if type(version) is not int or version != VOICE_PHRASE_STORAGE_VERSION:
        _fail_config()
    if type(enabled) is not bool:
        _fail_config()
    if enabled is False and len(root) == 2:
        if set(root) != _DISABLED_KEYS:
            _fail_config()
        return VoiceRuntimeConfig(False)
    if len(root) != 7 or set(root) != _ENABLED_KEYS:
        _fail_config()

    endpoint = root["endpoint"]
    token = root["token"]
    model = root["model"]
    allow_insecure_http = root["allow_insecure_http"]
    if (
        type(endpoint) is not str
        or type(token) is not str
        or type(model) is not str
        or type(allow_insecure_http) is not bool
    ):
        _fail_config()

    stt_config: SttConfig | None = None
    invalid_stt = False
    try:
        stt_config = SttConfig(
            endpoint=endpoint,
            token=token,
            model=model,
            allow_insecure_http=allow_insecure_http,
        )
    except ValueError:
        invalid_stt = True
    if invalid_stt or stt_config is None:
        _fail_config()

    stored_targets = root["targets"]
    if (
        type(stored_targets) is not dict
        or not (1 if enabled else 0) <= len(stored_targets) <= MAX_VOICE_TARGETS
    ):
        _fail_config()

    # Validate every cheap bounded field before invoking any nested phrase parser.
    for key, stored_target in stored_targets.items():
        if (
            not _valid_digest(key)
            or type(stored_target) is not dict
            or len(stored_target) not in (2, 3)
            or any(type(field_name) is not str for field_name in stored_target)
            or set(stored_target) not in (_TARGET_KEYS, _EDITABLE_TARGET_KEYS)
            or not _valid_digest(stored_target.get("binding"))
        ):
            _fail_config()

    targets: list[VoiceTargetConfig] = []
    for key in sorted(stored_targets):
        stored_target = stored_targets[key]
        matcher: PhraseMatcher | None = None
        invalid_matcher = False
        try:
            matcher = PhraseMatcher.from_stored(stored_target["phrases"])
            if (
                "entered_phrases" in stored_target
                and validate_entered_phrases(stored_target["entered_phrases"])
                != matcher.count
            ):
                _fail_config()
        except (PhraseConfigError, ValueError):
            invalid_matcher = True
        if invalid_matcher or matcher is None:
            _fail_config()
        targets.append(
            VoiceTargetConfig(
                key=key,
                binding=stored_target["binding"],
                _matcher=matcher,
            )
        )
    if enabled is False:
        return VoiceRuntimeConfig(False)
    return VoiceRuntimeConfig(True, stt_config, tuple(targets))


class _PcmSource(Protocol):
    async def async_start(self) -> None: ...

    async def async_read_frame(self) -> bytes | None: ...

    async def async_close(self) -> None: ...


class _Vad(Protocol):
    def process(self, frame: object) -> float | None: ...


class _Segmenter(Protocol):
    def process(self, frame: object, speech_probability: object) -> bytes | None: ...


class _SttClient(Protocol):
    async def transcribe_pcm(self, pcm: object) -> str: ...


SnapshotProvider = Callable[[], Mapping[str, DiscoveredDoor]]
StreamUrlProvider = Callable[[str], str | None]
SourceFactory = Callable[[str, str], _PcmSource]
VadFactory = Callable[[], _Vad]
SegmenterFactory = Callable[[], _Segmenter]
AsyncExecutor = Callable[[Callable[[object], object], object], Awaitable[object]]
Sleep = Callable[[float], Awaitable[None]]
Listener = Callable[[], None]


async def _default_executor(
    function: Callable[[object], object], value: object
) -> object:
    return await asyncio.to_thread(function, value)


def _default_source_factory(binary: str, url: str) -> _PcmSource:
    return FfmpegPcmSource(binary, url)


def _valid_loopback_proxy_url(value: object, expected_alias: str) -> bool:
    """Require the exact token-free one-segment loopback RTSP route shape."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 4_096
        or "?" in value
        or "#" in value
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    if host not in {"127.0.0.1", "::1"} or port is None or not 1 <= port <= 65_535:
        return False
    expected_netloc = f"127.0.0.1:{port}" if host == "127.0.0.1" else f"[::1]:{port}"
    if (
        parsed.scheme != "rtsp"
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.count("/") != 1
    ):
        return False
    segment = parsed.path[1:]
    return bool(
        segment
        and segment == expected_alias
        and segment not in {".", ".."}
        and _OPAQUE_PATH_SEGMENT.fullmatch(segment) is not None
    )


@dataclass(slots=True, repr=False)
class _Worker:
    target: VoiceTargetConfig
    url: str
    generation: int
    task: asyncio.Task[None] | None = None
    stt_task: asyncio.Task[None] | None = None
    pulse_task: asyncio.Task[None] | None = None
    pulse_cleanup_task: asyncio.Task[None] | None = None
    pulse_token: object | None = None
    audio_epoch: int = 0
    stt_failed: bool = False

    def __repr__(self) -> str:
        """Render aggregate lifecycle state without target, route, or task details."""

        worker_active = self.task is not None and not self.task.done()
        stt_active = self.stt_task is not None and not self.stt_task.done()
        pulse_active = any(
            task is not None and not task.done()
            for task in (self.pulse_task, self.pulse_cleanup_task)
        )
        return (
            "_Worker("
            f"active={worker_active}, stt_active={stt_active}, "
            f"pulse_active={pulse_active}, stt_failed={self.stt_failed})"
        )


class VoicePhraseManager:
    """Coordinate at most one bounded audio worker per configured target.

    Construction is inert.  ``async_start`` performs the first synchronous
    reconciliation.  Later inventory/proxy updates call ``reconcile``; shutdown must
    await ``async_stop``.
    """

    def __init__(
        self,
        config: VoiceRuntimeConfig,
        *,
        snapshot_provider: SnapshotProvider | None,
        stream_url_provider: StreamUrlProvider | None,
        ffmpeg_binary: str | None = None,
        stt_client: _SttClient | None = None,
        async_executor: AsyncExecutor = _default_executor,
        source_factory: SourceFactory = _default_source_factory,
        vad_factory: VadFactory = MicroVadAdapter,
        segmenter_factory: SegmenterFactory = PcmSegmenter,
        sleep: Sleep = asyncio.sleep,
        cleanup_retry_sleep: Sleep = asyncio.sleep,
        frame_timeout_seconds: float = DEFAULT_FRAME_TIMEOUT_SECONDS,
        pulse_seconds: float = DEFAULT_PULSE_SECONDS,
        cleanup_retry_seconds: float = DEFAULT_CLEANUP_RETRY_SECONDS,
        utterance_freshness_seconds: float = DEFAULT_UTTERANCE_FRESHNESS_SECONDS,
        min_stt_interval_seconds: float = DEFAULT_MIN_STT_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(config) is not VoiceRuntimeConfig:
            raise TypeError("config must be exact VoiceRuntimeConfig")
        frame_timeout = _finite_positive_seconds(frame_timeout_seconds)
        pulse_duration = _finite_positive_seconds(pulse_seconds)
        cleanup_retry_delay = _finite_positive_seconds(cleanup_retry_seconds)
        utterance_freshness = _finite_positive_seconds(utterance_freshness_seconds)
        min_stt_interval = (
            0.0
            if type(min_stt_interval_seconds) in (int, float)
            and min_stt_interval_seconds == 0
            else _finite_positive_seconds(min_stt_interval_seconds)
        )
        callables = (
            async_executor,
            source_factory,
            vad_factory,
            segmenter_factory,
            sleep,
            cleanup_retry_sleep,
            monotonic,
        )
        if (
            (snapshot_provider is not None and not callable(snapshot_provider))
            or (stream_url_provider is not None and not callable(stream_url_provider))
            or any(not callable(item) for item in callables)
            or (
                ffmpeg_binary is not None
                and (
                    type(ffmpeg_binary) is not str
                    or not ffmpeg_binary
                    or "\x00" in ffmpeg_binary
                )
            )
        ):
            raise ValueError("Invalid voice phrase manager configuration.")

        self._config = config
        self._snapshot_provider = snapshot_provider
        self._stream_url_provider = stream_url_provider
        self._ffmpeg_binary = ffmpeg_binary
        self._stt_client = stt_client
        self._async_executor = async_executor
        self._source_factory = source_factory
        self._vad_factory = vad_factory
        self._segmenter_factory = segmenter_factory
        self._sleep = sleep
        self._cleanup_retry_sleep = cleanup_retry_sleep
        self._frame_timeout_seconds = frame_timeout
        self._pulse_seconds = pulse_duration
        self._cleanup_retry_seconds = cleanup_retry_delay
        self._utterance_freshness_seconds = utterance_freshness
        self._min_stt_interval_seconds = min_stt_interval
        self._monotonic = monotonic
        # Retain one start boundary per target across source/worker replacement.
        self._next_stt_start: dict[str, float] = {}
        self._workers: dict[str, _Worker] = {}
        self._retiring_by_key: dict[str, asyncio.Task[None]] = {}
        self._all_worker_tasks: set[asyncio.Task[None]] = set()
        self._all_stt_tasks: set[asyncio.Task[None]] = set()
        self._all_timer_tasks: set[asyncio.Task[None]] = set()
        self._available = dict.fromkeys(config.target_keys, False)
        self._on = dict.fromkeys(config.target_keys, False)
        self._listeners: list[Listener] = []
        self._next_generation = 0
        self._started = False
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_outcome: list[BaseException] | None = None

    @property
    def config(self) -> VoiceRuntimeConfig:
        return self._config

    @property
    def worker_count(self) -> int:
        return sum(
            1
            for worker in self._workers.values()
            if worker.task is not None and not worker.task.done()
        )

    @property
    def in_flight_count(self) -> int:
        return sum(not task.done() for task in self._all_stt_tasks)

    @property
    def available_count(self) -> int:
        return sum(self._available.values())

    @property
    def on_count(self) -> int:
        return sum(self._on.values())

    @property
    def timer_count(self) -> int:
        return sum(not task.done() for task in self._all_timer_tasks)

    def configured_for(self, key: object, binding: object) -> bool:
        if type(key) is not str or type(binding) is not str:
            return False
        target = self._config.target_for(key)
        return target is not None and target.binding == binding

    def available_for(self, key: object) -> bool:
        return type(key) is str and self._available.get(key, False) is True

    def is_on_for(self, key: object) -> bool:
        return type(key) is str and self._on.get(key, False) is True

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("listener must be callable")
        self._listeners.append(listener)
        removed = False

        def remove() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    def __repr__(self) -> str:
        return (
            "VoicePhraseManager("
            f"configured={self._config.target_count}, workers={self.worker_count}, "
            f"available={self.available_count}, on={self.on_count}, "
            f"in_flight={self.in_flight_count})"
        )

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except BaseException as error:  # noqa: BLE001 - observers are isolated
                _sanitize_exception_chain(error)

    def _set_available(self, key: str, value: bool) -> None:
        if self._available.get(key) is value:
            return
        self._available[key] = value
        self._notify()

    def _cancel_pulse(
        self, worker: _Worker | None, *, notify: bool = True
    ) -> asyncio.Task[None] | None:
        task: asyncio.Task[None] | None = None
        if worker is not None:
            task = worker.pulse_task
            if task is not None:
                worker.pulse_task = None
                cleanup = worker.pulse_cleanup_task
                if cleanup is None or cleanup.done() or cleanup is task:
                    worker.pulse_cleanup_task = task
            else:
                task = worker.pulse_cleanup_task
            worker.pulse_token = None
            if task is not None:
                _cancel_owned_task_for_lifecycle(task)
        key = worker.target.key if worker is not None else None
        if key is not None and self._on.get(key) is True:
            self._on[key] = False
            if notify:
                self._notify()
        return task

    def _clear_target_state(
        self, key: str, worker: _Worker | None = None
    ) -> asyncio.Task[None] | None:
        changed = self._available.get(key) is True or self._on.get(key) is True
        task = self._cancel_pulse(worker, notify=False)
        self._available[key] = False
        self._on[key] = False
        if changed:
            self._notify()
        return task

    def _is_current(self, worker: _Worker) -> bool:
        return (
            self._started
            and self._workers.get(worker.target.key) is worker
            and self.configured_for(worker.target.key, worker.target.binding)
        )

    def _is_current_generation_epoch(
        self, worker: _Worker, generation: int, epoch: int
    ) -> bool:
        """Confirm synchronous notifications did not retire or replace a worker."""

        return (
            self._is_current(worker)
            and worker.generation == generation
            and worker.audio_epoch == epoch
        )

    def _has_infrastructure(self) -> bool:
        if not (
            self._config.enabled
            and self._snapshot_provider is not None
            and self._stream_url_provider is not None
            and self._ffmpeg_binary is not None
            and self._stt_client is not None
        ):
            return False
        try:
            transcribe = getattr(self._stt_client, "transcribe_pcm", None)
        except BaseException as error:  # noqa: BLE001 - dependency is non-authoritative
            _sanitize_exception_chain(error)
            return False
        return callable(transcribe)

    async def async_start(self) -> None:
        """Start eligible workers; repeated calls are idempotent."""

        if self._started:
            return
        stop_task = self._stop_task
        if stop_task is not None and not stop_task.done():
            await asyncio.shield(stop_task)
        self._started = True
        self.reconcile()

    def reconcile(self) -> None:
        """Synchronously reconcile exact trusted identities and proxy routes."""

        desired: dict[str, tuple[VoiceTargetConfig, str]] = {}
        if self._started and self._has_infrastructure():
            snapshot: Mapping[str, DiscoveredDoor] | None = None
            try:
                candidate = self._snapshot_provider()  # type: ignore[misc]
                if isinstance(candidate, Mapping):
                    snapshot = candidate
            except BaseException as error:  # noqa: BLE001 - snapshot fails closed
                _sanitize_exception_chain(error)
                snapshot = None
            if snapshot is not None:
                for target in self._config.targets:
                    try:
                        current = snapshot.get(target.key)
                    except BaseException as error:  # noqa: BLE001 - target fails closed
                        _sanitize_exception_chain(error)
                        continue
                    if (
                        type(current) is not DiscoveredDoor
                        or current.key != target.key
                        or not current.trusted
                        or current.binding != target.binding
                    ):
                        continue
                    try:
                        url = self._stream_url_provider(target.key)  # type: ignore[misc]
                    except BaseException as error:  # noqa: BLE001 - target fails closed
                        _sanitize_exception_chain(error)
                        continue
                    if _valid_loopback_proxy_url(url, target.key):
                        assert type(url) is str
                        desired[target.key] = (target, url)

        for key, worker in tuple(self._workers.items()):
            wanted = desired.get(key)
            if wanted is not None and (
                worker.target.binding == wanted[0].binding and worker.url == wanted[1]
            ):
                desired.pop(key)
                continue
            self._retire_worker(key, worker)

        for key, (target, url) in desired.items():
            if key in self._workers:
                continue
            predecessor = self._retiring_by_key.get(key)
            if predecessor is not None:
                if not predecessor.done():
                    continue
                if self._retiring_by_key.get(key) is predecessor:
                    self._retiring_by_key.pop(key, None)
                self._consume_task(predecessor)
                if key in self._retiring_by_key:
                    continue
            self._launch_worker(target, url)

    def _reconcile_quarantined(self) -> None:
        """Run owned callback reconciliation without exposing terminal failures."""

        try:
            self.reconcile()
        except BaseException as error:  # noqa: BLE001 - loop callback quarantine
            _sanitize_exception_chain(error)

    def _consume_task(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:  # noqa: BLE001, S110 - consume task outcome
            pass

    def _track_task(
        self, task: asyncio.Task[None], collection: set[asyncio.Task[None]]
    ) -> None:
        collection.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            collection.discard(completed)
            self._consume_task(completed)

        _TASK_ADD_DONE_CALLBACK(task, done)

    def _launch_worker(self, target: VoiceTargetConfig, url: str) -> None:
        self._next_generation += 1
        worker = _Worker(target, url, self._next_generation)
        self._workers[target.key] = worker
        task = _create_owned_task(_quarantine_task(self._worker_loop, (worker,)))
        if task is None:
            if self._workers.get(target.key) is worker:
                self._workers.pop(target.key, None)
            self._clear_target_state(target.key, worker)
            return
        worker.task = task
        self._track_task(task, self._all_worker_tasks)

        def done(completed: asyncio.Task[None]) -> None:
            if self._workers.get(target.key) is worker:
                self._workers.pop(target.key, None)
                self._clear_target_state(target.key, worker)

        task.add_done_callback(done)

    def _retire_worker(self, key: str, worker: _Worker) -> None:
        if self._workers.get(key) is worker:
            self._workers.pop(key, None)
        worker.audio_epoch += 1
        task = worker.task
        if task is not None and not task.done():
            self._retiring_by_key[key] = task

            def retired(completed: asyncio.Task[None]) -> None:
                if self._retiring_by_key.get(key) is completed:
                    self._retiring_by_key.pop(key, None)
                    self._reconcile_quarantined()

            task.add_done_callback(retired)
        if task is not None:
            _cancel_owned_task_for_lifecycle(task)
        self._clear_target_state(key, worker)

    def _cancel_stt(self, worker: _Worker) -> asyncio.Task[None] | None:
        task = worker.stt_task
        if task is not None:
            _cancel_owned_task_for_lifecycle(task)
        return task

    async def _await_stt_terminal(
        self,
        worker: _Worker,
        task: asyncio.Task[None] | None,
        interruption: BaseException | None,
    ) -> BaseException | None:
        """Await worker-owned STT completion while preserving first interruption."""

        if task is None or task is asyncio.current_task():
            return interruption
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                current_task = asyncio.current_task()
                if (
                    current_task is not None
                    and current_task.cancelling()
                    and interruption is None
                ):
                    interruption = error
            except BaseException:  # noqa: BLE001, S110 - consume outcome below
                pass
        self._consume_task(task)
        if worker.stt_task is task:
            worker.stt_task = None
        return interruption

    async def _await_pulse_terminal(
        self,
        worker: _Worker,
        task: asyncio.Task[None] | None,
        interruption: BaseException | None,
    ) -> BaseException | None:
        """Cancel and own one pulse task until terminal, retaining first control flow."""

        if task is None or task is asyncio.current_task():
            return interruption
        _cancel_owned_task_for_lifecycle(task)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                current_task = asyncio.current_task()
                if (
                    current_task is not None
                    and current_task.cancelling()
                    and interruption is None
                ):
                    interruption = error
            except BaseException:  # noqa: BLE001, S110 - consume outcome below
                pass
        self._consume_task(task)
        self._all_timer_tasks.discard(task)
        if worker.pulse_task is task:
            worker.pulse_task = None
        if worker.pulse_cleanup_task is task:
            worker.pulse_cleanup_task = None
        return interruption

    async def _await_cleanup_retry(
        self,
        interruption: BaseException | None,
        sleeper: Sleep,
    ) -> tuple[BaseException | None, Sleep]:
        """Wait once between close attempts while retaining first interruption."""

        while True:
            try:
                await sleeper(self._cleanup_retry_seconds)
                return interruption, sleeper
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
                sleeper = asyncio.sleep
            except Exception:  # noqa: BLE001 - use fail-safe real pacing
                sleeper = asyncio.sleep
            except BaseException as error:  # noqa: BLE001 - defer control flow
                if interruption is None:
                    interruption = error
                sleeper = asyncio.sleep

    async def _close_source(
        self,
        source: _PcmSource | None,
        interruption: BaseException | None,
    ) -> BaseException | None:
        """Retry the same source until close is verified, without exposing errors."""

        if source is None:
            return interruption
        sleeper = self._cleanup_retry_sleep
        while True:
            try:
                await source.async_close()
                return interruption
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
            except Exception:  # noqa: BLE001, S110 - target-local, no normal logging
                pass
            except BaseException as error:  # noqa: BLE001 - defer control flow
                if interruption is None:
                    interruption = error
            interruption, sleeper = await self._await_cleanup_retry(
                interruption, sleeper
            )

    def _source_read_failed(self, worker: _Worker, generation: int, epoch: int) -> None:
        """Revoke this source's evidence synchronously, before its slow reap."""

        if not self._is_current_generation_epoch(worker, generation, epoch):
            return
        worker.audio_epoch += 1
        self._cancel_stt(worker)
        self._clear_target_state(worker.target.key, worker)

    async def _worker_loop(self, worker: _Worker) -> None:
        failures = 0
        while self._is_current(worker):
            worker.audio_epoch += 1
            epoch = worker.audio_epoch
            source: _PcmSource | None = None
            healthy = False
            interruption: BaseException | None = None
            try:
                source = self._source_factory(self._ffmpeg_binary, worker.url)  # type: ignore[arg-type]
                if isinstance(source, FfmpegPcmSource):
                    source.set_read_failure_callback(
                        lambda generation=worker.generation, epoch=epoch: (
                            self._source_read_failed(worker, generation, epoch)
                        )
                    )
                vad = self._vad_factory()
                segmenter = self._segmenter_factory()
                await source.async_start()
                while self._is_current(worker):
                    frame = await asyncio.wait_for(
                        source.async_read_frame(), timeout=self._frame_timeout_seconds
                    )
                    if frame is None:
                        raise RuntimeError
                    probability = vad.process(frame)
                    if probability is None:
                        continue
                    pcm = segmenter.process(frame, probability)
                    deadline = (
                        self._monotonic() + self._utterance_freshness_seconds
                        if pcm is not None
                        else None
                    )
                    healthy = True
                    failures = 0
                    if not worker.stt_failed:
                        self._set_available(worker.target.key, True)
                    task = worker.stt_task
                    if pcm is not None and (task is None or task.done()):
                        self._start_stt(worker, epoch, pcm, deadline)
            except asyncio.CancelledError as error:
                interruption = error
            except Exception:  # noqa: BLE001, S110 - isolated, no normal logging
                pass
            except BaseException as error:  # noqa: BLE001 - defer through cleanup
                interruption = error
            finally:
                worker.audio_epoch += 1
                stt_task = self._cancel_stt(worker)
                if self._is_current(worker):
                    try:
                        self._clear_target_state(worker.target.key, worker)
                    except BaseException as error:  # noqa: BLE001 - defer cleanup flow
                        if interruption is None:
                            interruption = error
                pulse_task = worker.pulse_cleanup_task or worker.pulse_task
                interruption = await self._close_source(source, interruption)
                interruption = await self._await_stt_terminal(
                    worker, stt_task, interruption
                )
                if worker.pulse_cleanup_task is not None:
                    pulse_task = worker.pulse_cleanup_task
                interruption = await self._await_pulse_terminal(
                    worker, pulse_task, interruption
                )
            if interruption is not None:
                _raise_sanitized(interruption)
            if not self._is_current(worker):
                return
            if healthy:
                failures = 1
            else:
                failures = min(failures + 1, 6)
            delay = min(float(1 << min(failures - 1, 5)), MAX_RECONNECT_SECONDS)
            await self._sleep(delay)

    def _start_stt(
        self, worker: _Worker, epoch: int, pcm: bytes, deadline: float | None = None
    ) -> None:
        now = self._monotonic()
        if deadline is None:
            deadline = now + self._utterance_freshness_seconds
        # Drop surplus immediately: capture never sleeps or queues for rate pacing.
        if now >= deadline or now < self._next_stt_start.get(
            worker.target.key, -math.inf
        ):
            return
        task = _create_owned_task(
            _quarantine_task(self._run_stt, (worker, epoch, pcm, deadline))
        )
        if task is None:
            raise RuntimeError from None
        worker.stt_task = task
        self._track_task(task, self._all_stt_tasks)

        def done(completed: asyncio.Task[None]) -> None:
            if worker.stt_task is completed:
                worker.stt_task = None

        task.add_done_callback(done)

    async def _run_stt(
        self, worker: _Worker, epoch: int, pcm: bytes, deadline: float | None = None
    ) -> None:
        text: str | None = None
        failure: BaseException | None = None
        generation = worker.generation
        now = self._monotonic()
        if deadline is None:
            deadline = now + self._utterance_freshness_seconds
        if now >= deadline:
            return
        freshness_timeout = asyncio.timeout(deadline - now)
        try:
            # One deadline covers queue wait, actual request and owned matcher work.
            # Timeout cancels waiting admission independently of other slow requests.
            async with freshness_timeout, _loop_limiter():
                if not self._is_current_generation_epoch(worker, generation, epoch):
                    return
                now = self._monotonic()
                if now >= deadline or now < self._next_stt_start.get(
                    worker.target.key, -math.inf
                ):
                    return
                self._next_stt_start[worker.target.key] = (
                    now + self._min_stt_interval_seconds
                )
                text = await self._stt_client.transcribe_pcm(pcm)  # type: ignore[union-attr]
                if self._monotonic() >= deadline:
                    return
                if type(text) is not str:
                    raise ValueError
                try:
                    size = len(text.encode("utf-8", errors="strict"))
                except UnicodeEncodeError:
                    raise ValueError from None
                if size > MAX_TRANSCRIPT_UTF8_BYTES:
                    raise ValueError
                # Own adapters (including to_thread) until their terminal outcome so
                # cancellation cannot release the shared semaphore around hidden work.
                cancellation: asyncio.CancelledError | None = None
                executor_outcome: list[tuple[bool, object]] = []
                executor_task = _create_owned_task(
                    _capture_task_outcome(
                        self._async_executor,
                        (worker.target.matches, text),
                        executor_outcome,
                    )
                )
                if executor_task is None:
                    raise RuntimeError from None
                current_task = asyncio.current_task()
                if executor_task is current_task:
                    raise RuntimeError
                while not executor_task.done():
                    try:
                        await asyncio.shield(executor_task)
                    except asyncio.CancelledError as error:
                        if (
                            current_task is not None
                            and current_task.cancelling()
                            and cancellation is None
                        ):
                            cancellation = error
                    except BaseException:  # noqa: BLE001, S110 - consume below
                        pass
                executor_task.result()
                if len(executor_outcome) != 1:
                    raise RuntimeError
                succeeded, matched = executor_outcome.pop()
                if not succeeded:
                    if cancellation is not None:
                        raise cancellation
                    if not issubclass(type(matched), BaseException):
                        raise RuntimeError
                    raise cast(BaseException, matched)
                if cancellation is not None:
                    raise cancellation
                if type(matched) is not bool:
                    raise ValueError
            if (
                not self._is_current_generation_epoch(worker, generation, epoch)
                or self._monotonic() >= deadline
            ):
                return
            worker.stt_failed = False
            self._set_available(worker.target.key, True)
            if (
                not self._is_current_generation_epoch(worker, generation, epoch)
                or self._monotonic() >= deadline
            ):
                return
            if matched:
                await self._start_pulse(worker)
        except TimeoutError as error:
            # Expiry drops evidence; it is not an audio-source health failure.
            if not freshness_timeout.expired():
                failure = error
        except BaseException as error:  # noqa: BLE001 - defer outside exception boundary
            failure = error
        finally:
            text = None
            del text

        if failure is None:
            return
        interruption = failure if not _is_ordinary_exception(failure) else None
        pulse_task = worker.pulse_cleanup_task or worker.pulse_task
        if self._is_current_generation_epoch(worker, generation, epoch):
            worker.stt_failed = True
            try:
                pulse_task = (
                    self._clear_target_state(worker.target.key, worker) or pulse_task
                )
            except BaseException as cleanup_error:  # noqa: BLE001 - first wins
                if interruption is None and not _is_ordinary_exception(cleanup_error):
                    interruption = cleanup_error
        if worker.pulse_cleanup_task is not None:
            pulse_task = worker.pulse_cleanup_task
        interruption = await self._await_pulse_terminal(
            worker, pulse_task, interruption
        )
        if interruption is not None:
            deferred = interruption
            failure = None
            interruption = None
            _raise_sanitized(deferred)

    def _rollback_pulse_start(
        self, worker: _Worker, generation: int, token: object
    ) -> None:
        """Synchronously clear only the exact pulse whose timer was rejected."""

        if (
            self._workers.get(worker.target.key) is not worker
            or worker.generation != generation
            or worker.pulse_token is not token
        ):
            return
        worker.pulse_token = None
        if self._on.get(worker.target.key) is True:
            self._on[worker.target.key] = False
            self._notify()

    def _pulse_start_failure_is_stale(
        self,
        worker: _Worker,
        generation: int,
        epoch: int,
        rejected_token: object,
    ) -> bool:
        """Recognize only retirement or a fully live reentrant successor pulse."""

        if not self._is_current_generation_epoch(worker, generation, epoch):
            return True
        successor_token = worker.pulse_token
        successor_task = worker.pulse_task
        if (
            successor_token is None
            or successor_token is rejected_token
            or type(successor_task) is not _BUILTIN_TASK_TYPE
        ):
            return False
        return successor_task in self._all_timer_tasks and _is_pristine_live_task(
            successor_task, loop=asyncio.get_running_loop()
        )

    async def _start_pulse(self, worker: _Worker) -> None:
        generation = worker.generation
        epoch = worker.audio_epoch
        if not self._is_current_generation_epoch(worker, generation, epoch):
            return
        old_task = worker.pulse_task
        token = object()
        worker.pulse_token = token
        if self._on.get(worker.target.key) is not True:
            self._on[worker.target.key] = True
            self._notify()
        if (
            not self._is_current_generation_epoch(worker, generation, epoch)
            or worker.pulse_token is not token
        ):
            return
        if old_task is not None:
            interruption = await self._await_pulse_terminal(worker, old_task, None)
            if interruption is not None:
                _raise_sanitized(interruption)
            if (
                not self._is_current_generation_epoch(worker, generation, epoch)
                or worker.pulse_token is not token
            ):
                return
        task = _create_owned_task(_quarantine_task(self._pulse_timer, (worker, token)))
        if (
            task is not None
            and _is_pristine_live_task(task, loop=asyncio.get_running_loop())
            and (
                not self._is_current_generation_epoch(worker, generation, epoch)
                or worker.pulse_token is not token
            )
        ):
            _fail_closed_owned_task(task)
            self._rollback_pulse_start(worker, generation, token)
            return
        if task is None or not _is_pristine_live_task(
            task, loop=asyncio.get_running_loop()
        ):
            if task is not None:
                _fail_closed_owned_task(task)
            self._rollback_pulse_start(worker, generation, token)
            if self._pulse_start_failure_is_stale(worker, generation, epoch, token):
                return
            raise RuntimeError from None
        worker.pulse_task = task

        def done(completed: asyncio.Task[None]) -> None:
            if worker.pulse_task is completed:
                worker.pulse_task = None

        tracking_failed = False
        try:
            self._track_task(task, self._all_timer_tasks)
            _TASK_ADD_DONE_CALLBACK(task, done)
            tracking_failed = (
                worker.pulse_task is not task
                or task not in self._all_timer_tasks
                or not _is_pristine_live_task(task, loop=asyncio.get_running_loop())
            )
        except BaseException as error:  # noqa: BLE001 - tracking fails closed
            tracking_failed = True
            _sanitize_exception_chain(error)
        if tracking_failed:
            if worker.pulse_task is task:
                worker.pulse_task = None
            try:
                self._all_timer_tasks.discard(task)
            except BaseException as error:  # noqa: BLE001 - best-effort detachment
                _sanitize_exception_chain(error)
            _fail_closed_owned_task(task)
            self._rollback_pulse_start(worker, generation, token)
            if self._pulse_start_failure_is_stale(worker, generation, epoch, token):
                return
            raise RuntimeError from None

    async def _pulse_timer(self, worker: _Worker, token: object) -> None:
        try:
            await self._sleep(self._pulse_seconds)
        finally:
            if self._is_current(worker) and worker.pulse_token is token:
                worker.pulse_token = None
                if self._on.get(worker.target.key) is True:
                    self._on[worker.target.key] = False
                    self._notify()

    async def _cleanup(self) -> None:
        workers = tuple(self._workers.items())
        self._workers.clear()
        interruption: BaseException | None = None
        for key, worker in workers:
            worker.audio_epoch += 1
            try:
                self._clear_target_state(key, worker)
            except BaseException as error:  # noqa: BLE001 - finish all cleanup first
                if interruption is None:
                    interruption = error
            if worker.task is not None:
                _cancel_owned_task_for_lifecycle(worker.task)
        for task in tuple(self._all_stt_tasks):
            _cancel_owned_task_for_lifecycle(task)
        for task in tuple(self._all_timer_tasks):
            _cancel_owned_task_for_lifecycle(task)
        for task in tuple(self._retiring_by_key.values()):
            _cancel_owned_task_for_lifecycle(task)

        pending = {
            *self._all_worker_tasks,
            *self._all_stt_tasks,
            *self._all_timer_tasks,
            *self._retiring_by_key.values(),
        }
        if pending:
            gathering = asyncio.gather(*pending, return_exceptions=True)
            cancellation: asyncio.CancelledError | None = None
            while not gathering.done():
                try:
                    await asyncio.shield(gathering)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
                    else:
                        _sanitize_exception_chain(error)
                except BaseException as error:  # noqa: BLE001 - consume below
                    _sanitize_exception_chain(error)
            try:
                gathering.result()
            except BaseException as error:  # noqa: BLE001 - first control flow wins
                _sanitize_exception_chain(error)
                if interruption is None:
                    interruption = error
        else:
            cancellation = None
        self._all_worker_tasks.clear()
        self._all_stt_tasks.clear()
        self._all_timer_tasks.clear()
        changed = any(self._available.values()) or any(self._on.values())
        self._available = dict.fromkeys(self._config.target_keys, False)
        self._on = dict.fromkeys(self._config.target_keys, False)
        if changed:
            self._notify()
        self._retiring_by_key.clear()
        self._listeners.clear()
        if cancellation is not None:
            if interruption is not None:
                _sanitize_exception_chain(interruption)
            deferred = cancellation
            cancellation = None
            interruption = None
            gathering = None
            pending = set()
            del workers, self
            _raise_sanitized(deferred)
        if interruption is not None:
            _raise_sanitized(interruption)

    async def async_stop(self) -> None:
        """Cancel and await all work, preserving caller cancellation after cleanup."""

        current = asyncio.current_task()
        entry_cancellation_baseline = current.cancelling() if current is not None else 0
        self._started = False
        cleanup = self._stop_task
        cleanup_outcome = self._stop_outcome
        if cleanup is None:
            cleanup_outcome = []
            cleanup = _create_owned_task(
                _cleanup_owned_task(self._cleanup, (), cleanup_outcome)
            )
            if cleanup is None:
                cleanup_outcome = None
                caller_cancellation: asyncio.CancelledError | None = None
                cleanup_error: BaseException | None = None
                cancellation_count = 0
                cancellation_delta = 0
                while True:
                    try:
                        # Keep synchronous cleanup failures from monopolizing the loop.
                        await _inline_stop_checkpoint()
                        await self._cleanup()
                    except asyncio.CancelledError as error:
                        cancellation_count = (
                            current.cancelling() if current is not None else 0
                        )
                        cancellation_delta = max(
                            0, cancellation_count - entry_cancellation_baseline
                        )
                        if current is not None and cancellation_delta > 0:
                            if caller_cancellation is None:
                                _sanitize_exception_chain(error)
                                caller_cancellation = error
                                for _ in range(cancellation_delta):
                                    current.uncancel()
                                current._cancel_message = None  # type: ignore[attr-defined]
                            else:
                                _sanitize_owned_cancellation(
                                    error, current, cancellation_delta
                                )
                        else:
                            _sanitize_exception_chain(error)
                            if cleanup_error is None:
                                cleanup_error = error
                    except BaseException as error:  # noqa: BLE001 - terminal retry
                        _sanitize_exception_chain(error)
                        if cleanup_error is None:
                            cleanup_error = error
                    else:
                        break
                current = None
                cancellation_count = 0
                cancellation_delta = 0
                if caller_cancellation is not None:
                    deferred = caller_cancellation
                    caller_cancellation = None
                    if cleanup_error is not None:
                        _sanitize_exception_chain(cleanup_error)
                    cleanup_error = None
                    cleanup = None
                    del self
                    _raise_sanitized(deferred)
                if cleanup_error is not None:
                    deferred = cleanup_error
                    cleanup_error = None
                    cleanup = None
                    del self
                    _raise_sanitized(deferred)
                return
            self._stop_outcome = cleanup_outcome
            self._stop_task = cleanup

        current = None
        entry_cancellation_baseline = 0
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
                else:
                    _sanitize_exception_chain(error)
        cleanup_error: BaseException | None = None
        try:
            cleanup.result()
        except BaseException as error:  # noqa: BLE001 - cancellation wins after cleanup
            cleanup_error = error
        if cleanup_error is None and cleanup_outcome:
            cleanup_error = cleanup_outcome[0]
        if self._stop_task is cleanup:
            self._stop_task = None
            self._stop_outcome = None
        if cancellation is not None:
            deferred = cancellation
            cancellation = None
            if cleanup_error is not None:
                _sanitize_exception_chain(cleanup_error)
            cleanup_error = None
            cleanup = None
            cleanup_outcome = None
            del self
            _raise_sanitized(deferred)
        if cleanup_error is not None:
            deferred = cleanup_error
            cleanup_error = None
            cleanup = None
            cleanup_outcome = None
            del self
            _raise_sanitized(deferred)
