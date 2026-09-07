"""Synthetic contract tests for the pure per-device voice runtime."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import gc
import inspect
import threading
import warnings
import weakref
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

import pytest

from custom_components.ufanet_intercom import voice_runtime
from custom_components.ufanet_intercom.const import DiscoveredDoor
from custom_components.ufanet_intercom.voice_audio import (
    END_SILENCE_FRAMES,
    FRAME_BYTES,
    FRAME_SAMPLES,
    MIN_VOICED_FRAMES,
)
from custom_components.ufanet_intercom.voice_phrase import encode_phrase_set
from custom_components.ufanet_intercom.voice_runtime import (
    MAX_VOICE_TARGETS,
    VOICE_PHRASE_OPTIONS_ROOT,
    VOICE_PHRASE_STORAGE_VERSION,
    VoicePhraseConfigError,
    VoicePhraseManager,
    VoiceRuntimeConfig,
    parse_voice_phrase_options,
)

KEY_A = "a" * 64
KEY_B = "b" * 64
KEY_C = "c" * 64
BINDING_A = "1" * 64
BINDING_B = "2" * 64
BINDING_C = "3" * 64
ENDPOINT = "https://stt.invalid/v1/audio/transcriptions"
TOKEN = "PRIVATE-STT-TOKEN"
MODEL = "PRIVATE-STT-MODEL"
PHRASE = "СОВЕРШЕННО СЕКРЕТНАЯ ФРАЗА"
VOICE = b"\x01\x00" * FRAME_SAMPLES
SILENCE = bytes(FRAME_BYTES)
PUBLIC_ERROR = "Invalid voice phrase runtime configuration."


class IntLookalike(int):
    pass


class FloatLookalike(float):
    pass


def door(
    key: str = KEY_A,
    binding: str = BINDING_A,
    *,
    trusted: bool = True,
) -> DiscoveredDoor:
    """Build one valid synthetic discovery object."""

    return DiscoveredDoor(
        key=key,
        shared_id={KEY_A: 1, KEY_B: 2, KEY_C: 3}.get(key, 8),
        door=0,
        model=7,
        display_name="Synthetic",
        binding=binding,
        openable=True,
        trusted=trusted,
    )


def phrase_storage(phrase: str = PHRASE) -> dict[str, object]:
    """Build protected storage through the approved phrase encoder."""

    return encode_phrase_set([phrase], salt=bytes(range(16)))


def enabled_root(
    targets: Mapping[str, tuple[str, dict[str, object]]] | None = None,
) -> dict[str, object]:
    """Build canonical enabled version-one storage."""

    actual = {KEY_A: (BINDING_A, phrase_storage())} if targets is None else targets
    return {
        "version": 1,
        "enabled": True,
        "endpoint": ENDPOINT,
        "token": TOKEN,
        "model": MODEL,
        "allow_insecure_http": False,
        "targets": {
            key: {"binding": binding, "phrases": phrases}
            for key, (binding, phrases) in actual.items()
        },
    }


def parse_enabled(
    targets: Mapping[str, tuple[str, dict[str, object]]] | None = None,
) -> VoiceRuntimeConfig:
    return parse_voice_phrase_options(
        {VOICE_PHRASE_OPTIONS_ROOT: enabled_root(targets)}
    )


def assert_fixed_config_error(operation: Callable[[], object], *secrets: str) -> None:
    with pytest.raises(VoicePhraseConfigError) as caught:
        operation()
    error = caught.value
    assert type(error) is VoicePhraseConfigError
    assert error.args == (PUBLIC_ERROR,)
    assert str(error) == PUBLIC_ERROR
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []
    for secret in secrets:
        assert secret not in str(error)
        assert secret not in repr(error)


def _contains_protected_referent(
    value: object,
    protected_ids: set[int],
    protected_values: set[str | bytes],
    seen: set[int] | None = None,
) -> bool:
    """Inspect only inert built-in referents without calling application hooks."""

    if id(value) in protected_ids:
        return True
    if type(value) in (str, bytes):
        return value in protected_values
    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, Mapping):
        return any(
            _contains_protected_referent(item, protected_ids, protected_values, seen)
            for pair in value.items()
            for item in pair
        )
    if type(value) in (list, tuple, set, frozenset):
        return any(
            _contains_protected_referent(item, protected_ids, protected_values, seen)
            for item in value
        )
    if issubclass(type(value), BaseException):
        referents = [
            physical_exception_slot(value, "__context__"),
            physical_exception_slot(value, "__cause__"),
            physical_exception_dict(value),
        ]
        try:
            referents.append(
                BaseException.__dict__["args"].__get__(value, BaseException)
            )
        except BaseException:  # noqa: BLE001, S110 - avoid hostile hooks
            pass
        return any(
            _contains_protected_referent(item, protected_ids, protected_values, seen)
            for item in referents
            if item is not None
        )
    return False


def assert_terminal_owned_task_scrubbed(
    task: asyncio.Task[None],
    protected_ids: set[int] | None = None,
    protected_values: set[str | bytes] | None = None,
) -> None:
    """Prove one owned Task has no terminal cancellation or private referents."""

    assert task.done() is True
    assert task.cancelled() is False
    assert task.exception() is None
    assert task.result() is None
    assert task.cancelling() == 0
    assert getattr(task, "_cancel_message", None) is None
    assert task.get_stack() == []
    assert getattr(task.get_coro(), "cr_frame", None) is None
    assert not _contains_protected_referent(
        gc.get_referents(task),
        set() if protected_ids is None else protected_ids,
        set() if protected_values is None else protected_values,
    )


@pytest.mark.parametrize("failure_stage", ["early", "middle", "late"])
def test_parser_error_traceback_cannot_reach_private_input_at_any_stage(
    failure_stage: str,
) -> None:
    """The public traceback owns no failed private parser frame or input local."""

    endpoint = "https://trace-private.invalid/v1/" + failure_stage
    token = "TRACE-PRIVATE-TOKEN-" + failure_stage
    model = "TRACE-PRIVATE-MODEL-" + failure_stage
    key = ("4" if failure_stage != "early" else "5") * 64
    binding = ("6" if failure_stage != "early" else "7") * 64
    storage = phrase_storage("trace private phrase " + failure_stage)
    root = enabled_root({key: (binding, storage)})
    root.update({"endpoint": endpoint, "token": token, "model": model})
    if failure_stage == "early":
        root["unexpected"] = object()
    elif failure_stage == "middle":
        root["allow_insecure_http"] = 1
    else:
        digests = storage["digests"]
        assert type(digests) is list
        digests[0] = "late-private-invalid-digest".ljust(44, "x")
    private_sentinel = object()
    options = {
        VOICE_PHRASE_OPTIONS_ROOT: root,
        "ignored-private-sentinel": private_sentinel,
    }
    protected_objects = (options, root, storage, private_sentinel)
    encoded_salt = storage["salt"]
    encoded_digests = storage["digests"]
    assert type(encoded_salt) is str
    assert type(encoded_digests) is list
    assert all(type(value) is str for value in encoded_digests)
    protected_values: set[str | bytes] = {
        endpoint,
        token,
        model,
        key,
        binding,
        encoded_salt,
        *(value for value in encoded_digests if type(value) is str),
    }

    caught_error: VoicePhraseConfigError | None = None
    traceback = None
    try:
        parse_voice_phrase_options(options)
    except VoicePhraseConfigError as error:
        caught_error = error
        traceback = physical_exception_slot(error, "__traceback__")
    else:
        pytest.fail("malformed private configuration was accepted")

    assert caught_error is not None
    runtime_frames = []
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename == voice_runtime.__file__:
            runtime_frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert runtime_frames
    assert "_parse_voice_phrase_options" not in {
        frame.f_code.co_name for frame in runtime_frames
    }
    protected_ids = {id(value) for value in protected_objects}
    for frame in runtime_frames:
        assert not _contains_protected_referent(
            frame.f_locals, protected_ids, protected_values
        )
    assert not _contains_protected_referent(
        caught_error, protected_ids, protected_values
    )


def _parse_while_handling_nested_private_options(
    options: object,
) -> VoicePhraseConfigError:
    """Return a config error raised within a private nested caller chain."""

    caught_config: VoicePhraseConfigError | None = None
    try:
        try:
            raise KeyError(options)
        except KeyError as first:
            raise LookupError(options) from first
    except LookupError as second:
        try:
            raise RuntimeError(options) from second
        except RuntimeError:
            try:
                parse_voice_phrase_options(options)
            except VoicePhraseConfigError as error:
                caught_config = error
    options = None
    del options
    if caught_config is None:
        pytest.fail("malformed private configuration was accepted")
    assert caught_config is not None
    return caught_config


@pytest.mark.parametrize("failure_stage", ["early", "middle", "late"])
def test_parser_error_physically_clears_nested_private_caller_context(
    failure_stage: str,
) -> None:
    """Caller exception graphs cannot remain reachable from a public error."""

    endpoint = "https://caller-private.invalid/v1/" + failure_stage
    token = "CALLER-PRIVATE-TOKEN-" + failure_stage
    model = "CALLER-PRIVATE-MODEL-" + failure_stage
    key = ("8" if failure_stage == "early" else "9") * 64
    binding = ("a" if failure_stage == "middle" else "b") * 64
    storage = phrase_storage("caller private phrase " + failure_stage)
    root = enabled_root({key: (binding, storage)})
    root.update({"endpoint": endpoint, "token": token, "model": model})
    if failure_stage == "early":
        root["unexpected"] = object()
    elif failure_stage == "middle":
        root["allow_insecure_http"] = 1
    else:
        digests = storage["digests"]
        assert type(digests) is list
        digests[0] = "late-caller-private-digest".ljust(44, "x")
    private_sentinel = object()
    options = {
        VOICE_PHRASE_OPTIONS_ROOT: root,
        "ignored-caller-private-sentinel": private_sentinel,
    }
    encoded_salt = storage["salt"]
    encoded_digests = storage["digests"]
    assert type(encoded_salt) is str
    assert type(encoded_digests) is list
    assert all(type(value) is str for value in encoded_digests)
    protected_objects = (options, root, storage, encoded_digests, private_sentinel)
    protected_ids = {id(value) for value in protected_objects}
    protected_values: set[str | bytes] = {
        endpoint,
        token,
        model,
        key,
        binding,
        encoded_salt,
        *(value for value in encoded_digests if type(value) is str),
    }

    error = _parse_while_handling_nested_private_options(options)

    assert type(error) is VoicePhraseConfigError
    assert BaseException.__dict__["args"].__get__(error, BaseException) == (
        PUBLIC_ERROR,
    )
    assert str(error) == PUBLIC_ERROR
    assert repr(error) == f"VoicePhraseConfigError({PUBLIC_ERROR!r})"
    assert physical_exception_slot(error, "__context__") is None
    assert physical_exception_slot(error, "__cause__") is None
    notes = physical_exception_dict(error).get("__notes__")
    assert notes is None or notes == []

    pending: list[BaseException] = [error]
    exception_graph: list[BaseException] = []
    seen_errors: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen_errors:
            continue
        seen_errors.add(id(current))
        exception_graph.append(current)
        for slot in ("__context__", "__cause__"):
            linked = physical_exception_slot(current, slot)
            if isinstance(linked, BaseException):
                pending.append(linked)

    assert exception_graph == [error]
    for current in exception_graph:
        assert not _contains_protected_referent(
            current, protected_ids, protected_values
        )
        for traceback in physical_traceback_chain(current):
            assert not _contains_protected_referent(
                traceback.tb_frame.f_locals, protected_ids, protected_values
            )
        assert "_parse_voice_phrase_options" not in {
            traceback.tb_frame.f_code.co_name
            for traceback in physical_traceback_chain(current)
        }


def test_storage_constants_missing_root_and_canonical_disabled_contract() -> None:
    assert VOICE_PHRASE_OPTIONS_ROOT == "voice_phrase"
    assert VOICE_PHRASE_STORAGE_VERSION == 1
    assert MAX_VOICE_TARGETS == 8

    absent = parse_voice_phrase_options({"unrelated": object()})
    disabled = parse_voice_phrase_options(
        {VOICE_PHRASE_OPTIONS_ROOT: {"version": 1, "enabled": False}}
    )

    assert absent is not disabled
    assert absent != disabled
    assert disabled.enabled is False
    assert disabled.target_count == 0
    assert disabled.target_keys == ()
    assert disabled.configured_targets == ()
    assert disabled.stt_config is None
    assert repr(disabled) == (
        "VoiceRuntimeConfig(enabled=False, target_count=0, phrase_count=0)"
    )


@pytest.mark.parametrize(
    ("stored_options", "expected_enabled", "expected_target_count"),
    [
        ({"unrelated": {"sentinel": [1]}}, False, 0),
        (
            {VOICE_PHRASE_OPTIONS_ROOT: {"version": 1, "enabled": False}},
            False,
            0,
        ),
        ({VOICE_PHRASE_OPTIONS_ROOT: enabled_root()}, True, 1),
    ],
    ids=("missing-root", "canonical-disabled", "valid-enabled"),
)
def test_parser_accepts_home_assistant_mappingproxy_options_without_mutation(
    stored_options: dict[str, object],
    expected_enabled: bool,
    expected_target_count: int,
) -> None:
    before = deepcopy(stored_options)
    options = MappingProxyType(stored_options)

    config = parse_voice_phrase_options(options)

    assert config.enabled is expected_enabled
    assert config.target_count == expected_target_count
    assert stored_options == before


@pytest.mark.parametrize(
    "root",
    [
        None,
        True,
        [],
        {},
        {"version": True, "enabled": False},
        {"version": 2, "enabled": False},
        {"version": 1, "enabled": 0},
        {"version": 1, "enabled": False, "targets": {}},
        {"version": 1, "enabled": True},
        {**enabled_root(), "extra": False},
        {**enabled_root(), "allow_insecure_http": 0},
        {**enabled_root(), "targets": []},
        {**enabled_root(), "targets": {}},
    ],
)
def test_parser_rejects_noncanonical_roots_exact_types_and_extras(root: object) -> None:
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root})
    )


def test_parser_rejects_options_and_container_subclasses() -> None:
    class DictLookalike(dict[str, object]):
        pass

    class MappingLookalike(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self):
            return iter(())

        def __len__(self) -> int:
            return 0

    assert_fixed_config_error(lambda: parse_voice_phrase_options(None))
    assert_fixed_config_error(lambda: parse_voice_phrase_options(True))
    assert_fixed_config_error(lambda: parse_voice_phrase_options([]))
    assert_fixed_config_error(lambda: parse_voice_phrase_options(DictLookalike()))
    assert_fixed_config_error(lambda: parse_voice_phrase_options(MappingLookalike()))
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options(
            {VOICE_PHRASE_OPTIONS_ROOT: DictLookalike(enabled_root())}
        )
    )
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options(
            {VOICE_PHRASE_OPTIONS_ROOT: MappingProxyType(enabled_root())}
        )
    )
    root = enabled_root()
    root["targets"] = DictLookalike(root["targets"])  # type: ignore[arg-type]
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root})
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", None),
        ("endpoint", "http://example.com/private"),
        ("endpoint", "x" * 1_000_000),
        ("token", None),
        ("token", "x" * 2049),
        ("model", None),
        ("model", "x" * 129),
        ("allow_insecure_http", 1),
    ],
)
def test_parser_delegates_strict_transport_validation_without_leaking(
    field: str, value: object
) -> None:
    root = enabled_root()
    root[field] = value
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root}),
        TOKEN,
        MODEL,
        repr(value),
    )


@pytest.mark.parametrize(
    "targets",
    [
        {"A" * 64: {"binding": BINDING_A, "phrases": phrase_storage()}},
        {"a" * 63: {"binding": BINDING_A, "phrases": phrase_storage()}},
        {KEY_A: {"binding": "A" * 64, "phrases": phrase_storage()}},
        {KEY_A: {"binding": BINDING_A, "phrases": [PHRASE]}},
        {KEY_A: {"binding": BINDING_A, "phrases": phrase_storage(), "x": 1}},
        {KEY_A: [BINDING_A, phrase_storage()]},
    ],
)
def test_parser_rejects_target_shape_plaintext_and_noncanonical_hex(
    targets: object,
) -> None:
    root = enabled_root()
    root["targets"] = targets
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root}),
        PHRASE,
        KEY_A,
        BINDING_A,
    )


def test_parser_bounds_huge_malformed_structures_before_nested_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected_matcher(_value: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("nested parser reached")

    monkeypatch.setattr(voice_runtime.PhraseMatcher, "from_stored", unexpected_matcher)
    huge_targets = {
        f"{index:064x}": {"binding": BINDING_A, "phrases": phrase_storage()}
        for index in range(MAX_VOICE_TARGETS + 1)
    }
    root = enabled_root()
    root["targets"] = huge_targets
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root})
    )
    assert calls == 0

    huge_root = {f"field-{index}": None for index in range(100_000)}
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: huge_root})
    )
    assert calls == 0


def test_enabled_config_is_immutable_sorted_and_repr_is_aggregate_only() -> None:
    stored = phrase_storage()
    config = parse_enabled(
        {
            KEY_C: (BINDING_C, stored),
            KEY_A: (BINDING_A, stored),
            KEY_B: (BINDING_B, stored),
        }
    )

    assert config.enabled is True
    assert config.target_count == 3
    assert config.target_keys == (KEY_A, KEY_B, KEY_C)
    assert config.configured_targets == (
        (KEY_A, BINDING_A),
        (KEY_B, BINDING_B),
        (KEY_C, BINDING_C),
    )
    assert config.phrase_count == 3
    assert repr(config) == (
        "VoiceRuntimeConfig(enabled=True, target_count=3, phrase_count=3)"
    )
    rendered = repr(config) + repr(config.targets) + repr(config.stt_config)
    for secret in (ENDPOINT, TOKEN, MODEL, KEY_A, BINDING_A, "salt", "digests"):
        assert secret not in rendered
    assert not hasattr(config, "__dict__")
    assert not hasattr(config.targets[0], "__dict__")
    assert not hasattr(config.targets[0], "matcher")
    assert config.targets[0].matches(PHRASE) is True
    with pytest.raises(FrozenInstanceError):
        config.enabled = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.targets[0].key = KEY_B  # type: ignore[misc]


@pytest.mark.parametrize("enabled", [True, False])
def test_parser_accepts_editable_configuration_but_runtime_retains_only_matcher(
    enabled,
):
    root = enabled_root()
    root["enabled"] = enabled
    root["targets"][KEY_A]["entered_phrases"] = [PHRASE]
    before = deepcopy(root)
    config = parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root})
    assert root == before
    assert config.enabled is enabled
    assert PHRASE not in repr(config) + repr(config.targets)
    if enabled:
        assert config.targets[0].matches(PHRASE)
        assert not hasattr(config.targets[0], "entered_phrases")
    else:
        assert config.targets == () and config.stt_config is None


@pytest.mark.parametrize(
    "entered",
    [
        None,
        [],
        "PRIVATE PHRASE",
        [1],
        ["!!"],
        ["x" * 513],
        [PHRASE] * 17,
        ["first phrase\nsecond phrase"],
        ["one", "two"],
    ],
)
def test_parser_rejects_malformed_editable_configuration_without_private_errors(
    entered,
):
    root = enabled_root()
    root["targets"][KEY_A]["entered_phrases"] = entered
    assert_fixed_config_error(
        lambda: parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: root}),
        PHRASE,
        "PRIVATE PHRASE",
        repr(entered),
    )


def test_runtime_config_equality_is_identity_even_when_only_token_differs() -> None:
    first = enabled_root()
    second = enabled_root()
    second["token"] = "ANOTHER-PRIVATE-STT-TOKEN"

    first_config = parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: first})
    second_config = parse_voice_phrase_options({VOICE_PHRASE_OPTIONS_ROOT: second})

    assert first_config is not second_config
    assert first_config != second_config


class FakeSource:
    """Small source implementing the real source's asynchronous boundary."""

    def __init__(
        self,
        frames: list[bytes | None | BaseException] | None = None,
        *,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.frames = [] if frames is None else list(frames)
        self.started = 0
        self.closed = 0
        self.close_gate = close_gate
        self.read_started = asyncio.Event()
        self._block = asyncio.Event()

    async def async_start(self) -> None:
        self.started += 1

    async def async_read_frame(self) -> bytes | None:
        self.read_started.set()
        await asyncio.sleep(0)
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        await self._block.wait()
        return None

    async def async_close(self) -> None:
        self.closed += 1
        if self.close_gate is not None:
            await self.close_gate.wait()


class QueuedSource(FakeSource):
    """Controllable source that exposes one frame at a time."""

    def __init__(self, *, close_gate: asyncio.Event | None = None) -> None:
        super().__init__(close_gate=close_gate)
        self._items: asyncio.Queue[bytes | None | BaseException] = asyncio.Queue()

    def push(self, item: bytes | None | BaseException) -> None:
        self._items.put_nowait(item)

    async def async_read_frame(self) -> bytes | None:
        self.read_started.set()
        item = await self._items.get()
        if isinstance(item, BaseException):
            raise item
        return item


class SourceFactory:
    def __init__(self, sources: list[FakeSource] | None = None) -> None:
        self.sources = [] if sources is None else list(sources)
        self.calls: list[tuple[str, str]] = []
        self.created: list[FakeSource] = []

    def __call__(self, binary: str, url: str) -> FakeSource:
        self.calls.append((binary, url))
        source = self.sources.pop(0) if self.sources else FakeSource()
        self.created.append(source)
        return source


class FakeVad:
    def __init__(self, probabilities: list[float | None] | None = None) -> None:
        self.probabilities = [] if probabilities is None else list(probabilities)
        self.frames: list[bytes] = []

    def process(self, frame: bytes) -> float | None:
        self.frames.append(frame)
        return self.probabilities.pop(0) if self.probabilities else 0.0


class EveryFrameSegmenter:
    """Treat every accepted frame as one complete synthetic segment."""

    def process(self, frame: bytes, _probability: float) -> bytes:
        return frame


class FakeStt:
    def __init__(self, results: list[str | BaseException] | None = None) -> None:
        self.results = [] if results is None else list(results)
        self.pcm: list[bytes] = []

    async def transcribe_pcm(self, pcm: bytes) -> str:
        self.pcm.append(pcm)
        await asyncio.sleep(0)
        item = self.results.pop(0) if self.results else "wrong phrase"
        if isinstance(item, BaseException):
            raise item
        return item


async def direct_executor(
    function: Callable[[object], object], value: object
) -> object:
    await asyncio.sleep(0)
    return function(value)


async def settle(turns: int = 30) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def route(key: str) -> str:
    return f"rtsp://127.0.0.1:18554/{key}"


def manager_for(
    config: VoiceRuntimeConfig,
    snapshot: Callable[[], Mapping[str, DiscoveredDoor]] | None,
    routes: Callable[[str], str | None] | None,
    source_factory: Callable[[str, str], FakeSource] | None,
    stt: Any,
    **kwargs: Any,
) -> VoicePhraseManager:
    return VoicePhraseManager(
        config,
        snapshot_provider=snapshot,
        stream_url_provider=routes,
        ffmpeg_binary="ffmpeg",
        stt_client=stt,
        async_executor=kwargs.pop("async_executor", direct_executor),
        source_factory=source_factory,
        vad_factory=kwargs.pop("vad_factory", lambda: FakeVad()),
        **kwargs,
    )


@pytest.mark.parametrize(
    "field",
    ["frame_timeout_seconds", "pulse_seconds", "cleanup_retry_seconds"],
)
@pytest.mark.parametrize(
    "invalid",
    [float("nan"), float("inf"), float("-inf"), 10**10_000],
    ids=["nan", "positive-infinity", "negative-infinity", "overflowing-int"],
)
def test_manager_rejects_nonfinite_or_unconvertible_timings_with_fixed_error(
    field: str, invalid: float
) -> None:
    with pytest.raises(ValueError) as caught:
        manager_for(
            parse_enabled(),
            lambda: {KEY_A: door()},
            route,
            SourceFactory(),
            FakeStt(),
            **{field: invalid},
        )
    assert caught.value.args == ("Invalid voice phrase manager configuration.",)


@pytest.mark.parametrize(
    "field", ["frame_timeout_seconds", "pulse_seconds", "cleanup_retry_seconds"]
)
@pytest.mark.parametrize(
    "invalid", [True, "1", object(), IntLookalike(1), FloatLookalike(1.0)]
)
def test_manager_timings_require_exact_int_or_float(
    field: str, invalid: object
) -> None:
    with pytest.raises(ValueError) as caught:
        manager_for(
            parse_enabled(),
            lambda: {KEY_A: door()},
            route,
            SourceFactory(),
            FakeStt(),
            **{field: invalid},
        )
    assert caught.value.args == ("Invalid voice phrase manager configuration.",)


@pytest.mark.parametrize("missing", ["disabled", "snapshot", "route", "source", "stt"])
async def test_disabled_or_missing_infrastructure_creates_zero_work(
    missing: str,
) -> None:
    config = (
        parse_voice_phrase_options({}) if missing == "disabled" else parse_enabled()
    )
    snapshots = 0
    routes = 0
    factory = SourceFactory()

    def get_snapshot() -> Mapping[str, DiscoveredDoor]:
        nonlocal snapshots
        snapshots += 1
        return {KEY_A: door()}

    def get_route(key: str) -> str:
        nonlocal routes
        routes += 1
        return route(key)

    runtime = VoicePhraseManager(
        config,
        snapshot_provider=None if missing == "snapshot" else get_snapshot,
        stream_url_provider=None if missing == "route" else get_route,
        ffmpeg_binary=None if missing == "source" else "ffmpeg",
        stt_client=None if missing == "stt" else FakeStt(),
        async_executor=direct_executor,
        source_factory=factory,
        vad_factory=lambda: FakeVad(),
    )

    assert runtime.worker_count == 0
    await runtime.async_start()
    await settle()
    assert runtime.worker_count == 0
    assert runtime.in_flight_count == 0
    assert factory.calls == []
    assert runtime.available_count == 0
    assert runtime.on_count == 0
    if missing == "disabled":
        assert snapshots == routes == 0
    await runtime.async_stop()


async def test_disabled_manager_does_not_allocate_a_loop_limiter() -> None:
    loop = asyncio.get_running_loop()
    assert loop not in voice_runtime._LOOP_LIMITERS
    runtime = manager_for(
        parse_voice_phrase_options({}),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )

    await runtime.async_start()
    await runtime.async_stop()

    assert loop not in voice_runtime._LOOP_LIMITERS
    assert not hasattr(runtime, "_semaphore")


def test_loop_limiter_registry_does_not_retain_a_closed_test_loop() -> None:
    loop = asyncio.new_event_loop()
    loop_reference = weakref.ref(loop)

    async def allocate() -> weakref.ReferenceType[asyncio.Semaphore]:
        return weakref.ref(voice_runtime._loop_limiter())

    limiter_reference = loop.run_until_complete(allocate())
    assert limiter_reference() is None
    assert loop in voice_runtime._LOOP_LIMITERS

    loop.close()
    del loop
    gc.collect()

    assert loop_reference() is None


@pytest.mark.parametrize(
    ("current", "url"),
    [
        (door(binding=BINDING_B), route(KEY_A)),
        (door(trusted=False), route(KEY_A)),
        (door(), None),
        (door(), "rtsp://192.0.2.10/live"),
        (door(), route(KEY_B)),
        (door(KEY_B, BINDING_B), route(KEY_A)),
    ],
)
async def test_worker_requires_exact_key_binding_trust_and_loopback_route(
    current: DiscoveredDoor, url: str | None
) -> None:
    factory = SourceFactory()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: current},
        lambda _key: url,
        factory,
        FakeStt(),
    )
    assert runtime.configured_for(KEY_A, BINDING_A) is True
    assert runtime.configured_for(KEY_A, BINDING_B) is False
    await runtime.async_start()
    await settle()
    assert factory.calls == []
    assert runtime.worker_count == 0
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    await runtime.async_stop()


async def test_full_source_vad_segment_stt_executor_matcher_flow_and_no_false_start() -> (
    None
):
    probabilities = [None, *([1.0] * MIN_VOICED_FRAMES), *([0.0] * END_SILENCE_FRAMES)]
    frames = [
        SILENCE,
        *([VOICE] * MIN_VOICED_FRAMES),
        *([SILENCE] * END_SILENCE_FRAMES),
    ]
    source = FakeSource(frames)
    vad = FakeVad(probabilities)
    stt = FakeStt([PHRASE.lower() + "!!!"])
    executor_calls: list[tuple[Callable[[object], object], object]] = []

    async def executor(function: Callable[[object], object], value: object) -> object:
        executor_calls.append((function, value))
        return function(value)

    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: MappingProxyType({KEY_A: door()}),
        stream_url_provider=route,
        ffmpeg_binary="ffmpeg",
        stt_client=stt,
        async_executor=executor,
        source_factory=SourceFactory([source]),
        vad_factory=lambda: vad,
    )
    await runtime.async_start()
    assert runtime.is_on_for(KEY_A) is False
    assert runtime.available_for(KEY_A) is False
    await settle(250)

    assert source.started == 1
    assert vad.frames == frames
    assert len(stt.pcm) == 1
    assert stt.pcm[0] == (VOICE * MIN_VOICED_FRAMES) + (SILENCE * END_SILENCE_FRAMES)
    assert len(executor_calls) == 1
    assert executor_calls[0][1] == PHRASE.lower() + "!!!"
    assert runtime.available_for(KEY_A) is True
    assert runtime.is_on_for(KEY_A) is True
    assert runtime.available_count == runtime.on_count == 1
    await runtime.async_stop()
    assert source.closed == 1


class GateStt:
    def __init__(self, result: str = "wrong") -> None:
        self.result = result
        self.calls = 0
        self.active = 0
        self.maximum = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def transcribe_pcm(self, _pcm: bytes) -> str:
        self.calls += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return self.result
        finally:
            self.active -= 1


class ThreadBlockedMatcher:
    """Track real executor threads while holding them at a deterministic gate."""

    def __init__(self, terminal_error: BaseException | None = None) -> None:
        self.release = threading.Event()
        self.terminal_error = terminal_error
        self._lock = threading.Lock()
        self._calls = 0
        self._active = 0
        self._maximum = 0

    def matches(self, _text: object) -> bool:
        with self._lock:
            self._calls += 1
            self._active += 1
            self._maximum = max(self._maximum, self._active)
        try:
            self.release.wait()
            if self.terminal_error is not None:
                raise self.terminal_error
            return True
        finally:
            with self._lock:
                self._active -= 1

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            return self._calls, self._active, self._maximum


class GatedExecutor:
    """Hold matcher execution without making cancellation own the gate."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.maximum = 0

    async def __call__(
        self, _function: Callable[[object], object], _value: object
    ) -> bool:
        self.calls += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return False
        finally:
            self.active -= 1


class CrossManagerProbe:
    """Track network and matcher work across independently owned managers."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[tuple[int, str]] = []
        self.active = 0
        self.maximum = 0

    async def hold(self, owner: int, phase: str) -> None:
        self.calls.append((owner, phase))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await self.release.wait()
            await asyncio.sleep(0)
        finally:
            self.active -= 1


class CompletingCrossManagerProbe:
    """Track short network and matcher phases across managers."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.active = 0
        self.maximum = 0

    async def run(self, owner: int, phase: str) -> None:
        self.calls.append((owner, phase))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0)
        finally:
            self.active -= 1


class ProbedStt:
    def __init__(self, probe: CrossManagerProbe, owner: int) -> None:
        self.probe = probe
        self.owner = owner

    async def transcribe_pcm(self, _pcm: bytes) -> str:
        await self.probe.hold(self.owner, "stt")
        return PHRASE


def queued_voice_sources(count: int) -> list[FakeSource]:
    sources: list[FakeSource] = []
    for _ in range(count):
        source = QueuedSource()
        source.push(VOICE)
        sources.append(source)
    return sources


def two_segments() -> tuple[list[bytes], list[float]]:
    frames = ([VOICE] * MIN_VOICED_FRAMES + [SILENCE] * END_SILENCE_FRAMES) * 2
    probabilities = ([1.0] * MIN_VOICED_FRAMES + [0.0] * END_SILENCE_FRAMES) * 2
    return frames, probabilities


async def test_only_one_stt_in_flight_per_device_and_later_segment_drops() -> None:
    frames, probabilities = two_segments()
    gate = GateStt()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([FakeSource(frames)]),
        gate,
        vad_factory=lambda: FakeVad(probabilities),
    )
    await runtime.async_start()
    await gate.started.wait()
    await settle(250)
    assert gate.calls == 1
    assert runtime.in_flight_count == 1
    gate.release.set()
    await settle()
    assert gate.calls == 1
    await runtime.async_stop()


async def test_shared_stt_concurrency_is_globally_capped_at_two() -> None:
    stored = phrase_storage()
    config = parse_enabled(
        {
            KEY_A: (BINDING_A, stored),
            KEY_B: (BINDING_B, stored),
            KEY_C: (BINDING_C, stored),
        }
    )
    frames = [VOICE] * MIN_VOICED_FRAMES + [SILENCE] * END_SILENCE_FRAMES
    probabilities = [1.0] * MIN_VOICED_FRAMES + [0.0] * END_SILENCE_FRAMES
    sources = [FakeSource(frames) for _ in range(3)]
    vads = [FakeVad(probabilities) for _ in range(3)]
    gate = GateStt()
    runtime = manager_for(
        config,
        lambda: {
            KEY_A: door(KEY_A, BINDING_A),
            KEY_B: door(KEY_B, BINDING_B),
            KEY_C: door(KEY_C, BINDING_C),
        },
        route,
        SourceFactory(sources),
        gate,
        vad_factory=lambda: vads.pop(0),
    )
    await runtime.async_start()
    await settle(300)
    assert gate.calls == 2
    assert gate.maximum == 2
    assert runtime.in_flight_count == 3
    gate.release.set()
    await settle(100)
    assert gate.calls == 3
    assert gate.maximum == 2
    await runtime.async_stop()


async def test_global_limit_spans_managers_stt_matcher_churn_and_stop() -> None:
    """One loop-wide permit pool survives cancellation and entry churn fairly."""

    stored = phrase_storage()
    config = parse_enabled({KEY_A: (BINDING_A, stored), KEY_B: (BINDING_B, stored)})
    probe = CrossManagerProbe()
    routes_by_owner = [{KEY_A: route(KEY_A), KEY_B: route(KEY_B)} for _ in range(4)]
    runtimes: list[VoicePhraseManager] = []
    for owner in range(4):

        async def executor(
            _function: Callable[[object], object],
            _value: object,
            owner: int = owner,
        ) -> bool:
            await probe.hold(owner, "matcher")
            return False

        runtimes.append(
            manager_for(
                config,
                lambda: {
                    KEY_A: door(KEY_A, BINDING_A),
                    KEY_B: door(KEY_B, BINDING_B),
                },
                routes_by_owner[owner].get,
                SourceFactory(queued_voice_sources(4)),
                ProbedStt(probe, owner),
                async_executor=executor,
                vad_factory=lambda: FakeVad([1.0]),
                segmenter_factory=EveryFrameSegmenter,
                min_stt_interval_seconds=0,
            )
        )

    try:
        await runtimes[0].async_start()
        await wait_until(lambda: probe.calls.count((0, "stt")) == 2)
        await asyncio.gather(*(runtime.async_start() for runtime in runtimes[1:]))
        await settle()
        assert probe.active == probe.maximum == 2

        await runtimes[0].async_stop()
        await wait_until(lambda: probe.calls.count((1, "stt")) == 2)
        assert probe.active == probe.maximum == 2

        routes_by_owner[1].update(
            {
                KEY_A: f"rtsp://127.0.0.1:18555/{KEY_A}",
                KEY_B: f"rtsp://127.0.0.1:18555/{KEY_B}",
            }
        )
        runtimes[1].reconcile()
        await wait_until(lambda: probe.calls.count((2, "stt")) == 2)
        assert probe.active == probe.maximum == 2

        probe.release.set()
        await wait_until(
            lambda: all(
                probe.calls.count((owner, "matcher")) >= 2 for owner in (1, 2, 3)
            )
        )
        await wait_until(
            lambda: all(runtime.in_flight_count == 0 for runtime in runtimes)
        )

        assert probe.maximum == 2
        assert probe.calls.count((1, "stt")) >= 4
        assert probe.calls.count((1, "matcher")) >= 2
        for owner in (2, 3):
            assert probe.calls.count((owner, "stt")) == 2
            assert probe.calls.count((owner, "matcher")) == 2
    finally:
        probe.release.set()
        await asyncio.gather(*(runtime.async_stop() for runtime in runtimes))
    assert probe.active == 0


async def test_pulse_cleanup_releases_global_limiter_for_third_manager() -> None:
    """Two resistant pulse retriggers cannot starve unrelated expensive work."""

    probe = CompletingCrossManagerProbe()
    snapshots = [{KEY_A: door()} for _ in range(3)]
    sources = [QueuedSource() for _ in range(3)]
    pulse_sleeps = [CancellationResistantPulseSleep() for _ in range(2)]
    runtimes: list[VoicePhraseManager] = []

    for owner in range(3):

        class CompletingStt:
            async def transcribe_pcm(self, _pcm: bytes, owner: int = owner) -> str:
                await probe.run(owner, "network")
                return PHRASE

        async def executor(
            _function: Callable[[object], object],
            _value: object,
            owner: int = owner,
        ) -> bool:
            await probe.run(owner, "matcher")
            return owner < 2

        runtimes.append(
            manager_for(
                parse_enabled(),
                snapshots[owner].copy,
                route,
                SourceFactory([sources[owner]]),
                CompletingStt(),
                async_executor=executor,
                vad_factory=lambda: FakeVad([1.0]),
                segmenter_factory=EveryFrameSegmenter,
                sleep=pulse_sleeps[owner] if owner < 2 else ControlledSleep(),
                min_stt_interval_seconds=0,
            )
        )

    try:
        for owner in range(2):
            await runtimes[owner].async_start()
            await sources[owner].read_started.wait()
            sources[owner].push(VOICE)
            await wait_until(lambda owner=owner: len(pulse_sleeps[owner].calls) == 1)
            await wait_until(lambda owner=owner: runtimes[owner].in_flight_count == 0)
            assert runtimes[owner].is_on_for(KEY_A) is True

        await runtimes[2].async_start()
        await sources[2].read_started.wait()
        sources[0].push(VOICE)
        sources[1].push(VOICE)
        await asyncio.gather(*(sleep.cancelled.wait() for sleep in pulse_sleeps))
        assert runtimes[0].in_flight_count == 1
        assert runtimes[1].in_flight_count == 1
        assert all(sleep.active == 1 for sleep in pulse_sleeps)

        sources[2].push(VOICE)
        await settle(100)

        assert probe.calls.count((2, "network")) == 1
        assert probe.calls.count((2, "matcher")) == 1
        assert runtimes[2].in_flight_count == 0
        assert runtimes[2].available_for(KEY_A) is True
        assert runtimes[2].is_on_for(KEY_A) is False
        assert runtimes[2]._workers[KEY_A].pulse_task is None
        assert runtimes[0].in_flight_count == 1
        assert runtimes[1].in_flight_count == 1
        assert probe.active == 0
        assert probe.maximum <= voice_runtime.MAX_STT_CONCURRENCY

        for owner in range(2):
            snapshots[owner].clear()
            runtimes[owner].reconcile()
        for sleep in pulse_sleeps:
            sleep.release_all()
        await wait_until(
            lambda: all(runtime.in_flight_count == 0 for runtime in runtimes)
        )
        await wait_until(
            lambda: all(runtime.worker_count == 0 for runtime in runtimes[:2])
        )
        assert all(runtime.is_on_for(KEY_A) is False for runtime in runtimes[:2])
        assert all(runtime.timer_count == 0 for runtime in runtimes[:2])
    finally:
        for owner in range(2):
            snapshots[owner].clear()
            runtimes[owner].reconcile()
        for sleep in pulse_sleeps:
            sleep.release_all()
        await asyncio.gather(*(runtime.async_stop() for runtime in runtimes))
    assert probe.active == 0
    assert probe.maximum <= voice_runtime.MAX_STT_CONCURRENCY


async def test_limiter_release_boundary_rechecks_generation_before_pulse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation and churn at permit release cannot publish a stale pulse."""

    private_cancel = "PRIVATE-LIMITER-RELEASE-CANCELLATION"
    snapshot = {KEY_A: door()}
    source = QueuedSource()
    sleep = ControlledSleep()
    runtime = manager_for(
        parse_enabled(),
        snapshot.copy,
        route,
        SourceFactory([source]),
        FakeStt([PHRASE]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    released_tasks: list[asyncio.Task[None]] = []

    class BoundaryLimiter(asyncio.Semaphore):
        def release(self) -> None:
            super().release()
            if released_tasks:
                return
            current = asyncio.current_task()
            assert current is not None
            released_tasks.append(current)
            assert current.cancel(private_cancel) is True
            snapshot.clear()
            runtime.reconcile()

    limiter = BoundaryLimiter(1)
    monkeypatch.setattr(voice_runtime, "_loop_limiter", lambda: limiter)
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await wait_until(lambda: len(released_tasks) == 1)
        stt_task = released_tasks[0]
        await wait_until(stt_task.done)
        await wait_until(lambda: runtime.worker_count == 0)
        await settle()
        gc.collect()

        assert limiter._value == 1
        assert sleep.calls == []
        assert not any(on for _available, on in writes)
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert runtime.timer_count == 0
        assert_terminal_owned_task_scrubbed(
            stt_task,
            {id(runtime), id(runtime.config), id(source), id(VOICE)},
            {
                private_cancel,
                VOICE,
                PHRASE,
                route(KEY_A),
                KEY_A,
                BINDING_A,
                TOKEN,
                ENDPOINT,
            },
        )
    finally:
        snapshot.clear()
        await runtime.async_stop()


async def test_default_executor_same_key_churn_keeps_matcher_owned_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = ThreadBlockedMatcher()
    monkeypatch.setattr(voice_runtime.PhraseMatcher, "matches", matcher.matches)
    current_route = route(KEY_A)
    sources = queued_voice_sources(4)
    factory = SourceFactory(sources)
    stt = FakeStt([PHRASE] * 4)
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=lambda _key: current_route,
        ffmpeg_binary="ffmpeg",
        stt_client=stt,
        source_factory=factory,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
    )
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    try:
        await runtime.async_start()
        await wait_until(lambda: matcher.snapshot()[0] == 1)

        for port in range(18555, 18558):
            current_route = f"rtsp://127.0.0.1:{port}/{KEY_A}"
            runtime.reconcile()
            await asyncio.sleep(0.01)
            await settle(50)

        calls, active, maximum = matcher.snapshot()
        assert (calls, active, maximum) == (1, 1, 1)
        assert len(factory.calls) == 1
        assert stt.pcm == [VOICE]
        assert runtime.in_flight_count == active == 1
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False

        stop_task = asyncio.create_task(runtime.async_stop())
        await settle(50)
        assert stop_task.done() is False
        assert runtime.in_flight_count == 1
        notifications = len(writes)

        matcher.release.set()
        await stop_task
        await wait_until(lambda: matcher.snapshot()[1] == 0)
        await settle()

        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert len(writes) == notifications
        assert not runtime._all_worker_tasks
        assert not runtime._all_stt_tasks
        assert not runtime._all_timer_tasks
        assert not runtime._retiring_by_key
        assert not {
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        }
    finally:
        matcher.release.set()
        await runtime.async_stop()
        await wait_until(lambda: matcher.snapshot()[1] == 0)


async def test_default_executor_two_key_churn_respects_global_matcher_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = ThreadBlockedMatcher()
    monkeypatch.setattr(voice_runtime.PhraseMatcher, "matches", matcher.matches)
    stored = phrase_storage()
    config = parse_enabled({KEY_A: (BINDING_A, stored), KEY_B: (BINDING_B, stored)})
    current_routes = {KEY_A: route(KEY_A), KEY_B: route(KEY_B)}
    factory = SourceFactory(queued_voice_sources(8))
    stt = FakeStt([PHRASE] * 8)
    runtime = VoicePhraseManager(
        config,
        snapshot_provider=lambda: {
            KEY_A: door(KEY_A, BINDING_A),
            KEY_B: door(KEY_B, BINDING_B),
        },
        stream_url_provider=current_routes.get,
        ffmpeg_binary="ffmpeg",
        stt_client=stt,
        source_factory=factory,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
    )
    try:
        await runtime.async_start()
        await wait_until(lambda: matcher.snapshot()[0] == 2)

        for port in range(18555, 18558):
            current_routes.update(
                {
                    KEY_A: f"rtsp://127.0.0.1:{port}/{KEY_A}",
                    KEY_B: f"rtsp://127.0.0.1:{port}/{KEY_B}",
                }
            )
            runtime.reconcile()
            await asyncio.sleep(0.01)
            await settle(50)

        calls, active, maximum = matcher.snapshot()
        assert (calls, active) == (2, 2)
        assert maximum <= 2
        assert len(factory.calls) == 2
        assert len(stt.pcm) == 2
        assert runtime.in_flight_count == active == 2

        stop_task = asyncio.create_task(runtime.async_stop())
        await settle(50)
        assert stop_task.done() is False
        matcher.release.set()
        await stop_task
        await wait_until(lambda: matcher.snapshot()[1] == 0)
        assert runtime.in_flight_count == 0
        assert runtime.worker_count == 0
    finally:
        matcher.release.set()
        await runtime.async_stop()
        await wait_until(lambda: matcher.snapshot()[1] == 0)


async def test_retirement_barrier_precedes_reentrant_clear_notification() -> None:
    current_route = route(KEY_A)
    old = QueuedSource()
    replacement = QueuedSource()
    factory = SourceFactory([old, replacement])
    executor = GatedExecutor()
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=lambda _key: current_route,
        ffmpeg_binary="ffmpeg",
        stt_client=FakeStt([PHRASE]),
        async_executor=executor,
        source_factory=factory,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
    )
    barrier_seen: list[asyncio.Task[None] | None] = []
    writes: list[tuple[bool, bool]] = []
    listener_failure = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE RECONCILE LISTENER"), "RECONCILE"
    )
    failed_calls = 0
    try:
        await runtime.async_start()
        await old.read_started.wait()
        old.push(VOICE)
        await executor.started.wait()
        old_task = runtime._workers[KEY_A].task
        assert old_task is not None

        def fail_clear_notification() -> None:
            nonlocal failed_calls
            failed_calls += 1
            raise listener_failure

        def restore_latest_route() -> None:
            nonlocal current_route
            state = (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
            writes.append(state)
            if state != (False, False) or barrier_seen:
                return
            barrier_seen.append(runtime._retiring_by_key.get(KEY_A))
            current_route = f"rtsp://127.0.0.1:18556/{KEY_A}"
            runtime.reconcile()

        runtime.add_listener(fail_clear_notification)
        runtime.add_listener(restore_latest_route)
        tasks_before = {task for task in asyncio.all_tasks() if not task.done()}
        current_route = f"rtsp://127.0.0.1:18555/{KEY_A}"
        runtime.reconcile()
        tasks_after = {task for task in asyncio.all_tasks() if not task.done()}
        await settle()

        assert barrier_seen == [old_task]
        assert failed_calls == 1
        assert_sanitized_listener_failure(listener_failure)
        assert tasks_after == tasks_before
        assert old_task.cancelling() == 1
        assert old.closed == 1
        assert replacement.started == 0
        assert [url for _, url in factory.calls] == [route(KEY_A)]
        assert writes == [(False, False)]

        executor.release.set()
        await replacement.read_started.wait()
        assert [url for _, url in factory.calls] == [route(KEY_A), current_route]
        assert executor.maximum == 1
        notifications = len(writes)
        await settle()
        assert len(writes) == notifications
    finally:
        executor.release.set()
        await runtime.async_stop()


async def test_thousand_synchronous_route_cycles_create_no_waiter_tasks() -> None:
    current_route: str | None = route(KEY_A)
    old = QueuedSource()
    replacement = QueuedSource()
    factory = SourceFactory([old, replacement])
    executor = GatedExecutor()
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=lambda _key: current_route,
        ffmpeg_binary="ffmpeg",
        stt_client=FakeStt([PHRASE, PHRASE]),
        async_executor=executor,
        source_factory=factory,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        min_stt_interval_seconds=0,
    )
    writes: list[tuple[bool, bool]] = []
    try:
        await runtime.async_start()
        await old.read_started.wait()
        old.push(VOICE)
        await executor.started.wait()
        runtime.add_listener(
            lambda: writes.append(
                (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
            )
        )
        old_task = runtime._workers[KEY_A].task
        assert old_task is not None

        for port in range(19_000, 20_000):
            current_route = None
            runtime.reconcile()
            current_route = f"rtsp://127.0.0.1:{port}/{KEY_A}"
            runtime.reconcile()

        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime._all_worker_tasks == {old_task}
        assert sum(not task.done() for task in runtime._all_worker_tasks) == 1
        assert len(factory.created) == 1
        assert old.closed == 0
        assert executor.calls == executor.active == executor.maximum == 1
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert writes == [(False, False)]

        await settle()
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime._all_worker_tasks == {old_task}
        assert old.closed == 1
        assert replacement.started == 0

        executor.release.set()
        await replacement.read_started.wait()
        assert old.closed == 1
        assert replacement.started == 1
        assert [url for _, url in factory.calls] == [route(KEY_A), current_route]
        replacement.push(VOICE)
        await wait_until(lambda: executor.calls == 2)
        await wait_until(lambda: runtime.available_for(KEY_A))
        assert executor.active == 0
        assert executor.maximum == 1
        assert writes == [(False, False), (True, False)]
    finally:
        executor.release.set()
        await runtime.async_stop()


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_cancelled_never_started_worker_is_the_only_retirement_barrier(
    task_factory_name: str,
) -> None:
    current_route = route(KEY_A)
    latest_route = f"rtsp://127.0.0.1:22554/{KEY_A}"
    source = QueuedSource()
    factory = SourceFactory([source])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        lambda _key: current_route,
        factory,
        FakeStt(),
    )
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)
    try:
        await runtime.async_start()
        predecessor = runtime._workers[KEY_A].task
        assert predecessor is not None
        callback_barriers: list[asyncio.Task[None] | None] = []

        def reconcile_before_retirement_callback(
            _completed: asyncio.Task[None],
        ) -> None:
            callback_barriers.append(runtime._retiring_by_key.get(KEY_A))
            runtime.reconcile()

        predecessor.add_done_callback(reconcile_before_retirement_callback)

        for port in range(21_000, 22_000):
            current_route = f"rtsp://127.0.0.1:{port}/{KEY_A}"
            runtime.reconcile()
        current_route = latest_route
        runtime.reconcile()

        assert predecessor.cancelling() == 1
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: predecessor}
        assert runtime._all_worker_tasks == {predecessor}
        assert factory.created == []

        await source.read_started.wait()
        assert predecessor.cancelled() is False
        assert predecessor.exception() is None
        assert predecessor.get_stack() == []
        assert callback_barriers == [predecessor]
        assert runtime._retiring_by_key == {}
        assert len(runtime._all_worker_tasks) == 1
        current = runtime._workers[KEY_A]
        assert current.task in runtime._all_worker_tasks
        assert current.url == latest_route
        assert [url for _, url in factory.calls] == [latest_route]
    finally:
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


class BackoffSleep:
    def __init__(self, allow: int) -> None:
        self.allow = allow
        self.delays: list[float] = []
        self.block = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) > self.allow:
            await self.block.wait()
        else:
            await asyncio.sleep(0)


async def test_frame_timeout_closes_source_marks_unavailable_and_reconnects() -> None:
    source = FakeSource()
    sleeper = BackoffSleep(allow=0)
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt(),
        frame_timeout_seconds=0.001,
        sleep=sleeper,
    )
    await runtime.async_start()
    await source.read_started.wait()
    await asyncio.sleep(0.005)
    assert source.closed == 1
    assert sleeper.delays == [1.0]
    assert runtime.available_for(KEY_A) is False
    await runtime.async_stop()


async def test_eof_reconnect_backoff_is_exponential_and_capped_at_thirty() -> None:
    sleeper = BackoffSleep(allow=6)
    sources = [FakeSource([None]) for _ in range(8)]
    factory = SourceFactory(sources)
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        factory,
        FakeStt(),
        sleep=sleeper,
    )
    await runtime.async_start()
    await settle(100)
    assert sleeper.delays[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert all(source.closed == 1 for source in factory.created[:7])
    assert runtime.worker_count == 1
    await runtime.async_stop()


async def test_audio_failure_isolated_to_one_device() -> None:
    stored = phrase_storage()
    bad = FakeSource([RuntimeError("PRIVATE AUDIO")])
    good = FakeSource([SILENCE])
    vads = [FakeVad(), FakeVad([0.0])]
    sleeper = BackoffSleep(allow=0)
    runtime = manager_for(
        parse_enabled({KEY_A: (BINDING_A, stored), KEY_B: (BINDING_B, stored)}),
        lambda: {KEY_A: door(), KEY_B: door(KEY_B, BINDING_B)},
        route,
        SourceFactory([bad, good]),
        FakeStt(),
        vad_factory=lambda: vads.pop(0),
        sleep=sleeper,
    )
    await runtime.async_start()
    await settle()
    assert runtime.available_for(KEY_A) is False
    assert runtime.available_for(KEY_B) is True
    assert runtime.available_count == 1
    await runtime.async_stop()


async def test_stt_failure_clears_pulse_and_only_later_success_recovers() -> None:
    frames, probabilities = two_segments()
    stt = FakeStt([RuntimeError("PRIVATE STT"), "wrong phrase"])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([FakeSource(frames)]),
        stt,
        vad_factory=lambda: FakeVad(probabilities),
        min_stt_interval_seconds=0,
    )
    await runtime.async_start()
    await wait_until(lambda: len(stt.pcm) == 1 and not runtime.available_for(KEY_A))
    assert len(stt.pcm) >= 1
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    await settle(200)
    assert len(stt.pcm) == 2
    assert runtime.available_for(KEY_A) is True
    assert runtime.is_on_for(KEY_A) is False
    await runtime.async_stop()


@pytest.mark.parametrize("failure_stage", ["protocol", "matcher"])
async def test_protocol_or_matcher_failure_stays_unavailable_until_later_success(
    failure_stage: str,
) -> None:
    frames, probabilities = two_segments()

    class ProtocolStt(FakeStt):
        async def transcribe_pcm(self, pcm: bytes) -> Any:
            value = await super().transcribe_pcm(pcm)
            return object() if len(self.pcm) == 1 else value

    stt: Any = (
        ProtocolStt(["ignored", "wrong phrase"])
        if failure_stage == "protocol"
        else FakeStt([PHRASE, "wrong phrase"])
    )
    executor_calls = 0

    async def executor(function: Callable[[object], object], value: object) -> object:
        nonlocal executor_calls
        executor_calls += 1
        if failure_stage == "matcher" and executor_calls == 1:
            raise RuntimeError("PRIVATE KDF DETAIL")
        return function(value)

    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=route,
        ffmpeg_binary="ffmpeg",
        stt_client=stt,
        async_executor=executor,
        source_factory=SourceFactory([FakeSource(frames)]),
        vad_factory=lambda: FakeVad(probabilities),
        min_stt_interval_seconds=0,
    )
    await runtime.async_start()
    await wait_until(lambda: len(stt.pcm) == 1 and not runtime.available_for(KEY_A))
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    await settle(200)
    assert len(stt.pcm) == 2
    assert runtime.available_for(KEY_A) is True
    assert runtime.is_on_for(KEY_A) is False
    await runtime.async_stop()


class ControlledSleep:
    def __init__(self) -> None:
        self.calls: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.calls.append((delay, future))
        await future

    def fire(self, index: int) -> None:
        future = self.calls[index][1]
        if not future.done():
            future.set_result(None)


class SelfCancellingPulseSleep:
    """Consume private cancellation in its owning pulse Task and return."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.tasks: list[asyncio.Task[None]] = []

    async def __call__(self, delay: float) -> None:
        assert delay == 5.0
        await self_cancel_deliver_and_uncancel(self.message, self.tasks)


async def self_cancel_deliver_and_uncancel(
    message: str, tasks: list[asyncio.Task[None]]
) -> None:
    """Consume one delivered self-cancel while leaving its Task payload behind."""

    current = asyncio.current_task()
    assert current is not None
    baseline = current.cancelling()
    tasks.append(current)
    assert current.cancel(message) is True
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError as error:
        assert error.args == (message,)
    else:
        pytest.fail("self-cancellation was not delivered")
    assert current.uncancel() == baseline
    assert current.cancelling() == baseline


def physical_task_slot(task: asyncio.Task[object], name: str) -> object:
    """Read one physical built-in Task field without instance dispatch."""

    descriptor = asyncio.Task.__dict__.get(name)
    assert descriptor is not None
    return descriptor.__get__(task, asyncio.Task)


async def self_cancel_deliver_without_uncancel(
    message: str, tasks: list[asyncio.Task[None]]
) -> None:
    """Consume delivery while deliberately retaining count and private payload."""

    current = asyncio.current_task()
    assert type(current) is asyncio.Task
    baseline = current.cancelling()
    tasks.append(current)
    assert current.cancel(message) is True
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError as error:
        assert error.args == (message,)
    else:
        pytest.fail("self-cancellation was not delivered")
    assert current.cancelling() == baseline + 1
    assert physical_task_slot(current, "_must_cancel") is False
    assert getattr(current, "_cancel_message", None) == message


class ResidualReadSource(QueuedSource):
    """Return one valid frame after swallowing cancel, then block forever."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.tasks: list[asyncio.Task[None]] = []
        self.read_calls = 0
        self.second_read_started = asyncio.Event()
        self.emergency_release = asyncio.Event()

    async def async_read_frame(self) -> bytes | None:
        self.read_calls += 1
        self.read_started.set()
        if self.read_calls == 1:
            await self_cancel_deliver_without_uncancel(self.message, self.tasks)
            return SILENCE
        self.second_read_started.set()
        await self.emergency_release.wait()
        return None


class ResidualBlockingStt:
    """Leave cancellation residue in STT, then wait for lifecycle interruption."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.tasks: list[asyncio.Task[None]] = []
        self.pcm: list[bytes] = []
        self.blocked = asyncio.Event()
        self.emergency_release = asyncio.Event()

    async def transcribe_pcm(self, pcm: bytes) -> str:
        self.pcm.append(pcm)
        await self_cancel_deliver_without_uncancel(self.message, self.tasks)
        self.blocked.set()
        await self.emergency_release.wait()
        return "wrong phrase"


class ResidualBlockingPulseSleep:
    """Leave cancellation residue in a pulse timer, then block."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.tasks: list[asyncio.Task[None]] = []
        self.blocked = asyncio.Event()
        self.emergency_release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        assert delay == 5.0
        await self_cancel_deliver_without_uncancel(self.message, self.tasks)
        self.blocked.set()
        await self.emergency_release.wait()


def install_task_factory(
    task_factory_name: str,
) -> tuple[asyncio.AbstractEventLoop, Any]:
    """Install the standard eager factory for dual-factory lifecycle probes."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)
    return loop, previous_factory


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_retirement_interrupts_worker_with_residual_cancellation_count(
    task_factory_name: str,
) -> None:
    """A swallowed source self-cancel cannot strand route retirement or stop."""

    loop, previous_factory = install_task_factory(task_factory_name)
    private_cancel = "PRIVATE-RESIDUAL-SOURCE-CANCEL"
    snapshot = {KEY_A: door()}
    source = ResidualReadSource(private_cancel)
    runtime = manager_for(
        parse_enabled(),
        snapshot.copy,
        route,
        SourceFactory([source]),
        FakeStt(),
    )
    runtime.add_listener(lambda: None)
    worker_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await source.second_read_started.wait()
        assert len(source.tasks) == 1
        worker_task = source.tasks[0]
        waiter = physical_task_slot(worker_task, "_fut_waiter")
        assert worker_task.cancelling() == 1
        assert physical_task_slot(worker_task, "_must_cancel") is False
        assert isinstance(waiter, asyncio.Future)
        assert asyncio.Future.cancelled(waiter) is False

        snapshot.clear()
        runtime.reconcile()
        await wait_until(worker_task.done)
        await runtime.async_stop()
        await settle()
        gc.collect()

        assert source.closed == 1
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._listeners == []
        assert runtime._stop_task is None
        assert_terminal_owned_task_scrubbed(
            worker_task,
            {id(runtime), id(runtime.config), id(source)},
            {
                private_cancel,
                route(KEY_A),
                KEY_A,
                BINDING_A,
                TOKEN,
                ENDPOINT,
            },
        )
        assert private_cancel not in gc.get_referents(worker_task)
    finally:
        if worker_task is None or not worker_task.done():
            source.emergency_release.set()
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize("path", ["stt", "pulse"])
async def test_stop_interrupts_child_with_residual_cancellation_count(
    task_factory_name: str, path: str
) -> None:
    """STT and pulse Tasks remain interruptible after a dependency swallows cancel."""

    loop, previous_factory = install_task_factory(task_factory_name)
    private_cancel = f"PRIVATE-RESIDUAL-{path.upper()}-CANCEL"
    source = QueuedSource()
    stt: Any = (
        ResidualBlockingStt(private_cancel) if path == "stt" else FakeStt([PHRASE])
    )
    sleep: Any = (
        ResidualBlockingPulseSleep(private_cancel)
        if path == "pulse"
        else ControlledSleep()
    )
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    owned_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        blocked = stt.blocked if path == "stt" else sleep.blocked
        await blocked.wait()
        tasks = stt.tasks if path == "stt" else sleep.tasks
        assert len(tasks) == 1
        owned_task = tasks[0]
        assert owned_task is not None
        assert owned_task.cancelling() == 1
        assert physical_task_slot(owned_task, "_must_cancel") is False

        await runtime.async_stop()
        await settle()
        gc.collect()

        assert_terminal_owned_task_scrubbed(
            owned_task,
            {id(runtime), id(runtime.config), id(source), id(stt), id(sleep)},
            {private_cancel, VOICE, PHRASE, KEY_A, BINDING_A, TOKEN, ENDPOINT},
        )
        assert source.closed == 1
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._stop_task is None
    finally:
        if owned_task is None or not owned_task.done():
            if path == "stt":
                stt.emergency_release.set()
            else:
                sleep.emergency_release.set()
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("boundary", ["quarantine", "capture", "cleanup"])
async def test_fail_closed_interrupts_owned_boundaries_with_residual_count(
    boundary: str,
) -> None:
    """Fail-closed cancellation also distinguishes residue from pending delivery."""

    private_cancel = f"PRIVATE-RESIDUAL-{boundary.upper()}-CANCEL"
    tasks: list[asyncio.Task[None]] = []
    first_blocked = asyncio.Event()
    never_release = asyncio.Event()
    calls = 0
    captured: list[tuple[bool, object]] = []
    cleanup_outcome: list[BaseException] = []

    async def operation() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            await self_cancel_deliver_without_uncancel(private_cancel, tasks)
            first_blocked.set()
            await never_release.wait()
        return True

    if boundary == "quarantine":
        coroutine = voice_runtime._quarantine_task(operation, ())
    elif boundary == "capture":
        coroutine = voice_runtime._capture_task_outcome(operation, (), captured)
    else:
        coroutine = voice_runtime._cleanup_owned_task(operation, (), cleanup_outcome)
    task = voice_runtime._create_owned_task(coroutine)
    assert task is not None
    try:
        await first_blocked.wait()
        assert tasks == [task]
        assert task.cancelling() == 1

        voice_runtime._fail_closed_owned_task(task)
        await wait_until(task.done)
        await settle()
        gc.collect()

        assert calls == (2 if boundary == "cleanup" else 1)
        assert cleanup_outcome == []
        assert_terminal_owned_task_scrubbed(task, {id(operation)}, {private_cancel})
    finally:
        if not task.done():
            never_release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_lifecycle_cancel_refreshes_only_when_delivery_is_not_pending() -> None:
    """Live waiters refresh residual cancel; cancelled waiter and bit do not duplicate."""

    private_cancel = "PRIVATE-RESIDUAL-LIVE-WAITER-CANCEL"
    tasks: list[asyncio.Task[None]] = []
    residual_blocked = asyncio.Event()
    never_release = asyncio.Event()

    async def residual_operation() -> None:
        await self_cancel_deliver_without_uncancel(private_cancel, tasks)
        residual_blocked.set()
        await never_release.wait()

    residual = voice_runtime._create_owned_task(
        voice_runtime._quarantine_task(residual_operation, ())
    )
    assert residual is not None
    await residual_blocked.wait()
    live_waiter = physical_task_slot(residual, "_fut_waiter")
    assert isinstance(live_waiter, asyncio.Future)
    assert asyncio.Future.cancelled(live_waiter) is False

    tasks_before = asyncio.all_tasks()
    requests = [
        voice_runtime._cancel_owned_task_for_lifecycle(residual) for _ in range(1_000)
    ]

    assert requests.count(True) == 1
    assert asyncio.all_tasks() == tasks_before
    assert residual.cancelling() == 2
    assert getattr(residual, "_cancel_message", None) is None
    assert asyncio.Future.cancelled(live_waiter) is True
    await wait_until(residual.done)
    assert_terminal_owned_task_scrubbed(residual, protected_values={private_cancel})

    waiter_started = asyncio.Event()

    class FutureSubclass(asyncio.Future[None]):
        def cancelled(self) -> bool:
            raise AssertionError("subclass cancellation hook dispatched")

    subclass_waiter = FutureSubclass()

    async def waiter_operation() -> None:
        waiter_started.set()
        await subclass_waiter

    delegated = voice_runtime._create_owned_task(
        voice_runtime._quarantine_task(waiter_operation, ())
    )
    assert delegated is not None
    await waiter_started.wait()
    delegated_waiter = physical_task_slot(delegated, "_fut_waiter")
    assert delegated_waiter is subclass_waiter
    assert voice_runtime._TASK_CANCEL(delegated) is True
    assert asyncio.Future.cancelled(subclass_waiter) is True
    assert voice_runtime._cancel_owned_task_for_lifecycle(delegated) is False
    assert delegated.cancelling() == 1
    await wait_until(delegated.done)
    assert_terminal_owned_task_scrubbed(delegated)

    must_cancel = voice_runtime._create_owned_task(
        voice_runtime._quarantine_task(waiter_operation, ())
    )
    assert must_cancel is not None
    assert voice_runtime._TASK_CANCEL(must_cancel) is True
    assert physical_task_slot(must_cancel, "_must_cancel") is True
    assert voice_runtime._cancel_owned_task_for_lifecycle(must_cancel) is False
    assert must_cancel.cancelling() == 1
    await wait_until(must_cancel.done)
    assert_terminal_owned_task_scrubbed(must_cancel)


async def test_lifecycle_cancel_fails_safe_when_waiter_state_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected physical inspection failure still requests one safe cancellation."""

    started = asyncio.Event()
    never_release = asyncio.Event()

    async def operation() -> None:
        started.set()
        await never_release.wait()

    task = voice_runtime._create_owned_task(
        voice_runtime._quarantine_task(operation, ())
    )
    assert task is not None
    await started.wait()

    def unavailable(_task: asyncio.Task[object]) -> object:
        raise RuntimeError("PRIVATE UNAVAILABLE TASK WAITER")

    monkeypatch.setattr(voice_runtime, "_task_waiter", unavailable)
    assert voice_runtime._cancel_owned_task_for_lifecycle(task) is True
    await wait_until(task.done)
    assert_terminal_owned_task_scrubbed(
        task, protected_values={"PRIVATE UNAVAILABLE TASK WAITER"}
    )


async def assert_manager_path_scrubs_consumed_self_cancellation(path: str) -> None:
    """Exercise one real manager-owned path through its terminal Task boundary."""

    private_cancel = f"PRIVATE-{path.upper()}-DEPENDENCY-CANCELLATION"
    self_cancelled_tasks: list[asyncio.Task[None]] = []
    source: FakeSource = QueuedSource()
    stt: Any = FakeStt(["wrong phrase"])
    executor: Callable[..., Awaitable[object]] = direct_executor

    if path == "worker":

        class SelfCancellingStartSource(QueuedSource):
            async def async_start(self) -> None:
                self.started += 1
                await self_cancel_deliver_and_uncancel(
                    private_cancel, self_cancelled_tasks
                )

            async def async_read_frame(self) -> bytes | None:
                self.read_started.set()
                raise SttPipelineControlFlow("terminal worker probe")

        source = SelfCancellingStartSource()
    elif path == "stt":

        class SelfCancellingStt:
            async def transcribe_pcm(self, _pcm: bytes) -> object:
                await self_cancel_deliver_and_uncancel(
                    private_cancel, self_cancelled_tasks
                )
                return object()

        stt = SelfCancellingStt()
    elif path == "executor":

        async def self_cancelling_executor(
            function: Callable[[object], object], value: object
        ) -> object:
            await self_cancel_deliver_and_uncancel(private_cancel, self_cancelled_tasks)
            return function(value)

        executor = self_cancelling_executor
    elif path != "cleanup":
        raise AssertionError(path)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        async_executor=executor,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
    )
    task: asyncio.Task[None] | None = None
    try:
        if path == "cleanup":
            original_cleanup = runtime._cleanup

            async def self_cancelling_cleanup() -> None:
                await self_cancel_deliver_and_uncancel(
                    private_cancel, self_cancelled_tasks
                )
                await original_cleanup()

            runtime._cleanup = self_cancelling_cleanup  # type: ignore[method-assign]
            await runtime.async_stop()
        else:
            await runtime.async_start()
            if path in {"stt", "executor"}:
                assert isinstance(source, QueuedSource)
                await source.read_started.wait()
                source.push(VOICE)
            await wait_until(lambda: len(self_cancelled_tasks) == 1)
            task = self_cancelled_tasks[0]
            await wait_until(task.done)
            await runtime.async_stop()
        if task is None:
            assert len(self_cancelled_tasks) == 1
            task = self_cancelled_tasks[0]
        await settle()
        gc.collect()

        assert_terminal_owned_task_scrubbed(
            task,
            {id(runtime), id(runtime.config), id(source), id(stt), id(executor)},
            {
                private_cancel,
                VOICE,
                PHRASE,
                route(KEY_A),
                KEY_A,
                BINDING_A,
                TOKEN,
                ENDPOINT,
            },
        )
        referents = gc.get_referents(task)
        assert private_cancel not in referents
        assert runtime not in referents
    finally:
        await runtime.async_stop()


class CancellationResistantPulseSleep:
    """Keep each canceled pulse sleeper alive until its explicit release."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, asyncio.Event]] = []
        self.cancelled = asyncio.Event()
        self.active = 0
        self.maximum = 0

    async def __call__(self, delay: float) -> None:
        release = asyncio.Event()
        self.calls.append((delay, release))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
        finally:
            self.active -= 1

    def fire(self, index: int) -> None:
        self.calls[index][1].set()

    def release_all(self) -> None:
        for _delay, release in self.calls:
            release.set()


class ReleasedFailureSleep:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.started.set()
        await self.release.wait()
        raise self.failure


class RetryCloseSource(QueuedSource):
    """Retain one source identity until a close attempt is verified."""

    def __init__(self, failures_before_success: int | None) -> None:
        super().__init__()
        self.failures_before_success = failures_before_success
        self.close_attempts = 0
        self.close_successes = 0

    def permit_close(self) -> None:
        self.failures_before_success = 0

    async def async_close(self) -> None:
        self.close_attempts += 1
        if self.failures_before_success is None:
            raise RuntimeError("PRIVATE CLOSE FAILURE")
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise RuntimeError("PRIVATE CLOSE FAILURE")
        self.close_successes += 1


class ControlFlowCloseSource(QueuedSource):
    """Raise one exact control-flow exception before verified close."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure
        self.close_attempts = 0
        self.close_successes = 0

    async def async_close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise self.failure
        self.close_successes += 1


class OneControlFlowSleep:
    """Interrupt one cleanup delay, then complete its real-sleep retry."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) == 1:
            raise self.failure
        await asyncio.sleep(0)


class BurstControlFlowSleep:
    """Raise synchronously many times and schedule a competing heartbeat."""

    def __init__(self, failure: BaseException, heartbeat: asyncio.Event) -> None:
        self.failure = failure
        self.heartbeat = heartbeat
        self.calls = 0
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def __call__(self, _delay: float) -> None:
        self.calls += 1
        if self.calls == 1:

            async def beat() -> None:
                await asyncio.sleep(0)
                self.heartbeat.set()

            self.heartbeat_task = asyncio.create_task(beat())
        if self.calls <= 1_000:
            raise self.failure


async def no_delay_sleep(_delay: float) -> None:
    await asyncio.sleep(0)


class PulseThenResistantStt:
    """Pulse once, then resist retirement cancellation until released."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.maximum = 0
        self.cancelled = 0
        self.old_started = asyncio.Event()
        self.old_cancelled = asyncio.Event()
        self.old_release = asyncio.Event()
        self.replacement_started = asyncio.Event()
        self.replacement_release = asyncio.Event()

    async def transcribe_pcm(self, _pcm: bytes) -> str:
        self.calls += 1
        call = self.calls
        if call == 1:
            await asyncio.sleep(0)
            return PHRASE

        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if call == 2:
                self.old_started.set()
                try:
                    await self.old_release.wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    self.old_cancelled.set()
                    await self.old_release.wait()
                return PHRASE
            self.replacement_started.set()
            await self.replacement_release.wait()
            return "wrong phrase"
        finally:
            self.active -= 1


async def start_active_pulse(
    *,
    snapshot: Callable[[], Mapping[str, DiscoveredDoor]],
    routes: Callable[[str], str | None],
    factory: SourceFactory,
    source: QueuedSource,
    stt: Any,
    sleep: ControlledSleep,
    executor: Callable[
        [Callable[[object], object], object], Awaitable[object]
    ] = direct_executor,
    vad_factory: Callable[[], Any] = lambda: FakeVad([1.0]),
    frame_timeout_seconds: float = 5.0,
) -> tuple[VoicePhraseManager, list[tuple[bool, bool]], asyncio.Future[None]]:
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=snapshot,
        stream_url_provider=routes,
        ffmpeg_binary="ffmpeg",
        stt_client=stt,
        async_executor=executor,
        source_factory=factory,
        vad_factory=vad_factory,
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
        frame_timeout_seconds=frame_timeout_seconds,
        min_stt_interval_seconds=0,
    )
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    await runtime.async_start()
    await source.read_started.wait()
    source.push(VOICE)
    await wait_until(lambda: runtime.is_on_for(KEY_A))
    await wait_until(lambda: any(delay == 5.0 for delay, _future in sleep.calls))
    pulse = next(future for delay, future in sleep.calls if delay == 5.0)
    assert runtime.available_for(KEY_A) is True
    assert pulse.done() is False
    return runtime, writes, pulse


@pytest.mark.parametrize("failure_stage", ["stt", "protocol", "executor"])
async def test_later_pipeline_failure_clears_active_pulse_timer_then_recovers(
    failure_stage: str,
) -> None:
    class ProtocolStt(FakeStt):
        async def transcribe_pcm(self, pcm: bytes) -> Any:
            value = await super().transcribe_pcm(pcm)
            return object() if len(self.pcm) == 2 else value

    source = QueuedSource()
    sleep = ControlledSleep()
    stt: Any
    if failure_stage == "stt":
        stt = FakeStt([PHRASE, RuntimeError("PRIVATE STT"), "wrong phrase"])
    elif failure_stage == "protocol":
        stt = ProtocolStt([PHRASE, "ignored", "wrong phrase"])
    else:
        stt = FakeStt([PHRASE, PHRASE, "wrong phrase"])
    executor_calls = 0

    async def executor(function: Callable[[object], object], value: object) -> object:
        nonlocal executor_calls
        executor_calls += 1
        if failure_stage == "executor" and executor_calls == 2:
            raise RuntimeError("PRIVATE KDF DETAIL")
        return function(value)

    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=stt,
        sleep=sleep,
        executor=executor,
    )
    source.push(VOICE)
    await wait_until(lambda: len(stt.pcm) == 2)
    await wait_until(lambda: not runtime.available_for(KEY_A))

    assert runtime.is_on_for(KEY_A) is False
    assert pulse.cancelled()
    assert writes[-1] == (False, False)
    notifications = len(writes)
    await settle()
    assert len(writes) == notifications

    source.push(VOICE)
    await wait_until(lambda: len(stt.pcm) == 3)
    await wait_until(lambda: runtime.available_for(KEY_A))
    assert runtime.is_on_for(KEY_A) is False
    await runtime.async_stop()


class SttPipelineControlFlow(BaseException):
    pass


_HOSTILE_DESCRIPTOR_HOOKS: list[tuple[str, str]] = []


class HostileListenerControlFlow(BaseException):
    """Shadow physical BaseException metadata with hostile Python hooks."""

    @property
    def __traceback__(self) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("get", "__traceback__"))

    @__traceback__.setter
    def __traceback__(self, _value: object) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("set", "__traceback__"))
        raise AssertionError("hostile traceback setter invoked")

    @property
    def __context__(self) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("get", "__context__"))

    @__context__.setter
    def __context__(self, _value: object) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("set", "__context__"))
        raise AssertionError("hostile context setter invoked")

    @property
    def __cause__(self) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("get", "__cause__"))

    @__cause__.setter
    def __cause__(self, _value: object) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("set", "__cause__"))
        raise AssertionError("hostile cause setter invoked")

    @property
    def __notes__(self) -> list[object]:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("get", "__notes__"))
        return []

    @__notes__.setter
    def __notes__(self, _value: object) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("set", "__notes__"))
        raise AssertionError("hostile notes setter invoked")

    @property
    def __suppress_context__(self) -> bool:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("get", "__suppress_context__"))
        return False

    @__suppress_context__.setter
    def __suppress_context__(self, _value: object) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("set", "__suppress_context__"))
        raise AssertionError("hostile suppress-context setter invoked")

    def __getattribute__(self, name: str) -> object:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("getattribute", name))
        raise AssertionError("hostile __getattribute__ invoked")

    def __setattr__(self, name: str, _value: object) -> None:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("setattr", name))
        raise AssertionError("hostile __setattr__ invoked")

    def __str__(self) -> str:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("str", ""))
        raise AssertionError("hostile __str__ invoked")

    def __repr__(self) -> str:
        _HOSTILE_DESCRIPTOR_HOOKS.append(("repr", ""))
        raise AssertionError("hostile __repr__ invoked")


def physical_exception_slot(error: BaseException, name: str) -> Any:
    """Read one physical BaseException slot without subclass dispatch."""

    return BaseException.__dict__[name].__get__(error, BaseException)


def physical_exception_dict(error: BaseException) -> dict[str, object]:
    """Read the physical BaseException instance dict without subclass dispatch."""

    value = BaseException.__dict__["__dict__"].__get__(error, BaseException)
    assert type(value) is dict
    return value


def dirty_control_flow(error: BaseException, private_label: str) -> BaseException:
    """Preload an exact control-flow object with private exception metadata."""

    try:
        raise RuntimeError(f"PRIVATE {private_label} TRACEBACK")
    except RuntimeError as traceback_source:
        private_traceback = physical_exception_slot(traceback_source, "__traceback__")
    assert private_traceback is not None
    for name, value in (
        ("__traceback__", private_traceback),
        ("__context__", RuntimeError(f"PRIVATE {private_label} CONTEXT")),
        ("__cause__", RuntimeError(f"PRIVATE {private_label} CAUSE")),
        ("__suppress_context__", True),
    ):
        BaseException.__dict__[name].__set__(error, value)
    dict.__setitem__(
        physical_exception_dict(error),
        "__notes__",
        [f"PRIVATE {private_label} NOTE"],
    )
    assert physical_exception_slot(error, "__traceback__") is not None
    assert physical_exception_slot(error, "__context__") is not None
    assert physical_exception_slot(error, "__cause__") is not None
    assert physical_exception_slot(error, "__suppress_context__") is True
    return error


def assert_sanitized_listener_failure(error: BaseException) -> None:
    if physical_exception_slot(error, "__traceback__") is not None:
        pytest.fail("physical traceback was not scrubbed")
    if physical_exception_slot(error, "__context__") is not None:
        pytest.fail("physical context was not scrubbed")
    if physical_exception_slot(error, "__cause__") is not None:
        pytest.fail("physical cause was not scrubbed")
    notes = physical_exception_dict(error).get("__notes__")
    if notes is not None and notes != []:
        pytest.fail("physical notes were not scrubbed")
    if physical_exception_slot(error, "__suppress_context__") is not False:
        pytest.fail("physical suppress-context flag was not scrubbed")


def physical_traceback_chain(error: BaseException) -> list[Any]:
    """Return physical traceback nodes without touching subclass attributes."""

    traceback = physical_exception_slot(error, "__traceback__")
    chain: list[Any] = []
    while traceback is not None:
        chain.append(traceback)
        traceback = traceback.tb_next
    return chain


def assert_dependency_failure_sanitized(
    failure: BaseException, runtime: VoicePhraseManager
) -> None:
    """A held dependency failure cannot retain manager-private runtime state."""

    assert_sanitized_listener_failure(failure)
    protected_ids = {id(runtime), id(runtime.config)}
    protected_values: set[str | bytes] = {
        TOKEN,
        MODEL,
        ENDPOINT,
        KEY_A,
        KEY_B,
        BINDING_A,
        BINDING_B,
    }
    physical_referents = (
        physical_exception_dict(failure),
        physical_exception_slot(failure, "args"),
    )
    assert all(
        not _contains_protected_referent(value, protected_ids, protected_values)
        for value in physical_referents
    )


@pytest.mark.parametrize(
    ("failure_stage", "failure_type"),
    [
        ("infrastructure", RuntimeError),
        ("infrastructure", HostileListenerControlFlow),
        ("snapshot", asyncio.CancelledError),
        ("snapshot", SystemExit),
        ("snapshot", KeyboardInterrupt),
        ("snapshot", SttPipelineControlFlow),
    ],
    ids=[
        "infrastructure-runtime-error",
        "infrastructure-hostile-base-exception",
        "snapshot-cancelled-error",
        "snapshot-system-exit",
        "snapshot-keyboard-interrupt",
        "snapshot-base-exception",
    ],
)
async def test_global_reconciliation_dependency_failure_retires_active_pulse(
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    """Global dependency failures are non-authoritative and fail all work closed."""

    failure = dirty_control_flow(
        failure_type("PRIVATE RECONCILIATION DEPENDENCY"), "RECONCILIATION"
    )

    class FailingInfrastructureStt:
        def __getattribute__(self, name: str) -> object:
            if name == "transcribe_pcm":
                private_manager = runtime
                assert private_manager.config.enabled is True
                raise failure
            return object.__getattribute__(self, name)

    snapshot_failure: BaseException | None = None
    runtime: VoicePhraseManager

    def get_snapshot() -> Mapping[str, DiscoveredDoor]:
        if snapshot_failure is not None:
            private_manager = runtime
            assert private_manager.config.enabled is True
            raise snapshot_failure
        return {KEY_A: door()}

    source = QueuedSource()
    replacement = QueuedSource()
    factory = SourceFactory([source, replacement])
    sleep = ControlledSleep()
    working_stt = FakeStt([PHRASE])
    runtime, writes, pulse = await start_active_pulse(
        snapshot=get_snapshot,
        routes=route,
        factory=factory,
        source=source,
        stt=working_stt,
        sleep=sleep,
    )
    old_worker = runtime._workers[KEY_A]
    old_task = old_worker.task
    assert old_task is not None
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: reports.append("reported"))
    _HOSTILE_DESCRIPTOR_HOOKS.clear()
    try:
        if failure_stage == "infrastructure":
            runtime._stt_client = FailingInfrastructureStt()  # type: ignore[assignment]
        else:
            snapshot_failure = failure

        escaped = False
        for _ in range(3):
            try:
                runtime.reconcile()
            except BaseException as error:  # noqa: BLE001 - prove no escape
                escaped = True
                voice_runtime._sanitize_exception_chain(error)
                break

        if escaped:
            pytest.fail("global dependency exception escaped reconcile")

        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert writes[-1] == (False, False)
        assert replacement.started == 0

        await wait_until(old_task.done)
        await wait_until(lambda: runtime.timer_count == 0)
        await settle()

        assert reports == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert replacement.started == 0
        assert pulse.done() is True
        assert_dependency_failure_sanitized(failure, runtime)
        assert _HOSTILE_DESCRIPTOR_HOOKS == []

        runtime._stt_client = working_stt  # type: ignore[assignment]
        snapshot_failure = None
        await settle()
        assert replacement.started == 0

        runtime.reconcile()
        await replacement.read_started.wait()
        assert runtime.worker_count == 1
        assert runtime._workers[KEY_A].task in runtime._all_worker_tasks
    finally:
        runtime._stt_client = working_stt  # type: ignore[assignment]
        snapshot_failure = None
        loop.set_exception_handler(previous_handler)
        await runtime.async_stop()


@pytest.mark.parametrize("failure_stage", ["snapshot-get", "route"])
async def test_per_target_reconciliation_failure_isolates_other_active_pulse(
    failure_stage: str,
) -> None:
    """A per-target dependency failure excludes only that exact target."""

    failure = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE TARGET DEPENDENCY"), "TARGET DEPENDENCY"
    )

    class SelectiveSnapshot(dict[str, DiscoveredDoor]):
        failing = False

        def get(self, key: str, default: Any = None) -> Any:
            private_manager = runtime
            if self.failing and key == KEY_A:
                assert private_manager.config.enabled is True
                raise failure
            return super().get(key, default)

    stored = phrase_storage()
    config = parse_enabled({KEY_A: (BINDING_A, stored), KEY_B: (BINDING_B, stored)})
    snapshot = SelectiveSnapshot({KEY_A: door(), KEY_B: door(KEY_B, BINDING_B)})
    route_failure = False
    runtime: VoicePhraseManager

    def get_route(key: str) -> str:
        private_manager = runtime
        if route_failure and key == KEY_A:
            assert private_manager.config.enabled is True
            raise failure
        return route(key)

    first_a = QueuedSource()
    first_b = QueuedSource()
    replacement_a = QueuedSource()
    factory = SourceFactory([first_a, first_b, replacement_a])
    sleep = ControlledSleep()
    runtime = manager_for(
        config,
        lambda: snapshot,
        get_route,
        factory,
        FakeStt([PHRASE, PHRASE]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: reports.append("reported"))
    _HOSTILE_DESCRIPTOR_HOOKS.clear()
    try:
        await runtime.async_start()
        await asyncio.gather(first_a.read_started.wait(), first_b.read_started.wait())
        first_a.push(VOICE)
        first_b.push(VOICE)
        await wait_until(lambda: runtime.on_count == 2)
        await wait_until(lambda: runtime.timer_count == 2)
        worker_a = runtime._workers[KEY_A]
        worker_b = runtime._workers[KEY_B]
        task_a = worker_a.task
        task_b = worker_b.task
        pulse_b = worker_b.pulse_task
        assert task_a is not None
        assert task_b is not None
        assert pulse_b is not None

        if failure_stage == "snapshot-get":
            snapshot.failing = True
        else:
            route_failure = True
        escaped = False
        for _ in range(3):
            try:
                runtime.reconcile()
            except BaseException as error:  # noqa: BLE001 - prove no escape
                escaped = True
                voice_runtime._sanitize_exception_chain(error)
                break

        if escaped:
            pytest.fail("per-target dependency exception escaped reconcile")

        assert set(runtime._workers) == {KEY_B}
        assert runtime._workers[KEY_B] is worker_b
        assert runtime._retiring_by_key == {KEY_A: task_a}
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert runtime.available_for(KEY_B) is True
        assert runtime.is_on_for(KEY_B) is True

        await wait_until(task_a.done)
        await settle()
        assert reports == []
        assert set(runtime._workers) == {KEY_B}
        assert runtime._workers[KEY_B].task is task_b
        assert runtime._workers[KEY_B].pulse_task is pulse_b
        assert task_b.done() is False
        assert pulse_b.done() is False
        assert runtime.worker_count == 1
        assert runtime.timer_count == 1
        assert replacement_a.started == 0
        assert_dependency_failure_sanitized(failure, runtime)
        assert _HOSTILE_DESCRIPTOR_HOOKS == []

        snapshot.failing = False
        route_failure = False
        await settle()
        assert replacement_a.started == 0
        runtime.reconcile()
        await replacement_a.read_started.wait()
        assert set(runtime._workers) == {KEY_A, KEY_B}
        assert runtime.worker_count == 2
        assert runtime._workers[KEY_B] is worker_b
    finally:
        snapshot.failing = False
        route_failure = False
        loop.set_exception_handler(previous_handler)
        await runtime.async_stop()


async def test_retirement_reconcile_callback_quarantines_unexpected_failure() -> None:
    """An owned done callback never reports or retains unexpected control flow."""

    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    close_gate = asyncio.Event()
    source = QueuedSource(close_gate=close_gate)
    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        route,
        SourceFactory([source]),
        FakeStt(),
    )
    failure = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE CALLBACK FAILURE"), "CALLBACK"
    )
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: reports.append("reported"))
    _HOSTILE_DESCRIPTOR_HOOKS.clear()
    try:
        await runtime.async_start()
        await source.read_started.wait()
        old_task = runtime._workers[KEY_A].task
        assert old_task is not None
        snapshot.clear()
        runtime.reconcile()
        await wait_until(lambda: source.closed == 1)
        assert old_task.done() is False

        def unexpected_reconcile() -> None:
            private_manager = runtime
            assert private_manager.config.enabled is True
            raise failure

        runtime.reconcile = unexpected_reconcile  # type: ignore[method-assign]
        close_gate.set()
        await wait_until(old_task.done)
        await settle()

        assert reports == []
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert_dependency_failure_sanitized(failure, runtime)
        assert _HOSTILE_DESCRIPTOR_HOOKS == []
    finally:
        close_gate.set()
        loop.set_exception_handler(previous_handler)
        await runtime.async_stop()


async def test_standard_eager_factory_publishes_all_runtime_tasks_before_work() -> None:
    """An installed eager factory stays untouched while every owned path works."""

    eager_factory = getattr(asyncio, "eager_task_factory", None)
    if eager_factory is None:
        pytest.skip("standard eager task factory unavailable")

    current_route: str | None = route(KEY_A)
    replacement_route = f"rtsp://127.0.0.1:18555/{KEY_A}"
    first = QueuedSource()
    replacement = QueuedSource()
    worker_publication: list[bool] = []
    stt_publication: list[bool] = []
    executor_publication: list[bool] = []
    pulse_publication: list[bool] = []
    cleanup_publication: list[bool] = []
    runtime: VoicePhraseManager

    class ReentrantFactory(SourceFactory):
        def __call__(self, binary: str, url: str) -> FakeSource:
            nonlocal current_route
            source = super().__call__(binary, url)
            if len(self.calls) == 1:
                worker = runtime._workers[KEY_A]
                task = asyncio.current_task()
                worker_publication.append(
                    type(task) is asyncio.Task
                    and worker.task is task
                    and task in runtime._all_worker_tasks
                )
                current_route = replacement_route
                runtime.reconcile()
            return source

    class ObservingStt(FakeStt):
        async def transcribe_pcm(self, pcm: bytes) -> str:
            worker = runtime._workers[KEY_A]
            task = asyncio.current_task()
            stt_publication.append(
                type(task) is asyncio.Task
                and worker.stt_task is task
                and task in runtime._all_stt_tasks
            )
            return await super().transcribe_pcm(pcm)

    async def observing_executor(
        function: Callable[[object], object], value: object
    ) -> object:
        worker = runtime._workers[KEY_A]
        task = asyncio.current_task()
        executor_publication.append(
            type(task) is asyncio.Task and task is not worker.stt_task
        )
        return await direct_executor(function, value)

    class ObservingSleep(ControlledSleep):
        async def __call__(self, delay: float) -> None:
            if delay == 5.0:
                worker = runtime._workers[KEY_A]
                task = asyncio.current_task()
                pulse_publication.append(
                    type(task) is asyncio.Task
                    and worker.pulse_task is task
                    and task in runtime._all_timer_tasks
                )
            await super().__call__(delay)

    factory = ReentrantFactory([first, replacement])
    sleep = ObservingSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        lambda _key: current_route,
        factory,
        ObservingStt([PHRASE]),
        async_executor=observing_executor,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    original_cleanup = runtime._cleanup

    async def observing_cleanup() -> None:
        task = asyncio.current_task()
        cleanup_publication.append(
            type(task) is asyncio.Task and runtime._stop_task is task
        )
        await original_cleanup()

    runtime._cleanup = observing_cleanup  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(eager_factory)
    try:
        assert loop.get_task_factory() is eager_factory
        await runtime.async_start()
        await replacement.read_started.wait()
        assert worker_publication == [True]
        assert first.closed == 1
        assert replacement.started == 1
        assert runtime.worker_count == 1
        current_worker = runtime._workers[KEY_A]
        assert current_worker.url == replacement_route
        assert current_worker.task in runtime._all_worker_tasks

        replacement.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        await wait_until(lambda: runtime.timer_count == 1)
        await wait_until(lambda: pulse_publication == [True])
        assert stt_publication == [True]
        assert executor_publication == [True]
        assert pulse_publication == [True]
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is True

        current_route = None
        runtime.reconcile()
        await wait_until(lambda: runtime.worker_count == 0)
        await wait_until(lambda: runtime.timer_count == 0)
        await settle()
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        await runtime.async_stop()
        assert cleanup_publication == [True]
        assert loop.get_task_factory() is eager_factory
        assert runtime._stop_task is None
    finally:
        if runtime._started:
            await runtime.async_stop()
        loop.set_task_factory(previous_factory)


async def test_standard_eager_factory_immediate_cancel_has_no_inner_work() -> None:
    """An installed eager factory cannot advance owned work before cancellation."""

    eager_factory = getattr(asyncio, "eager_task_factory", None)
    if eager_factory is None:
        pytest.skip("standard eager task factory unavailable")

    factory = SourceFactory()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        factory,
        FakeStt(),
    )
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(eager_factory)
    runtime._started = True
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            runtime._launch_worker(runtime.config.targets[0], route(KEY_A))
            worker = runtime._workers[KEY_A]
            task = worker.task
            assert task is not None
            assert task in runtime._all_worker_tasks
            private_cancel = "PRIVATE-EAGER-CANCEL-BEFORE-FIRST-STEP"
            task.cancel(private_cancel)
            await wait_until(task.done)
            await settle()
            gc.collect()

        try:
            outcome = task.exception()
        except asyncio.CancelledError as error:
            outcome = error
        assert outcome is None
        assert_terminal_owned_task_scrubbed(
            task,
            {id(runtime), id(runtime.config), id(worker)},
            {
                private_cancel,
                route(KEY_A),
                KEY_A,
                BINDING_A,
                TOKEN,
                ENDPOINT,
            },
        )
        assert factory.calls == []
        assert runtime._workers == {}
        assert runtime._all_worker_tasks == set()
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        rendered = repr(task) + repr(worker) + repr(runtime)
        for private_value in (
            private_cancel,
            route(KEY_A),
            KEY_A,
            BINDING_A,
            TOKEN,
            ENDPOINT,
        ):
            assert private_value not in rendered
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        runtime._started = False
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("failure_stage", ["worker", "stt", "pulse"])
async def test_manager_tasks_quarantine_private_terminal_failures(
    failure_stage: str,
) -> None:
    """Tracked tasks finish cleanly without retaining private failure graphs."""

    private_pcm = b"PRIVATE-PCM-" + failure_stage.encode()
    private_transcript = "PRIVATE-TRANSCRIPT-" + failure_stage
    private_identity = object()
    failure = dirty_control_flow(
        HostileListenerControlFlow(private_transcript), "TASK " + failure_stage
    )
    dict.__setitem__(
        physical_exception_dict(failure), "private_identity", private_identity
    )
    source = QueuedSource()
    sleep = ReleasedFailureSleep(failure)

    async def executor(_function: Callable[[object], object], _value: object) -> bool:
        if failure_stage == "stt":
            raise failure
        return True

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt([private_transcript]),
        async_executor=executor,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    tracked: list[asyncio.Task[None]] = []
    original_track = runtime._track_task

    def capture_task(
        task: asyncio.Task[None], collection: set[asyncio.Task[None]]
    ) -> None:
        tracked.append(task)
        original_track(task, collection)

    runtime._track_task = capture_task  # type: ignore[method-assign]
    worker: voice_runtime._Worker | None = None
    _HOSTILE_DESCRIPTOR_HOOKS.clear()
    try:
        await runtime.async_start()
        await source.read_started.wait()
        worker = runtime._workers[KEY_A]
        active_rendered = repr(worker) + repr(runtime)
        for secret in (route(KEY_A), KEY_A, BINDING_A, TOKEN, ENDPOINT):
            assert secret not in active_rendered

        if failure_stage == "worker":
            source.push(failure)
            assert worker.task is not None
            await wait_until(worker.task.done)
        else:
            source.push(private_pcm)
            await wait_until(lambda: len(tracked) >= 2)
            if failure_stage == "pulse":
                await sleep.started.wait()
                sleep.release.set()
                await wait_until(lambda: all(task.done() for task in tracked[2:]))
            await wait_until(tracked[1].done)
        await runtime.async_stop()
        await settle()
    finally:
        sleep.release.set()
        await runtime.async_stop()

    assert worker is not None
    assert tracked
    protected_ids = {
        id(runtime),
        id(runtime.config),
        id(worker),
        id(private_pcm),
        id(private_identity),
        id(failure),
    }
    protected_values: set[str | bytes] = {
        private_pcm,
        private_transcript,
        route(KEY_A),
        KEY_A,
        BINDING_A,
        TOKEN,
        ENDPOINT,
    }
    rendered = repr(worker) + repr(runtime)
    for task in tracked:
        assert_terminal_owned_task_scrubbed(task, protected_ids, protected_values)
        task_rendered = repr(task)
        assert "exception=" not in task_rendered
        rendered += task_rendered
        assert not _contains_protected_referent(
            task.exception(), protected_ids, protected_values
        )
    for secret in protected_values:
        rendered_secret = secret.decode() if isinstance(secret, bytes) else secret
        assert rendered_secret not in rendered
    assert not {
        hook for hook in _HOSTILE_DESCRIPTOR_HOOKS if hook[0] in {"repr", "str"}
    }


async def test_worker_boundary_cancel_before_first_step_creates_no_inner_coroutine() -> (
    None
):
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    runtime._started = True
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        runtime._launch_worker(runtime.config.targets[0], route(KEY_A))
        worker = runtime._workers[KEY_A]
        task = worker.task
        assert task is not None
        private_cancel = "PRIVATE-CANCEL-BEFORE-FIRST-STEP"
        task.cancel(private_cancel)
        await wait_until(task.done)
        await settle()
        gc.collect()

    runtime._started = False
    runtime._workers.clear()
    await runtime.async_stop()
    try:
        outcome = task.exception()
    except asyncio.CancelledError as error:
        outcome = error
    assert outcome is None
    assert_terminal_owned_task_scrubbed(
        task,
        {id(runtime), id(runtime.config), id(worker)},
        {
            private_cancel,
            route(KEY_A),
            KEY_A,
            BINDING_A,
            TOKEN,
            ENDPOINT,
        },
    )
    rendered = repr(task) + repr(worker) + repr(runtime)
    assert private_cancel not in rendered
    assert route(KEY_A) not in rendered
    assert not [
        warning for warning in caught if "was never awaited" in str(warning.message)
    ]


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_primed_owned_boundary_cannot_retain_private_operation_on_cancel(
    task_factory_name: str,
) -> None:
    """The returned Task already owns cancellation inside its clean boundary."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    private_pcm = b"PRIVATE-PRESTART-PCM"
    private_cancel = "PRIVATE-PRESTART-CANCELLATION"
    stt = FakeStt()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        stt,
    )
    worker = voice_runtime._Worker(runtime.config.targets[0], route(KEY_A), 1)
    runtime._started = True
    runtime._workers[KEY_A] = worker
    operation = runtime._run_stt
    arguments = (worker, worker.audio_epoch, private_pcm)
    task: asyncio.Task[None] | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            task = voice_runtime._create_owned_task(
                voice_runtime._quarantine_task(operation, arguments)
            )
            assert task is not None
            task.cancel(private_cancel)
            await wait_until(task.done)
            await settle()
            gc.collect()

        try:
            outcome = task.exception()
        except asyncio.CancelledError as error:
            outcome = error
        protected_ids = {
            id(runtime),
            id(runtime.config),
            id(worker),
            id(operation),
            id(arguments),
            id(private_pcm),
        }
        protected_values: set[str | bytes] = {
            private_cancel,
            private_pcm,
            route(KEY_A),
            KEY_A,
            BINDING_A,
            TOKEN,
            ENDPOINT,
        }

        assert outcome is None
        assert_terminal_owned_task_scrubbed(task, protected_ids, protected_values)
        assert stt.pcm == []
        assert not _contains_protected_referent(
            outcome, protected_ids, protected_values
        )
        if isinstance(outcome, BaseException):
            for traceback in physical_traceback_chain(outcome):
                assert not _contains_protected_referent(
                    traceback.tb_frame.f_locals, protected_ids, protected_values
                )
        rendered = repr(task) + repr(worker) + repr(runtime)
        for value in protected_values:
            rendered_value = value.decode() if isinstance(value, bytes) else value
            assert rendered_value not in rendered
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        runtime._started = False
        runtime._workers.clear()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize("boundary", ["quarantine", "capture", "cleanup"])
async def test_owned_boundaries_scrub_self_cancellation_after_operation_returns(
    task_factory_name: str, boundary: str
) -> None:
    """A dependency cannot leave its clean terminal Task carrying a cancel secret."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    private_cancel = f"PRIVATE-{boundary.upper()}-SELF-CANCELLATION"
    private_argument = object()
    returned = object()
    operation_calls: list[object] = []
    self_cancelled_tasks: list[asyncio.Task[None]] = []
    captured: list[tuple[bool, object]] = []
    cleanup_outcome: list[BaseException] = []

    async def operation(argument: object) -> object:
        operation_calls.append(argument)
        await self_cancel_deliver_and_uncancel(private_cancel, self_cancelled_tasks)
        return returned

    if boundary == "quarantine":
        coroutine = voice_runtime._quarantine_task(operation, (private_argument,))
    elif boundary == "capture":
        coroutine = voice_runtime._capture_task_outcome(
            operation, (private_argument,), captured
        )
    else:
        coroutine = voice_runtime._cleanup_owned_task(
            operation, (private_argument,), cleanup_outcome
        )

    task: asyncio.Task[None] | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            task = voice_runtime._create_owned_task(coroutine)
            assert task is not None
            await wait_until(task.done)
            await settle()
            gc.collect()

        assert operation_calls == [private_argument]
        assert self_cancelled_tasks == [task]
        if boundary == "capture":
            assert captured == [(True, returned)]
        else:
            assert captured == []
        assert cleanup_outcome == []
        assert_terminal_owned_task_scrubbed(
            task,
            {id(operation), id(private_argument), id(returned)},
            {private_cancel},
        )
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
        if boundary == "quarantine":
            await assert_manager_path_scrubs_consumed_self_cancellation("worker")
            await assert_manager_path_scrubs_consumed_self_cancellation("stt")
        elif boundary == "capture":
            await assert_manager_path_scrubs_consumed_self_cancellation("executor")
        else:
            await assert_manager_path_scrubs_consumed_self_cancellation("cleanup")
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_worker_launch_then_immediate_retire_scrubs_terminal_cancellation(
    task_factory_name: str,
) -> None:
    """Synchronous retirement cannot leave the unpublished worker Task cancelling."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    factory = SourceFactory()
    runtime = manager_for(parse_enabled(), snapshot.copy, route, factory, FakeStt())
    worker: voice_runtime._Worker | None = None
    task: asyncio.Task[None] | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            runtime._started = True
            runtime._launch_worker(runtime.config.targets[0], route(KEY_A))
            worker = runtime._workers[KEY_A]
            task = worker.task
            assert task is not None
            snapshot.clear()
            runtime.reconcile()
            await wait_until(task.done)
            await settle()
            gc.collect()

        assert factory.calls == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert_terminal_owned_task_scrubbed(
            task,
            {id(runtime), id(runtime.config), id(worker)},
            {route(KEY_A), KEY_A, BINDING_A, TOKEN, ENDPOINT},
        )
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        runtime._started = False
        snapshot.clear()
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_pulse_sleep_self_cancel_return_scrubs_terminal_task(
    task_factory_name: str,
) -> None:
    """An injected pulse sleeper cannot retain a private Task.cancel payload."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    private_cancel = "PRIVATE-PULSE-SLEEP-CANCELLATION"
    source = QueuedSource()
    sleep = SelfCancellingPulseSleep(private_cancel)
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt([PHRASE]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    worker: voice_runtime._Worker | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            await runtime.async_start()
            await source.read_started.wait()
            worker = runtime._workers[KEY_A]
            source.push(VOICE)
            await wait_until(lambda: len(sleep.tasks) == 1)
            pulse = sleep.tasks[0]
            await wait_until(pulse.done)
            await settle()
            gc.collect()

        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_task is None
        assert worker.pulse_token is None
        assert_terminal_owned_task_scrubbed(
            pulse,
            {id(runtime), id(runtime.config), id(worker), id(source), id(VOICE)},
            {
                private_cancel,
                VOICE,
                PHRASE,
                route(KEY_A),
                KEY_A,
                BINDING_A,
                TOKEN,
                ENDPOINT,
            },
        )
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize("guard_count", [1, 2], ids=["marker-yield", "no-owner-resume"])
async def test_create_owned_task_rejects_preadvanced_recognized_boundary(
    task_factory_name: str, guard_count: int
) -> None:
    """Only a fresh recognized coroutine may enter the owned Task boundary."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    operation_calls: list[str] = []

    async def operation() -> None:
        operation_calls.append("operation-ran-before-publication")

    coroutine = voice_runtime._quarantine_task(operation, ())
    creator = asyncio.current_task()
    assert creator is not None
    creator_entry_count = creator.cancelling()
    creator_entry_message = getattr(creator, "_cancel_message", None)
    creator_cancel = "PRIVATE PREADVANCE CREATOR CANCELLATION"
    assert creator.cancel(creator_cancel) is True
    try:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            assert error.args == (creator_cancel,)
        else:
            pytest.fail("preadvance creator cancellation was not delivered")
        creator_count = creator.cancelling()
        creator_message = getattr(creator, "_cancel_message", None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            advances: list[object] = []
            for _ in range(guard_count):
                try:
                    advances.append(coroutine.send(None))
                except StopIteration:
                    advances.append("closed-without-owner")
                    break
            assert advances == (
                [None] if guard_count == 1 else [None, "closed-without-owner"]
            )

            task = voice_runtime._create_owned_task(coroutine)
            assert task is None
            assert operation_calls == []
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
            assert coroutine.cr_frame is None
            del coroutine
            gc.collect()

        assert creator.cancelling() == creator_count
        assert getattr(creator, "_cancel_message", None) is creator_message
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        assert creator.uncancel() == creator_entry_count
        creator._cancel_message = creator_entry_message  # type: ignore[attr-defined]
        loop.set_task_factory(previous_factory)


def _make_test_owned_boundary(
    boundary: str, operation: Callable[[], Awaitable[object]]
) -> Any:
    """Build one private runtime boundary without exposing its outcome containers."""

    if boundary == "quarantine":
        return voice_runtime._quarantine_task(operation, ())
    if boundary == "capture":
        return voice_runtime._capture_task_outcome(operation, (), [])
    return voice_runtime._cleanup_owned_task(operation, (), [])


async def _physically_finish_test_task(task: asyncio.Task[Any]) -> None:
    """Terminally consume test cleanup without invoking Task subclass methods."""

    if not asyncio.Task.done(task):
        asyncio.Task.cancel(task)
    await wait_until(lambda: asyncio.Task.done(task))
    try:
        asyncio.Future.exception(task)
    except BaseException as error:  # noqa: BLE001 - test cleanup consumes all states
        voice_runtime._sanitize_exception_chain(error)


async def test_owned_task_bypasses_hidden_raising_task_factory() -> None:
    """A factory cannot hide a second owner because owned creation bypasses it."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    previous_handler = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    factory_calls = 0
    hidden: list[asyncio.Task[Any]] = []
    operation_calls: list[tuple[bool, asyncio.Task[Any] | None]] = []
    published = False

    class HidingTask(asyncio.Task[Any]):
        def get_loop(self) -> asyncio.AbstractEventLoop:
            return object()  # type: ignore[return-value]

        def done(self) -> bool:
            return True

    async def operation() -> None:
        operation_calls.append((published, asyncio.current_task()))

    def hiding_factory(
        factory_loop: asyncio.AbstractEventLoop,
        owned_coroutine: Any,
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        nonlocal factory_calls
        factory_calls += 1
        task_kwargs = dict(kwargs)
        task_kwargs.pop("eager_start", None)
        task = HidingTask(
            owned_coroutine,
            loop=factory_loop,
            eager_start=False,
            **task_kwargs,
        )
        hidden.append(task)
        raise RuntimeError("PRIVATE HIDDEN FACTORY FAILURE")

    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    loop.set_task_factory(hiding_factory)
    task: asyncio.Task[None] | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            task = voice_runtime._create_owned_task(
                voice_runtime._quarantine_task(operation, ())
            )
            assert task is not None
            assert type(task) is asyncio.Task
            assert factory_calls == 0
            assert hidden == []
            assert operation_calls == []
            published = True
            await task
            await settle()
            gc.collect()

        assert operation_calls == [(True, task)]
        assert_terminal_owned_task_scrubbed(task, {id(operation)})
        assert loop.get_task_factory() is hiding_factory
        assert reports == []
        assert caught == []
    finally:
        loop.set_task_factory(previous_factory)
        for created in hidden:
            await _physically_finish_test_task(created)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await settle()
        loop.set_exception_handler(previous_handler)


async def test_owned_task_ignores_monkeypatched_creation_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned creation uses the import-captured exact Task constructor only once."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    real_task_type = asyncio.Task
    forbidden_calls: list[str] = []
    factory_calls = 0
    duplicates: list[asyncio.Task[Any]] = []
    operation_calls: list[tuple[bool, asyncio.Task[Any] | None]] = []
    published = False

    class SubstituteTask(real_task_type[Any]):
        pass

    def forbidden_create(*_args: Any, **_kwargs: Any) -> NoReturn:
        forbidden_calls.append("create")
        raise RuntimeError("configured creation API was called")

    def duplicate_factory(
        factory_loop: asyncio.AbstractEventLoop,
        owned_coroutine: Any,
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        nonlocal factory_calls
        factory_calls += 1
        task_kwargs = dict(kwargs)
        task_kwargs.pop("eager_start", None)
        duplicates.extend(
            real_task_type(
                owned_coroutine,
                loop=factory_loop,
                eager_start=False,
                **task_kwargs,
            )
            for _ in range(2)
        )
        return duplicates[0]

    async def operation() -> None:
        operation_calls.append((published, asyncio.current_task()))

    loop.set_task_factory(duplicate_factory)
    task: asyncio.Task[None] | None = None
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(asyncio, "Task", SubstituteTask)
            scoped.setattr(asyncio, "create_task", forbidden_create)
            scoped.setattr(loop, "create_task", forbidden_create)
            task = voice_runtime._create_owned_task(
                voice_runtime._quarantine_task(operation, ())
            )
            assert task is not None
            assert type(task) is real_task_type
            assert factory_calls == 0
            assert duplicates == []
            assert forbidden_calls == []
            assert operation_calls == []
            published = True
            await task

        assert operation_calls == [(True, task)]
        assert_terminal_owned_task_scrubbed(task, {id(operation)})
        assert loop.get_task_factory() is duplicate_factory
    finally:
        loop.set_task_factory(previous_factory)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owned_task_constructor_preserves_default_context() -> None:
    """The direct Task constructor captures the caller's current ContextVars."""

    variable: contextvars.ContextVar[object] = contextvars.ContextVar(
        "owned_task_context"
    )
    expected = object()
    token = variable.set(expected)
    observed: list[object] = []

    async def operation() -> None:
        observed.append(variable.get())

    task = voice_runtime._create_owned_task(
        voice_runtime._quarantine_task(operation, ())
    )
    assert task is not None
    assert type(task) is asyncio.Task
    assert observed == []
    variable.reset(token)
    await task

    assert observed == [expected]
    assert_terminal_owned_task_scrubbed(task)


@pytest.mark.parametrize("ownership_stage", ["before", "after"])
@pytest.mark.parametrize("boundary", ["quarantine", "capture", "cleanup"])
async def test_owned_task_constructor_failure_closes_or_consumes_exact_owner(
    monkeypatch: pytest.MonkeyPatch, boundary: str, ownership_stage: str
) -> None:
    """Constructor failure closes unowned work or consumes its captured exact Task."""

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    real_task_type = asyncio.Task
    created: list[asyncio.Task[Any]] = []
    operation_calls: list[str] = []
    capture_outcome: list[tuple[bool, object]] = []
    cleanup_outcome: list[BaseException] = []
    failure = dirty_control_flow(
        RuntimeError("PRIVATE CAPTURED CONSTRUCTOR FAILURE"), "TASK CONSTRUCTOR"
    )

    async def operation() -> None:
        operation_calls.append("ran")

    def failing_constructor(
        owned_coroutine: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        eager_start: bool,
    ) -> asyncio.Task[Any]:
        assert eager_start is False
        if ownership_stage == "after":
            task = real_task_type(owned_coroutine, loop=loop, eager_start=False)
            frame = owned_coroutine.cr_frame
            assert frame is not None
            frame.f_locals["_owned_task_identity"].task = task
            created.append(task)
        raise failure

    if boundary == "quarantine":
        coroutine = voice_runtime._quarantine_task(operation, ())
    elif boundary == "capture":
        coroutine = voice_runtime._capture_task_outcome(operation, (), capture_outcome)
    else:
        coroutine = voice_runtime._cleanup_owned_task(operation, (), cleanup_outcome)

    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    monkeypatch.setattr(voice_runtime, "_OWNED_TASK_CONSTRUCTOR", failing_constructor)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            assert voice_runtime._create_owned_task(coroutine) is None
            if created:
                await wait_until(lambda: asyncio.Task.done(created[0]))
            await settle()
            gc.collect()

        assert operation_calls == []
        assert capture_outcome == []
        assert cleanup_outcome == []
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert coroutine.cr_frame is None
        assert len(created) == (ownership_stage == "after")
        for task in created:
            assert_terminal_owned_task_scrubbed(
                task, {id(operation)}, {"PRIVATE CAPTURED CONSTRUCTOR FAILURE"}
            )
        assert_sanitized_listener_failure(failure)
        assert reports == []
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        for task in created:
            await _physically_finish_test_task(task)
        await settle()
        loop.set_exception_handler(previous_handler)


async def test_direct_start_pulse_rolls_back_when_live_timer_tracking_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live timer that cannot be tracked is cancelled before fixed failure."""

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    worker = voice_runtime._Worker(runtime.config.targets[0], route(KEY_A), 1)
    runtime._started = True
    runtime._workers[KEY_A] = worker
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append(
            (runtime.is_on_for(KEY_A), worker.pulse_token is not None)
        )
    )
    tracked: list[asyncio.Task[None]] = []
    failure = dirty_control_flow(
        RuntimeError("PRIVATE-PULSE-TRACKING-FAILURE"), "PULSE TRACKING"
    )

    def fail_tracking(
        task: asyncio.Task[None], _collection: set[asyncio.Task[None]]
    ) -> None:
        tracked.append(task)
        raise failure

    monkeypatch.setattr(runtime, "_track_task", fail_tracking)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            with pytest.raises(RuntimeError) as raised:
                await runtime._start_pulse(worker)
            assert raised.value.args == ()
            assert raised.value.__cause__ is None
            assert raised.value.__context__ is None
            assert runtime.is_on_for(KEY_A) is False
            assert worker.pulse_token is None
            assert worker.pulse_task is None
            assert worker.pulse_cleanup_task is None
            assert runtime.timer_count == 0
            assert writes == [(True, True), (False, False)]
            assert len(tracked) == 1
            timer = tracked[0]
            await wait_until(timer.done)
            await settle()
            gc.collect()

        assert_terminal_owned_task_scrubbed(timer)
        assert_sanitized_listener_failure(failure)
        assert reports == []
        assert caught == []
    finally:
        for task in tracked:
            if not task.done():
                task.cancel()
        if tracked:
            await asyncio.gather(*tracked, return_exceptions=True)
        runtime._started = False
        runtime._workers.clear()
        await runtime.async_stop()
        await settle()
        loop.set_exception_handler(previous_handler)


async def test_reentrant_successor_pulse_survives_stale_failed_start_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OFF listener's valid successor makes the older failed start stale."""

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    secret = "PRIVATE-REENTRANT-TRACKING-PULSE"
    source = QueuedSource()
    sleep = ControlledSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt([PHRASE]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    tracking_failure = dirty_control_flow(
        RuntimeError(secret), "REENTRANT PULSE TRACKING"
    )
    failed_tracking: list[asyncio.Task[None]] = []
    original_track = runtime._track_task

    def fail_first_timer_tracking(
        task: asyncio.Task[None], collection: set[asyncio.Task[None]]
    ) -> None:
        if collection is runtime._all_timer_tasks and not failed_tracking:
            failed_tracking.append(task)
            raise tracking_failure
        original_track(task, collection)

    monkeypatch.setattr(runtime, "_track_task", fail_first_timer_tracking)

    successor_drivers: list[asyncio.Task[None]] = []
    notifications: list[tuple[bool, object | None, asyncio.Task[None] | None]] = []
    saw_initial_on = False
    successor_installed = False
    worker: voice_runtime._Worker | None = None

    def install_successor_after_rollback() -> None:
        nonlocal saw_initial_on, successor_installed
        assert worker is not None
        notifications.append(
            (runtime.is_on_for(KEY_A), worker.pulse_token, worker.pulse_task)
        )
        if runtime.is_on_for(KEY_A) and worker.pulse_token is not None:
            saw_initial_on = True
            return
        if (
            saw_initial_on
            and not runtime.is_on_for(KEY_A)
            and worker.pulse_token is None
            and not successor_installed
        ):
            successor_installed = True
            successor_drivers.append(
                asyncio.Task(runtime._start_pulse(worker), loop=loop, eager_start=True)
            )

    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            await runtime.async_start()
            await source.read_started.wait()
            worker = runtime._workers[KEY_A]
            runtime.add_listener(install_successor_after_rollback)
            source.push(VOICE)
            await wait_until(lambda: successor_installed and len(sleep.calls) == 1)
            await wait_until(lambda: runtime.in_flight_count == 0)

            assert len(successor_drivers) == 1
            assert asyncio.Task.done(successor_drivers[0]) is True
            assert asyncio.Future.exception(successor_drivers[0]) is None
            assert len(failed_tracking) == 1
            rejected = failed_tracking[0]
            successor = worker.pulse_task
            assert successor is not None
            assert successor is not rejected
            await wait_until(lambda: asyncio.Task.done(rejected))
            assert worker.pulse_task is successor
            assert worker.pulse_token is not None
            assert runtime.is_on_for(KEY_A) is True
            assert runtime.available_for(KEY_A) is True
            assert worker.stt_failed is False
            assert asyncio.Task.done(successor) is False
            assert asyncio.Task.cancelling(successor) == 0
            assert successor in runtime._all_timer_tasks
            assert runtime.timer_count == 1

            sleep.fire(0)
            await wait_until(lambda: asyncio.Task.done(successor))
            await wait_until(lambda: runtime.timer_count == 0)
            await settle()
            gc.collect()

        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_token is None
        assert worker.pulse_task is None
        assert worker.stt_failed is False
        assert any(state[0] is False for state in notifications)
        assert_terminal_owned_task_scrubbed(rejected, protected_values={secret})
        assert_terminal_owned_task_scrubbed(successor)
        assert failed_tracking == [rejected]
        assert_sanitized_listener_failure(tracking_failure)
        assert reports == []
        assert caught == []
    finally:
        for driver in successor_drivers:
            if not asyncio.Task.done(driver):
                asyncio.Task.cancel(driver)
            await wait_until(lambda owned=driver: asyncio.Task.done(owned))
            try:
                asyncio.Future.exception(driver)
            except BaseException as error:  # noqa: BLE001 - test cleanup only
                voice_runtime._sanitize_exception_chain(error)
        await runtime.async_stop()
        await settle()
        loop.set_exception_handler(previous_handler)


async def test_stop_uses_inline_fallback_after_cleanup_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unowned cleanup construction failure falls back to complete inline cleanup."""

    source = QueuedSource()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt(),
    )

    def listener() -> None:
        pass

    runtime.add_listener(listener)
    await runtime.async_start()
    await source.read_started.wait()
    worker = runtime._workers[KEY_A]
    worker_task = worker.task
    assert worker_task is not None
    runtime._available[KEY_A] = True
    runtime._on[KEY_A] = True
    worker.pulse_token = object()

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    failure = dirty_control_flow(
        RuntimeError("PRIVATE CLEANUP CONSTRUCTOR FAILURE"), "CLEANUP CONSTRUCTOR"
    )
    constructor_calls = 0

    def fail_cleanup_constructor(
        _owned_coroutine: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        eager_start: bool,
    ) -> asyncio.Task[Any]:
        nonlocal constructor_calls
        del loop
        assert eager_start is False
        constructor_calls += 1
        raise failure

    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    voice_runtime,
                    "_OWNED_TASK_CONSTRUCTOR",
                    fail_cleanup_constructor,
                )
                await runtime.async_stop()
            await settle()
            gc.collect()

        assert constructor_calls == 1
        assert_sanitized_listener_failure(failure)
        assert_terminal_owned_task_scrubbed(worker_task)
        assert source.closed == 1
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._listeners == []
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
        assert runtime.available_count == 0
        assert runtime.on_count == 0
        assert runtime.timer_count == 0
        assert runtime.in_flight_count == 0
        assert reports == []
        assert caught == []
    finally:
        if runtime._workers or source.closed == 0:
            await runtime.async_stop()
        await settle()
        loop.set_exception_handler(previous_handler)


async def test_manager_bypasses_raising_factory_at_every_owned_callsite() -> None:
    """Worker, STT, executor, pulse, and cleanup publish exact owned Tasks."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    previous_handler = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    factory_calls = 0

    def raising_factory(
        _factory_loop: asyncio.AbstractEventLoop,
        _owned_coroutine: Any,
        **_kwargs: Any,
    ) -> asyncio.Task[Any]:
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError("configured task factory was called")

    source = QueuedSource()
    pulse_sleep = ControlledSleep()
    worker_publication: list[bool] = []
    stt_publication: list[bool] = []
    executor_publication: list[bool] = []
    pulse_publication: list[bool] = []
    cleanup_publication: list[bool] = []
    runtime: VoicePhraseManager

    class ObservingSourceFactory(SourceFactory):
        def __call__(self, binary: str, url: str) -> FakeSource:
            worker = runtime._workers[KEY_A]
            current = asyncio.current_task()
            worker_publication.append(
                worker.task is current and current in runtime._all_worker_tasks
            )
            return super().__call__(binary, url)

    class ObservingStt(FakeStt):
        async def transcribe_pcm(self, pcm: bytes) -> str:
            worker = runtime._workers[KEY_A]
            current = asyncio.current_task()
            stt_publication.append(
                worker.stt_task is current and current in runtime._all_stt_tasks
            )
            return await super().transcribe_pcm(pcm)

    async def observed_executor(
        function: Callable[[object], object], value: object
    ) -> object:
        worker = runtime._workers[KEY_A]
        current = asyncio.current_task()
        executor_publication.append(
            type(current) is asyncio.Task and current is not worker.stt_task
        )
        return await direct_executor(function, value)

    async def observed_sleep(delay: float) -> None:
        worker = runtime._workers[KEY_A]
        current = asyncio.current_task()
        pulse_publication.append(
            worker.pulse_task is current and current in runtime._all_timer_tasks
        )
        await pulse_sleep(delay)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        ObservingSourceFactory([source]),
        ObservingStt([PHRASE]),
        async_executor=observed_executor,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=observed_sleep,
    )
    original_cleanup = runtime._cleanup

    async def observing_cleanup() -> None:
        current = asyncio.current_task()
        cleanup_publication.append(runtime._stop_task is current)
        await original_cleanup()

    runtime._cleanup = observing_cleanup  # type: ignore[method-assign]
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    loop.set_task_factory(raising_factory)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            await runtime.async_start()
            await source.read_started.wait()
            source.push(VOICE)
            await wait_until(lambda: runtime.is_on_for(KEY_A))
            await wait_until(lambda: len(pulse_sleep.calls) == 1)
            await runtime.async_stop()
            await settle()
            gc.collect()

        assert worker_publication == [True]
        assert stt_publication == [True]
        assert executor_publication == [True]
        assert pulse_publication == [True]
        assert cleanup_publication == [True]
        assert factory_calls == 0
        assert loop.get_task_factory() is raising_factory
        assert runtime._workers == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._retiring_by_key == {}
        assert runtime._stop_task is None
        assert reports == []
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        loop.set_task_factory(previous_factory)
        pulse_sleep.fire(0)
        await runtime.async_stop()
        await settle()
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize("guard_count", [1, 2], ids=["one-yield", "two-yields"])
@pytest.mark.parametrize("relation", ["same", "cross"])
@pytest.mark.parametrize("creator_boundary", ["quarantine", "capture", "cleanup"])
async def test_rejected_nested_boundary_never_scrubs_same_code_creator_cancellation(
    task_factory_name: str,
    guard_count: int,
    relation: str,
    creator_boundary: str,
) -> None:
    """A manually advanced child can never claim its same-code creator Task."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    boundary_order = ("quarantine", "capture", "cleanup")
    child_boundary = (
        creator_boundary
        if relation == "same"
        else boundary_order[
            (boundary_order.index(creator_boundary) + 1) % len(boundary_order)
        ]
    )
    secret = f"SECRET-NESTED-{creator_boundary}-{child_boundary}"
    child_operation_calls: list[str] = []
    rejection_results: list[tuple[object, str, object]] = []
    creator_states: list[tuple[int, int, object, frozenset[int], frozenset[int]]] = []
    delivered_cancellations: list[asyncio.CancelledError] = []

    async def child_operation() -> None:
        child_operation_calls.append("ran")

    async def creator_operation() -> None:
        creator = asyncio.current_task()
        if creator is None:
            return
        entry_count = creator.cancelling()
        child = _make_test_owned_boundary(child_boundary, child_operation)
        creator.cancel(secret)
        before_referents = frozenset(map(id, gc.get_referents(creator)))
        for _ in range(guard_count):
            try:
                child.send(None)
            except StopIteration:
                break
        rejected = voice_runtime._create_owned_task(child)
        after_referents = frozenset(map(id, gc.get_referents(creator)))
        creator_states.append(
            (
                entry_count,
                creator.cancelling(),
                getattr(creator, "_cancel_message", None),
                before_referents,
                after_referents,
            )
        )
        rejection_results.append(
            (
                rejected,
                inspect.getcoroutinestate(child),
                getattr(child, "cr_frame", object()),
            )
        )
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            delivered_cancellations.append(error)

    creator_task: asyncio.Task[None] | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            creator_task = voice_runtime._create_owned_task(
                _make_test_owned_boundary(creator_boundary, creator_operation)
            )
            assert creator_task is not None
            await creator_task
            await settle()
            gc.collect()

        assert creator_states == [
            (
                0,
                1,
                secret,
                creator_states[0][3],
                creator_states[0][3],
            )
        ]
        assert rejection_results == [(None, inspect.CORO_CLOSED, None)]
        assert len(delivered_cancellations) == 1
        delivered = delivered_cancellations[0]
        assert delivered.args == (secret,)
        assert delivered.args[0] is secret
        assert child_operation_calls == []
        assert_terminal_owned_task_scrubbed(creator_task, protected_values={secret})
        assert not [
            warning for warning in caught if "was never awaited" in str(warning.message)
        ]
    finally:
        if creator_task is not None and not creator_task.done():
            creator_task.cancel()
            await asyncio.gather(creator_task, return_exceptions=True)
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_create_owned_task_accepts_only_fresh_boundary_and_publishes_first(
    task_factory_name: str,
) -> None:
    """Fresh work uses one exact non-eager Task and publishes before it runs."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    expected_factory: Callable[..., asyncio.Task[Any]] | None = None
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)
        expected_factory = eager_factory

    published = False
    operation_publication: list[bool] = []

    async def operation() -> None:
        operation_publication.append(published)

    task: asyncio.Task[None] | None = None
    try:
        coroutine = voice_runtime._quarantine_task(operation, ())
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED
        task = voice_runtime._create_owned_task(coroutine)
        assert task is not None
        assert type(task) is asyncio.Task
        assert loop.get_task_factory() is expected_factory
        assert operation_publication == []
        published = True
        await task

        assert operation_publication == [True]
        assert task.cancelled() is False
        assert task.exception() is None
        assert task.get_stack() == []
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize(
    "failure_type",
    [
        asyncio.CancelledError,
        SystemExit,
        KeyboardInterrupt,
        SttPipelineControlFlow,
        HostileListenerControlFlow,
    ],
    ids=[
        "cancelled-error",
        "system-exit",
        "keyboard-interrupt",
        "base-exception",
        "hostile-base-exception",
    ],
)
def test_state_listener_baseexceptions_are_repeatedly_sanitized_and_isolated(
    failure_type: type[BaseException],
) -> None:
    """A synchronous observer cannot control lifecycle or block later observers."""

    failure = dirty_control_flow(failure_type("PRIVATE LISTENER ARGUMENT"), "LISTENER")
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    failed_calls = 0
    later_writes: list[tuple[bool, bool]] = []

    def fail_observation() -> None:
        nonlocal failed_calls
        failed_calls += 1
        raise failure

    runtime.add_listener(fail_observation)
    runtime.add_listener(
        lambda: later_writes.append(
            (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
        )
    )

    runtime._set_available(KEY_A, True)
    runtime._notify()
    runtime._set_available(KEY_A, False)

    assert failed_calls == 3
    assert later_writes == [(True, False), (True, False), (False, False)]
    assert BaseException.__getattribute__(failure, "args") == (
        "PRIVATE LISTENER ARGUMENT",
    )
    assert_sanitized_listener_failure(failure)
    assert runtime._all_worker_tasks == set()
    assert runtime._all_stt_tasks == set()
    assert runtime._all_timer_tasks == set()
    assert runtime._retiring_by_key == {}


def test_hostile_descriptor_listener_is_physically_scrubbed_without_hooks() -> None:
    """Listener isolation bypasses every hostile subclass metadata hook."""

    _HOSTILE_DESCRIPTOR_HOOKS.clear()
    failure = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE HOSTILE LISTENER ARGUMENT"),
        "HOSTILE LISTENER",
    )
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    later_calls: list[str] = []

    def fail_observation() -> None:
        raise failure

    runtime.add_listener(fail_observation)
    runtime.add_listener(lambda: later_calls.append("later"))

    runtime._notify()

    assert later_calls == ["later"]
    if physical_exception_slot(failure, "args") != (
        "PRIVATE HOSTILE LISTENER ARGUMENT",
    ):
        pytest.fail("listener exception args changed")
    assert_sanitized_listener_failure(failure)
    assert _HOSTILE_DESCRIPTOR_HOOKS == []


async def test_hostile_stt_cleanup_scrubs_listeners_and_exact_rethrow() -> None:
    """Private STT cleanup cannot retain chains or dispatch hostile hooks."""

    _HOSTILE_DESCRIPTOR_HOOKS.clear()
    primary = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE HOSTILE STT ARGUMENT"),
        "HOSTILE STT",
    )
    listener_failure = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE HOSTILE CLEANUP LISTENER ARGUMENT"),
        "HOSTILE CLEANUP LISTENER",
    )
    private_traceback = physical_exception_slot(primary, "__traceback__")

    class HostileFailureStt:
        def __init__(self) -> None:
            self.calls = 0

        async def transcribe_pcm(self, _pcm: bytes) -> str:
            self.calls += 1
            await asyncio.sleep(0)
            if self.calls == 1:
                return PHRASE
            raise primary

    source = QueuedSource()
    sleep = ControlledSleep()
    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=HostileFailureStt(),
        sleep=sleep,
    )
    failed_calls = 0
    later_writes: list[tuple[bool, bool]] = []

    def fail_cleanup_notification() -> None:
        nonlocal failed_calls
        failed_calls += 1
        raise listener_failure

    runtime.add_listener(fail_cleanup_notification)
    runtime.add_listener(
        lambda: later_writes.append(
            (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
        )
    )
    caught: BaseException | None = None
    try:
        worker = runtime._workers[KEY_A]
        try:
            await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        except BaseException as error:  # noqa: BLE001 - exact object is the contract
            caught = error

        if caught is not primary:
            pytest.fail("STT cleanup did not rethrow the exact control-flow object")
        if physical_exception_slot(primary, "args") != (
            "PRIVATE HOSTILE STT ARGUMENT",
        ):
            pytest.fail("STT control-flow args changed")
        if physical_exception_slot(primary, "__context__") is not None:
            pytest.fail("physical STT context was not scrubbed")
        if physical_exception_slot(primary, "__cause__") is not None:
            pytest.fail("physical STT cause was not scrubbed")
        primary_notes = physical_exception_dict(primary).get("__notes__")
        if primary_notes is not None and primary_notes != []:
            pytest.fail("physical STT notes were not scrubbed")
        if physical_exception_slot(primary, "__suppress_context__") is not False:
            pytest.fail("physical STT suppress-context flag was not scrubbed")
        traceback_chain = physical_traceback_chain(primary)
        assert private_traceback not in traceback_chain
        frame_names = [node.tb_frame.f_code.co_name for node in traceback_chain]
        assert frame_names.count("_raise_sanitized") == 1
        assert "dirty_control_flow" not in frame_names
        assert "transcribe_pcm" not in frame_names

        assert failed_calls == 1
        assert later_writes == [(False, False)]
        assert writes[-1] == (False, False)
        assert_sanitized_listener_failure(listener_failure)
        assert pulse.cancelled()
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert _HOSTILE_DESCRIPTOR_HOOKS == []
    finally:
        await runtime.async_stop()


@pytest.mark.parametrize(
    "pipeline_failure",
    ["stt-ordinary", "executor-ordinary", "stt-control", "executor-control"],
)
@pytest.mark.parametrize(
    "listener_failure_type",
    [
        None,
        asyncio.CancelledError,
        SystemExit,
        KeyboardInterrupt,
        SttPipelineControlFlow,
    ],
    ids=[
        "no-listener-failure",
        "listener-cancelled-error",
        "listener-system-exit",
        "listener-keyboard-interrupt",
        "listener-base-exception",
    ],
)
async def test_stt_failure_defers_sanitized_control_flow_until_pulse_terminal(
    pipeline_failure: str,
    listener_failure_type: type[BaseException] | None,
) -> None:
    """No private exception chain or live old pulse crosses the STT boundary."""

    primary: BaseException
    if pipeline_failure.endswith("ordinary"):
        primary = RuntimeError("PRIVATE STT DETAIL")
    else:
        primary = dirty_control_flow(
            SttPipelineControlFlow(f"{pipeline_failure}-failure"), "PIPELINE"
        )
    listener_failure = (
        None
        if listener_failure_type is None
        else dirty_control_flow(
            listener_failure_type("listener-cleanup-failure"), "LISTENER"
        )
    )
    source = QueuedSource()
    sleep = CancellationResistantPulseSleep()
    stt_calls = 0
    executor_calls = 0

    class FailingStt:
        async def transcribe_pcm(self, _pcm: bytes) -> str:
            nonlocal stt_calls
            stt_calls += 1
            if stt_calls == 2 and pipeline_failure.startswith("stt-"):
                raise primary
            return PHRASE if stt_calls < 3 else "wrong phrase"

    def executor(
        function: Callable[[object], object], value: object
    ) -> asyncio.Future[object]:
        nonlocal executor_calls
        executor_calls += 1
        future = asyncio.get_running_loop().create_future()
        if executor_calls == 2 and pipeline_failure.startswith("executor-"):
            future.set_exception(primary)
        else:
            future.set_result(function(value))
        return future

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FailingStt(),
        async_executor=executor,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
        min_stt_interval_seconds=0,
    )
    listener_failed = False

    def fail_cleanup_notification_once() -> None:
        nonlocal listener_failed
        if (
            listener_failure is not None
            and not listener_failed
            and not runtime.available_for(KEY_A)
            and not runtime.is_on_for(KEY_A)
        ):
            listener_failed = True
            raise listener_failure

    later_writes: list[tuple[bool, bool]] = []
    runtime.add_listener(fail_cleanup_notification_once)
    runtime.add_listener(
        lambda: later_writes.append(
            (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
        )
    )
    release_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        await wait_until(lambda: len(sleep.calls) == 1)
        worker = runtime._workers[KEY_A]
        pulse = worker.pulse_task
        assert pulse is not None and pulse.done() is False

        outcome_finished = False
        cleanup_observation: list[tuple[bool, bool, bool, bool, bool, int, int]] = []

        async def release_old_pulse() -> None:
            await sleep.cancelled.wait()
            cleanup_observation.append(
                (
                    outcome_finished,
                    runtime.available_for(KEY_A),
                    runtime.is_on_for(KEY_A),
                    worker.pulse_token is None,
                    getattr(worker, "pulse_cleanup_task", None) is pulse,
                    runtime.timer_count,
                    len(runtime._all_timer_tasks),
                )
            )
            sleep.fire(0)

        release_task = asyncio.create_task(release_old_pulse())
        caught: BaseException | None = None
        try:
            await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        except BaseException as error:  # noqa: BLE001 - exact identity is the contract
            caught = error
        outcome_finished = True
        await release_task
        release_task = None

        expected = primary if not isinstance(primary, Exception) else None
        assert caught is expected
        if expected is not None:
            assert expected.args == (f"{pipeline_failure}-failure",)
        assert cleanup_observation == [(False, False, False, True, True, 1, 1)]
        assert pulse.done() is True
        assert worker.stt_failed is True
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_task is None
        assert getattr(worker, "pulse_cleanup_task", None) is None
        assert worker.pulse_token is None
        assert runtime.timer_count == 0
        assert later_writes[-1] == (False, False)
        if expected is not None:
            assert expected.__context__ is None
            assert expected.__cause__ is None
            assert getattr(expected, "__notes__", []) == []
            assert expected.__suppress_context__ is False
        if listener_failure is not None:
            assert_sanitized_listener_failure(listener_failure)

        await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        assert stt_calls == 3
        assert worker.stt_failed is False
        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is False
    finally:
        sleep.release_all()
        if release_task is not None:
            await release_task
        await runtime.async_stop()


@pytest.mark.parametrize("failure_stage", ["stt", "executor"])
@pytest.mark.parametrize(
    "failure_type",
    [
        asyncio.CancelledError,
        SystemExit,
        KeyboardInterrupt,
        SttPipelineControlFlow,
    ],
    ids=["cancelled-error", "system-exit", "keyboard-interrupt", "base-exception"],
)
async def test_active_pulse_control_flow_failure_fails_closed_then_recovers(
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type(f"{failure_stage}-control-flow")
    source = QueuedSource()
    sleep = ControlledSleep()
    stt_calls = 0
    executor_calls = 0

    class FailureStt:
        async def transcribe_pcm(self, _pcm: bytes) -> str:
            nonlocal stt_calls
            stt_calls += 1
            if stt_calls == 2 and failure_stage == "stt":
                raise failure
            return PHRASE if stt_calls < 3 else "wrong phrase"

    def executor(
        function: Callable[[object], object], value: object
    ) -> asyncio.Future[object]:
        nonlocal executor_calls
        executor_calls += 1
        future = asyncio.get_running_loop().create_future()
        if executor_calls == 2 and failure_stage == "executor":
            future.set_exception(failure)
        else:
            future.set_result(function(value))
        return future

    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=FailureStt(),
        sleep=sleep,
        executor=executor,
    )
    try:
        worker = runtime._workers[KEY_A]
        assert worker.task is not None and worker.task.done() is False
        try:
            await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        except BaseException as caught:  # noqa: BLE001 - identity is the contract
            assert caught is failure
        else:
            pytest.fail("STT pipeline control flow was swallowed")
        await wait_until(lambda: runtime.timer_count == 0)

        assert worker.stt_failed is True
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_task is None
        assert worker.pulse_token is None
        assert pulse.cancelled()
        assert writes[-1] == (False, False)
        notifications = len(writes)
        await settle()
        assert len(writes) == notifications
        assert worker.task.done() is False

        await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        assert stt_calls == 3
        assert worker.stt_failed is False
        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_task is None
        assert worker.pulse_token is None
        assert runtime.timer_count == 0
    finally:
        await runtime.async_stop()


@pytest.mark.parametrize(
    "failure_type",
    [
        asyncio.CancelledError,
        SystemExit,
        KeyboardInterrupt,
        SttPipelineControlFlow,
    ],
    ids=["cancelled-error", "system-exit", "keyboard-interrupt", "base-exception"],
)
async def test_run_stt_listener_control_flow_isolated_from_availability_and_on(
    failure_type: type[BaseException],
) -> None:
    failure = dirty_control_flow(
        failure_type("available-listener-control-flow"), "RUN STT LISTENER"
    )
    source = QueuedSource()
    stt = FakeStt([PHRASE, "wrong phrase"])

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        min_stt_interval_seconds=0,
    )
    writes: list[tuple[bool, bool]] = []
    failed_calls = 0

    def fail_every_notification() -> None:
        nonlocal failed_calls
        failed_calls += 1
        raise failure

    runtime.add_listener(fail_every_notification)
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    try:
        await runtime.async_start()
        await source.read_started.wait()
        worker = runtime._workers[KEY_A]
        assert worker.task is not None and worker.task.done() is False
        await runtime._run_stt(worker, worker.audio_epoch, VOICE)

        assert worker.stt_failed is False
        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is True
        assert worker.pulse_task is not None
        assert worker.pulse_token is not None
        assert runtime.timer_count == 1
        assert failed_calls == 2
        assert writes == [(True, False), (True, True)]
        assert_sanitized_listener_failure(failure)

        await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        assert len(stt.pcm) == 2
        assert worker.stt_failed is False
        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is True
        assert runtime.timer_count == 1
        assert failed_calls == 2
        assert writes == [(True, False), (True, True)]
        assert worker.task.done() is False
    finally:
        await runtime.async_stop()


@pytest.mark.parametrize("failure_stage", ["eof", "error", "frame-timeout", "vad"])
async def test_later_audio_failure_clears_active_pulse_and_cancels_timer(
    failure_stage: str,
) -> None:
    class LaterFailVad(FakeVad):
        def process(self, frame: bytes) -> float:
            self.frames.append(frame)
            if failure_stage == "vad" and len(self.frames) == 2:
                raise RuntimeError("PRIVATE VAD")
            return 1.0

    source = QueuedSource()
    sleep = ControlledSleep()
    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=FakeStt([PHRASE]),
        sleep=sleep,
        vad_factory=LaterFailVad,
        frame_timeout_seconds=0.05 if failure_stage == "frame-timeout" else 5.0,
    )

    if failure_stage == "eof":
        source.push(None)
    elif failure_stage == "error":
        source.push(RuntimeError("PRIVATE AUDIO"))
    elif failure_stage == "vad":
        source.push(VOICE)
    await wait_until(lambda: source.closed == 1)

    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    assert pulse.cancelled()
    assert writes[-1] == (False, False)
    notifications = len(writes)
    await settle()
    assert len(writes) == notifications
    await runtime.async_stop()


async def assert_reentrant_listener_transition_fails_closed(
    config: VoiceRuntimeConfig,
    transition: str,
    notification: str,
) -> None:
    """Exercise synchronous reconcile from availability and ON notifications."""

    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    current_route: list[str | None] = [route(KEY_A)]
    old = QueuedSource()
    replacement = QueuedSource()
    factory = SourceFactory([old, replacement])
    sleep = ControlledSleep()

    async def matching_executor(
        _function: Callable[[object], object], _value: object
    ) -> object:
        return True

    runtime = VoicePhraseManager(
        config,
        snapshot_provider=lambda: snapshot,
        stream_url_provider=lambda _key: current_route[0],
        ffmpeg_binary="ffmpeg",
        stt_client=FakeStt(["synthetic match"]),
        async_executor=matching_executor,
        source_factory=factory,
        vad_factory=FakeVad,
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    await runtime.async_start()
    await old.read_started.wait()
    worker = runtime._workers[KEY_A]
    if notification == "on":
        runtime._set_available(KEY_A, True)

    writes: list[tuple[bool, bool]] = []
    triggered = False

    def invalidate_from_listener() -> None:
        nonlocal triggered
        state = (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
        writes.append(state)
        trigger_state = (True, False) if notification == "available" else (True, True)
        if triggered or state != trigger_state:
            return
        triggered = True
        if transition == "removal":
            snapshot.clear()
        elif transition == "rebind":
            snapshot[KEY_A] = door(binding=BINDING_B)
        elif transition == "trust-loss":
            snapshot[KEY_A] = door(trusted=False)
        elif transition == "route-loss":
            current_route[0] = None
        else:
            current_route[0] = f"rtsp://127.0.0.1:18555/{KEY_A}"
        runtime.reconcile()

    runtime.add_listener(invalidate_from_listener)
    try:
        await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        assert triggered is True
        await settle()

        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert runtime.timer_count == 0
        assert all(delay != 5.0 for delay, _future in sleep.calls)
        assert writes == (
            [(True, False), (False, False)]
            if notification == "available"
            else [(True, True), (False, False)]
        )

        notifications = len(writes)
        await settle()
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert runtime.timer_count == 0
        assert len(writes) == notifications
    finally:
        await runtime.async_stop()


@pytest.mark.parametrize(
    "transition",
    ["removal", "rebind", "trust-loss", "route-loss"],
)
async def test_invalidating_transition_clears_pulse_timer_and_ignores_stale_match(
    transition: str,
) -> None:
    reentrant_config = parse_enabled()
    reentrant_transitions = (
        (transition, "route-change") if transition == "route-loss" else (transition,)
    )
    for reentrant_transition in reentrant_transitions:
        for notification in ("available", "on"):
            await assert_reentrant_listener_transition_fails_closed(
                reentrant_config, reentrant_transition, notification
            )

    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    current_route: str | None = route(KEY_A)
    source = QueuedSource()
    stt = PulseThenResistantStt()
    sleep = ControlledSleep()
    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: snapshot,
        routes=lambda _key: current_route,
        factory=SourceFactory([source]),
        source=source,
        stt=stt,
        sleep=sleep,
    )
    source.push(VOICE)
    await stt.old_started.wait()

    if transition == "removal":
        snapshot.clear()
    elif transition == "rebind":
        snapshot[KEY_A] = door(binding=BINDING_B)
    elif transition == "trust-loss":
        snapshot[KEY_A] = door(trusted=False)
    else:
        current_route = None
    runtime.reconcile()
    await stt.old_cancelled.wait()

    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    assert pulse.cancelled()
    assert writes[-1] == (False, False)
    notifications = len(writes)
    stt.old_release.set()
    await wait_until(lambda: stt.active == 0)
    await settle()
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    assert len(writes) == notifications
    await runtime.async_stop()


@pytest.mark.parametrize("replacement", ["route-change", "binding-change"])
async def test_same_key_replacement_waits_for_cancellation_resistant_stt_terminal(
    replacement: str,
) -> None:
    snapshot = {KEY_A: door()}
    current_route = route(KEY_A)
    old = QueuedSource()
    new = QueuedSource()
    factory = SourceFactory([old, new])
    stt = PulseThenResistantStt()
    sleep = ControlledSleep()
    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: snapshot,
        routes=lambda _key: current_route,
        factory=factory,
        source=old,
        stt=stt,
        sleep=sleep,
    )
    old.push(VOICE)
    await stt.old_started.wait()

    if replacement == "route-change":
        current_route = f"rtsp://127.0.0.1:18555/{KEY_A}"
        runtime.reconcile()
    else:
        snapshot[KEY_A] = door(binding=BINDING_B)
        runtime.reconcile()
        snapshot[KEY_A] = door()
        runtime.reconcile()
    await stt.old_cancelled.wait()
    await settle()
    state_after_transition = (
        runtime.available_for(KEY_A),
        runtime.is_on_for(KEY_A),
    )
    notifications_after_transition = len(writes)
    replacement_started_early = new.started != 0
    if replacement_started_early:
        new.push(VOICE)
        await stt.replacement_started.wait()
    replacement_stt_started_early = stt.replacement_started.is_set()
    calls_before_old_terminal = stt.calls
    maximum_before_old_terminal = stt.maximum

    stt.old_release.set()
    await wait_until(lambda: new.started == 1)
    if not replacement_started_early:
        new.push(VOICE)
    await stt.replacement_started.wait()
    maximum_after_replacement = stt.maximum
    stt.replacement_release.set()
    await wait_until(lambda: stt.active == 0)
    await settle()

    assert state_after_transition == (False, False)
    assert pulse.cancelled()
    assert replacement_started_early is False
    assert replacement_stt_started_early is False
    assert calls_before_old_terminal == 2
    assert maximum_before_old_terminal == 1
    assert maximum_after_replacement == 1
    assert runtime.is_on_for(KEY_A) is False
    notifications = len(writes)
    assert notifications >= notifications_after_transition
    await settle()
    assert len(writes) == notifications
    await runtime.async_stop()


@pytest.mark.parametrize(
    ("transition", "listener_failure_type"),
    [
        ("removal", asyncio.CancelledError),
        ("rebind", SystemExit),
        ("trust-loss", KeyboardInterrupt),
        ("route-loss", SttPipelineControlFlow),
        ("route-change", HostileListenerControlFlow),
    ],
)
async def test_replacement_waits_for_cancellation_resistant_pulse_terminal(
    transition: str, listener_failure_type: type[BaseException]
) -> None:
    """Listener control flow cannot escape synchronous terminal retirement setup."""

    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    current_route: str | None = route(KEY_A)
    source_release = asyncio.Event()
    old = QueuedSource(close_gate=source_release)
    replacement = QueuedSource()
    factory = SourceFactory([old, replacement])
    sleep = CancellationResistantPulseSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        lambda _key: current_route,
        factory,
        FakeStt([PHRASE]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    try:
        await runtime.async_start()
        await old.read_started.wait()
        old.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        await wait_until(lambda: len(sleep.calls) == 1)
        old_worker = runtime._workers[KEY_A]
        old_task = old_worker.task
        pulse = old_worker.pulse_task
        assert old_task is not None
        assert pulse is not None

        listener_failure = dirty_control_flow(
            listener_failure_type("PRIVATE RETIREMENT LISTENER"), "RETIREMENT"
        )
        failed_calls = 0
        later_writes: list[tuple[bool, bool]] = []

        def fail_retirement_notification() -> None:
            nonlocal failed_calls
            failed_calls += 1
            raise listener_failure

        runtime.add_listener(fail_retirement_notification)
        runtime.add_listener(
            lambda: later_writes.append(
                (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
            )
        )
        tasks_before = {task for task in asyncio.all_tasks() if not task.done()}

        if transition == "removal":
            snapshot.clear()
        elif transition == "rebind":
            snapshot[KEY_A] = door(binding=BINDING_B)
        elif transition == "trust-loss":
            snapshot[KEY_A] = door(trusted=False)
        elif transition == "route-loss":
            current_route = None
        else:
            current_route = f"rtsp://127.0.0.1:18555/{KEY_A}"
        runtime.reconcile()

        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert old_worker.pulse_token is None
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime._all_worker_tasks == {old_task}
        assert old_task.done() is False
        assert old_worker.pulse_cleanup_task is pulse
        assert runtime._all_timer_tasks == {pulse}
        assert failed_calls == 1
        assert later_writes == [(False, False)]
        assert_sanitized_listener_failure(listener_failure)
        assert {task for task in asyncio.all_tasks() if not task.done()} == tasks_before
        assert replacement.started == 0

        snapshot[KEY_A] = door()
        current_route = f"rtsp://127.0.0.1:18556/{KEY_A}"
        runtime.reconcile()
        await sleep.cancelled.wait()
        await wait_until(lambda: old.closed == 1)
        notifications = len(writes)

        for port in range(20_000, 21_000):
            current_route = f"rtsp://127.0.0.1:{port}/{KEY_A}"
            runtime.reconcile()

        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime._all_worker_tasks == {old_task}
        assert old_task.done() is False
        assert old_worker.pulse_cleanup_task is pulse
        assert pulse.done() is False
        assert runtime.timer_count == 1
        assert runtime._all_timer_tasks == {pulse}
        assert sleep.active == sleep.maximum == 1
        assert replacement.started == 0
        assert len(factory.created) == 1
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert len(writes) == notifications

        sleep.fire(0)
        await wait_until(pulse.done)
        await settle()
        assert old_task.done() is False
        assert replacement.started == 0
        assert runtime._retiring_by_key == {KEY_A: old_task}

        source_release.set()
        await replacement.read_started.wait()
        assert old_task.done() is True
        assert pulse.done() is True
        assert current_route is not None
        assert runtime._workers[KEY_A].url == current_route
        assert replacement.started == 1
        assert len(factory.created) == 2
        assert runtime.timer_count == 0
        assert sleep.active == 0
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert len(writes) == notifications
    finally:
        source_release.set()
        sleep.release_all()
        await runtime.async_stop()


async def test_thousand_retrigger_burst_keeps_one_cancellation_resistant_timer() -> (
    None
):
    """A retrigger never overlaps its old timer and excess segments stay bounded."""

    source = QueuedSource()
    sleep = CancellationResistantPulseSleep()
    stt_results: list[str | BaseException] = [PHRASE] * 1_002
    probabilities: list[float | None] = [1.0] * 1_002
    stt = FakeStt(stt_results)

    async def matching_executor(
        _function: Callable[[object], object], _value: object
    ) -> bool:
        await asyncio.sleep(0)
        return True

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        async_executor=matching_executor,
        vad_factory=lambda: FakeVad(probabilities),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
        min_stt_interval_seconds=0,
    )
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        await wait_until(lambda: len(sleep.calls) == 1)
        worker = runtime._workers[KEY_A]
        old_pulse = worker.pulse_task
        old_token = worker.pulse_token
        assert old_pulse is not None
        assert old_token is not None

        source.push(VOICE)
        await sleep.cancelled.wait()
        for _ in range(1_000):
            source.push(VOICE)
        await wait_until(source._items.empty)
        await settle()

        assert len(stt.pcm) == 2
        assert runtime.in_flight_count == 1
        assert worker.pulse_task is old_pulse
        assert worker.pulse_token is not None
        assert worker.pulse_token is not old_token
        assert old_pulse.done() is False
        assert len(sleep.calls) == 1
        assert sleep.active == sleep.maximum == 1
        assert runtime.timer_count == 1
        assert runtime._all_timer_tasks == {old_pulse}
        assert runtime.is_on_for(KEY_A) is True

        sleep.fire(0)
        await wait_until(lambda: len(sleep.calls) == 2)
        await wait_until(lambda: runtime.in_flight_count == 0)
        latest_pulse = worker.pulse_task
        latest_token = worker.pulse_token
        assert latest_pulse is not None and latest_pulse is not old_pulse
        assert latest_token is not None and latest_token is not old_token
        assert old_pulse.done() is True
        assert latest_pulse.done() is False
        assert sleep.active == sleep.maximum == 1
        assert runtime.timer_count == 1
        assert runtime._all_timer_tasks == {latest_pulse}
        assert runtime.is_on_for(KEY_A) is True

        sleep.fire(1)
        await wait_until(latest_pulse.done)
        await settle()
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_token is None
        assert worker.pulse_task is None
        assert runtime.timer_count == 0
        assert sleep.active == 0
    finally:
        sleep.release_all()
        await runtime.async_stop()


async def test_executor_cancellation_waits_terminal_and_preserves_exact_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_error = RuntimeError("PRIVATE MATCHER FAILURE")
    matcher = ThreadBlockedMatcher(terminal_error)
    monkeypatch.setattr(voice_runtime.PhraseMatcher, "matches", matcher.matches)
    source = QueuedSource()
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=route,
        ffmpeg_binary="ffmpeg",
        stt_client=FakeStt([PHRASE]),
        source_factory=SourceFactory([source]),
        vad_factory=FakeVad,
        segmenter_factory=EveryFrameSegmenter,
    )
    matcher_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await source.read_started.wait()
        worker = runtime._workers[KEY_A]
        runtime._set_available(KEY_A, True)
        listener_failure = dirty_control_flow(
            HostileListenerControlFlow("PRIVATE CANCEL LISTENER"), "CANCEL"
        )
        later_writes: list[tuple[bool, bool]] = []

        def fail_cancellation_cleanup() -> None:
            raise listener_failure

        runtime.add_listener(fail_cancellation_cleanup)
        runtime.add_listener(
            lambda: later_writes.append(
                (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
            )
        )
        matcher_task = asyncio.create_task(
            runtime._run_stt(worker, worker.audio_epoch, VOICE)
        )
        await wait_until(lambda: matcher.snapshot()[0] == 1)

        matcher_task.cancel("first-matcher-cancel")
        await settle()
        assert matcher_task.done() is False
        frame = matcher_task.get_coro().cr_frame
        assert frame is not None
        first_cancellation = frame.f_locals.get("cancellation")
        assert isinstance(first_cancellation, asyncio.CancelledError)
        assert first_cancellation.args == ("first-matcher-cancel",)

        matcher_task.cancel("second-matcher-cancel")
        await settle()
        frame = matcher_task.get_coro().cr_frame
        assert frame is not None
        assert frame.f_locals.get("cancellation") is first_cancellation
        assert matcher_task.done() is False

        matcher.release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await matcher_task
        assert caught.value is first_cancellation
        assert caught.value.args == ("first-matcher-cancel",)
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert runtime.timer_count == 0
        assert later_writes == [(False, False)]
        assert_sanitized_listener_failure(listener_failure)
    finally:
        matcher.release.set()
        if matcher_task is not None:
            try:
                await matcher_task
            except asyncio.CancelledError:
                pass
        await runtime.async_stop()
        await wait_until(lambda: matcher.snapshot()[1] == 0)


class MatcherControlFlow(BaseException):
    pass


@pytest.mark.parametrize(
    "failure",
    [MatcherControlFlow("control-flow"), asyncio.CancelledError("executor-cancel")],
    ids=["base-exception", "cancelled-error"],
)
async def test_executor_preserves_terminal_control_flow_identity_without_cancellation(
    failure: BaseException,
) -> None:
    async def failing_executor(
        _function: Callable[[object], object], _value: object
    ) -> object:
        await asyncio.sleep(0)
        raise failure

    source = QueuedSource()
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=route,
        ffmpeg_binary="ffmpeg",
        stt_client=FakeStt([PHRASE]),
        async_executor=failing_executor,
        source_factory=SourceFactory([source]),
        vad_factory=FakeVad,
        segmenter_factory=EveryFrameSegmenter,
    )
    try:
        await runtime.async_start()
        await source.read_started.wait()
        worker = runtime._workers[KEY_A]
        try:
            await runtime._run_stt(worker, worker.audio_epoch, VOICE)
        except BaseException as caught:  # noqa: BLE001 - identity is the contract
            assert caught is failure
        else:
            pytest.fail("executor control flow was swallowed")
    finally:
        await runtime.async_stop()


async def test_worker_task_waits_for_stt_terminal_then_finishes_cleanly() -> None:
    source = QueuedSource()
    stt = PulseThenResistantStt()
    runtime, _writes, _pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=stt,
        sleep=ControlledSleep(),
    )
    source.push(VOICE)
    await stt.old_started.wait()
    worker_task = runtime._workers[KEY_A].task
    assert worker_task is not None
    original_consume = runtime._consume_task
    worker_outcomes: list[BaseException | None] = []

    def capture_worker_cancellation(completed: asyncio.Task[None]) -> None:
        if completed is not worker_task:
            original_consume(completed)
            return
        try:
            worker_outcomes.append(completed.exception())
        except asyncio.CancelledError as error:
            worker_outcomes.append(error)

    runtime._consume_task = capture_worker_cancellation  # type: ignore[method-assign]
    worker_task.cancel("first-worker-cancel")
    await stt.old_cancelled.wait()
    assert worker_task.done() is False
    worker_task.cancel("second-worker-cancel")
    await settle()
    assert worker_task.done() is False

    stt.old_release.set()
    await wait_until(worker_task.done)
    await settle()
    assert worker_outcomes == [None]
    assert worker_task.cancelled() is False
    assert worker_task.result() is None
    await runtime.async_stop()


async def test_stop_clears_active_pulse_and_cancels_timer_without_late_notice() -> None:
    source = QueuedSource()
    sleep = ControlledSleep()
    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=FakeStt([PHRASE]),
        sleep=sleep,
    )

    await runtime.async_stop()
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    assert runtime.timer_count == 0
    assert pulse.cancelled()
    notifications = len(writes)
    await settle()
    assert len(writes) == notifications


async def test_stop_cancellation_waits_for_resistant_pulse_terminal() -> None:
    source = QueuedSource()
    sleep = CancellationResistantPulseSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt([PHRASE]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    stop_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        worker = runtime._workers[KEY_A]
        pulse = worker.pulse_task
        assert pulse is not None

        stop_task = asyncio.create_task(runtime.async_stop())
        await sleep.cancelled.wait()
        stop_task.cancel("first-stop-cancellation")
        await settle()
        frame = stop_task.get_coro().cr_frame  # type: ignore[union-attr]
        assert frame is not None
        cancellation = frame.f_locals.get("cancellation")
        assert isinstance(cancellation, asyncio.CancelledError)
        assert cancellation.args == ("first-stop-cancellation",)
        stop_task.cancel("later-stop-cancellation")
        await settle()

        assert stop_task.done() is False
        assert pulse.done() is False
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_token is None
        assert runtime.timer_count == 1
        assert sleep.active == sleep.maximum == 1

        sleep.fire(0)
        with pytest.raises(asyncio.CancelledError) as caught:
            await stop_task
        assert caught.value is cancellation
        stop_task = None
        assert pulse.done() is True
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert sleep.active == 0
        assert not runtime._all_worker_tasks
        assert not runtime._all_stt_tasks
        assert not runtime._all_timer_tasks
        assert not runtime._retiring_by_key
    finally:
        sleep.release_all()
        if stop_task is not None:
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
        await runtime.async_stop()


class PulseTimerControlFlow(BaseException):
    pass


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("pulse sleep failed"), PulseTimerControlFlow("pulse control flow")],
    ids=["ordinary-exception", "base-exception"],
)
async def test_pulse_sleep_failure_clears_state_and_task_finishes_cleanly(
    failure: BaseException,
) -> None:
    source = QueuedSource()
    sleep = ReleasedFailureSleep(failure)
    runtime = VoicePhraseManager(
        parse_enabled(),
        snapshot_provider=lambda: {KEY_A: door()},
        stream_url_provider=route,
        ffmpeg_binary="ffmpeg",
        stt_client=FakeStt([PHRASE]),
        async_executor=direct_executor,
        source_factory=SourceFactory([source]),
        vad_factory=lambda: FakeVad([1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
    )
    writes: list[tuple[bool, bool]] = []
    listener_failure = dirty_control_flow(
        HostileListenerControlFlow("PRIVATE PULSE LISTENER"), "PULSE"
    )
    failed_calls = 0
    pulse: asyncio.Task[None] | None = None
    outcomes: list[BaseException | None] = []
    original_consume = runtime._consume_task

    def fail_pulse_notification() -> None:
        nonlocal failed_calls
        failed_calls += 1
        raise listener_failure

    def capture_pulse_outcome(completed: asyncio.Task[None]) -> None:
        if completed is pulse:
            try:
                outcome = completed.exception()
            except asyncio.CancelledError as caught:
                outcomes.append(caught)
            else:
                outcomes.append(outcome)
        original_consume(completed)

    runtime._consume_task = capture_pulse_outcome  # type: ignore[method-assign]
    runtime.add_listener(fail_pulse_notification)
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await sleep.started.wait()
        worker = runtime._workers[KEY_A]
        pulse = worker.pulse_task
        assert pulse is not None
        assert worker.pulse_token is not None
        assert runtime.is_on_for(KEY_A) is True

        sleep.release.set()
        await wait_until(pulse.done)
        await settle()

        assert outcomes == [None]
        assert pulse.cancelled() is False
        assert pulse.result() is None
        assert runtime.available_for(KEY_A) is True
        assert runtime.is_on_for(KEY_A) is False
        assert worker.pulse_token is None
        assert worker.pulse_task is None
        assert not runtime._all_timer_tasks
        assert writes == [(True, False), (True, True), (True, False)]
        assert failed_calls == 3
        assert_sanitized_listener_failure(listener_failure)
    finally:
        sleep.release.set()
        await runtime.async_stop()


async def test_exact_match_pulses_five_seconds_retrigger_replaces_timer_and_notifies() -> (
    None
):
    sleep = ControlledSleep()
    frames, probabilities = two_segments()
    stt = FakeStt([PHRASE, PHRASE])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([FakeSource(frames)]),
        stt,
        vad_factory=lambda: FakeVad(probabilities),
        sleep=sleep,
        min_stt_interval_seconds=0,
    )
    writes: list[tuple[bool, bool]] = []
    listener_failure = dirty_control_flow(
        SttPipelineControlFlow("PRIVATE PULSE EXPIRY LISTENER"), "PULSE EXPIRY"
    )
    failed_calls = 0

    def fail_pulse_notifications() -> None:
        nonlocal failed_calls
        failed_calls += 1
        raise listener_failure

    runtime.add_listener(fail_pulse_notifications)
    unsubscribe = runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    await runtime.async_start()
    await settle(350)
    pulse_sleeps = [(delay, future) for delay, future in sleep.calls if delay == 5.0]
    assert len(stt.pcm) == 2
    assert len(pulse_sleeps) == 2
    assert pulse_sleeps[0][1].cancelled()
    worker = runtime._workers[KEY_A]
    latest_token = worker.pulse_token
    latest_task = worker.pulse_task
    assert latest_token is not None
    assert latest_task is not None
    assert latest_task.done() is False
    assert runtime.is_on_for(KEY_A) is True
    assert (True, True) in writes
    await settle()
    assert worker.pulse_token is latest_token
    assert worker.pulse_task is latest_task
    assert runtime.is_on_for(KEY_A) is True

    sleep.fire(sleep.calls.index(pulse_sleeps[1]))
    await settle()
    assert runtime.is_on_for(KEY_A) is False
    assert worker.pulse_token is None
    assert worker.pulse_task is None
    assert not runtime._all_timer_tasks
    assert writes[-1] == (True, False)
    assert failed_calls == 3
    assert_sanitized_listener_failure(listener_failure)
    unsubscribe()
    await runtime.async_stop()


async def test_wrong_transcript_never_pulses() -> None:
    frames = [VOICE] * MIN_VOICED_FRAMES + [SILENCE] * END_SILENCE_FRAMES
    probabilities = [1.0] * MIN_VOICED_FRAMES + [0.0] * END_SILENCE_FRAMES
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([FakeSource(frames)]),
        FakeStt(["definitely wrong"]),
        vad_factory=lambda: FakeVad(probabilities),
    )
    await runtime.async_start()
    await settle(200)
    assert runtime.available_for(KEY_A) is True
    assert runtime.is_on_for(KEY_A) is False
    await runtime.async_stop()


async def test_removal_cancels_worker_and_clears_availability_and_pulse() -> None:
    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    source = FakeSource([SILENCE])
    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        route,
        SourceFactory([source]),
        FakeStt(),
        vad_factory=lambda: FakeVad([0.0]),
    )
    await runtime.async_start()
    await settle()
    assert runtime.available_for(KEY_A) is True
    snapshot.clear()
    runtime.reconcile()
    await settle()
    assert runtime.worker_count == 0
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    assert source.closed == 1
    await runtime.async_stop()


async def test_rebind_waits_for_old_worker_cleanup_before_new_worker_starts() -> None:
    snapshot = {KEY_A: door()}
    close_gate = asyncio.Event()
    old = FakeSource([SILENCE], close_gate=close_gate)
    new = FakeSource([SILENCE])
    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        route,
        SourceFactory([old, new]),
        FakeStt(),
        vad_factory=lambda: FakeVad([0.0]),
    )
    await runtime.async_start()
    await settle()
    assert old.started == 1
    snapshot[KEY_A] = door(binding=BINDING_B)
    runtime.reconcile()
    await settle()
    assert old.closed == 1
    assert new.started == 0
    assert runtime.worker_count == 0

    snapshot[KEY_A] = door()
    runtime.reconcile()
    await settle()
    assert new.started == 0
    close_gate.set()
    await settle()
    assert new.started == 1
    await runtime.async_stop()


async def test_thousand_synchronous_route_cycles_wait_for_exact_source_close() -> None:
    current_route: str | None = route(KEY_A)
    close_gate = asyncio.Event()
    old = FakeSource([SILENCE], close_gate=close_gate)
    replacement = FakeSource([SILENCE])
    factory = SourceFactory([old, replacement])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        lambda _key: current_route,
        factory,
        FakeStt(),
        vad_factory=lambda: FakeVad([0.0]),
    )
    try:
        await runtime.async_start()
        await old.read_started.wait()
        old_task = runtime._workers[KEY_A].task
        assert old_task is not None

        current_route = None
        runtime.reconcile()
        await wait_until(lambda: old.closed == 1)

        for port in range(23_000, 24_000):
            current_route = None
            runtime.reconcile()
            current_route = f"rtsp://127.0.0.1:{port}/{KEY_A}"
            runtime.reconcile()

        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime._all_worker_tasks == {old_task}
        assert len(factory.created) == 1
        assert old.closed == 1
        assert replacement.started == 0
        assert runtime._all_worker_tasks == {old_task}

        close_gate.set()
        await replacement.read_started.wait()
        assert replacement.started == 1
        assert [url for _, url in factory.calls] == [route(KEY_A), current_route]
    finally:
        close_gate.set()
        await runtime.async_stop()


async def test_thousand_synchronous_binding_trust_cycles_use_latest_snapshot() -> None:
    snapshot = {KEY_A: door()}
    current_route = route(KEY_A)
    close_gate = asyncio.Event()
    old = FakeSource([SILENCE], close_gate=close_gate)
    replacement = FakeSource([SILENCE])
    factory = SourceFactory([old, replacement])
    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        lambda _key: current_route,
        factory,
        FakeStt(),
        vad_factory=lambda: FakeVad([0.0]),
    )
    writes: list[tuple[bool, bool]] = []
    try:
        await runtime.async_start()
        await wait_until(lambda: runtime.available_for(KEY_A))
        runtime.add_listener(
            lambda: writes.append(
                (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
            )
        )
        old_task = runtime._workers[KEY_A].task
        assert old_task is not None

        snapshot[KEY_A] = door(binding=BINDING_B)
        runtime.reconcile()
        await wait_until(lambda: old.closed == 1)

        for port in range(24_000, 25_000):
            snapshot[KEY_A] = door(binding=BINDING_B)
            runtime.reconcile()
            snapshot[KEY_A] = door(trusted=False)
            runtime.reconcile()
            snapshot[KEY_A] = door()
            current_route = f"rtsp://127.0.0.1:{port}/{KEY_A}"
            runtime.reconcile()

        assert runtime._workers == {}
        assert runtime._retiring_by_key == {KEY_A: old_task}
        assert runtime._all_worker_tasks == {old_task}
        assert len(factory.created) == 1
        assert writes == [(False, False)]

        assert replacement.started == 0
        close_gate.set()
        await replacement.read_started.wait()

        current = runtime._workers[KEY_A]
        assert current.target.binding == BINDING_A
        assert current.url == current_route
        assert [url for _, url in factory.calls] == [route(KEY_A), current_route]
        assert len(runtime._all_worker_tasks) == 1
        assert writes == [(False, False)]
    finally:
        close_gate.set()
        await runtime.async_stop()


async def test_maximum_keys_bound_synchronous_churn_and_isolate_retirements() -> None:
    keys = tuple(f"{index:064x}" for index in range(1, MAX_VOICE_TARGETS + 1))
    bindings = tuple(f"{index:064x}" for index in range(17, 17 + MAX_VOICE_TARGETS))
    stored = phrase_storage()
    config = parse_enabled(
        {key: (binding, stored) for key, binding in zip(keys, bindings, strict=True)}
    )
    snapshot = {
        key: door(key, binding) for key, binding in zip(keys, bindings, strict=True)
    }
    routes = {key: route(key) for key in keys}
    close_gates = [asyncio.Event() for _ in keys]
    old_sources = [FakeSource(close_gate=gate) for gate in close_gates]
    replacements = [FakeSource() for _ in keys]
    factory = SourceFactory([*old_sources, *replacements])
    runtime = manager_for(
        config,
        lambda: snapshot,
        routes.get,
        factory,
        FakeStt(),
    )
    try:
        await runtime.async_start()
        await asyncio.gather(*(source.read_started.wait() for source in old_sources))
        old_tasks = {key: runtime._workers[key].task for key in keys}
        assert all(task is not None for task in old_tasks.values())

        snapshot.clear()
        runtime.reconcile()
        await wait_until(lambda: all(source.closed == 1 for source in old_sources))

        for cycle in range(1_000):
            for key, binding in zip(keys, bindings, strict=True):
                snapshot[key] = door(key, binding, trusted=False)
                runtime.reconcile()
                snapshot[key] = door(key, binding)
                routes[key] = f"rtsp://127.0.0.1:{30_000 + cycle}/{key}"
                runtime.reconcile()

        assert runtime._workers == {}
        assert runtime._retiring_by_key == old_tasks
        assert runtime._all_worker_tasks == set(old_tasks.values())
        assert len(runtime._all_worker_tasks) == MAX_VOICE_TARGETS
        assert len(factory.created) == MAX_VOICE_TARGETS
        assert all(source.closed == 1 for source in old_sources)

        close_gates[0].set()
        await replacements[0].read_started.wait()
        assert set(runtime._workers) == {keys[0]}
        assert set(runtime._retiring_by_key) == set(keys[1:])
        assert len(factory.created) == MAX_VOICE_TARGETS + 1
        assert all(source.started == 0 for source in replacements[1:])

        for gate in close_gates[1:]:
            gate.set()
        await asyncio.gather(*(source.read_started.wait() for source in replacements))
        assert set(runtime._workers) == set(keys)
        assert runtime._retiring_by_key == {}
        assert len(runtime._all_worker_tasks) == MAX_VOICE_TARGETS
        assert len(factory.created) == MAX_VOICE_TARGETS * 2
        for key in keys:
            assert [url for _, url in factory.calls if url.endswith(f"/{key}")] == [
                route(key),
                routes[key],
            ]
    finally:
        for gate in close_gates:
            gate.set()
        await runtime.async_stop()


async def test_replacement_waits_until_same_source_close_retry_succeeds() -> None:
    current_route = route(KEY_A)
    old = RetryCloseSource(failures_before_success=1)
    replacement = QueuedSource()
    factory = SourceFactory([old, replacement])
    cleanup_sleep = ControlledSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        lambda _key: current_route,
        factory,
        FakeStt(),
        cleanup_retry_sleep=cleanup_sleep,
        cleanup_retry_seconds=0.25,
    )
    try:
        await runtime.async_start()
        await old.read_started.wait()
        current_route = f"rtsp://127.0.0.1:18555/{KEY_A}"
        runtime.reconcile()
        await wait_until(lambda: old.close_attempts == 1)
        await wait_until(lambda: len(cleanup_sleep.calls) == 1)

        assert old.close_successes == 0
        assert replacement.started == 0
        assert len(factory.created) == 1
        assert cleanup_sleep.calls[0][0] == 0.25
        assert sum(not task.done() for task in runtime._all_worker_tasks) == 1
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False

        cleanup_sleep.fire(0)
        await replacement.read_started.wait()
        assert old.close_attempts == 2
        assert old.close_successes == 1
        assert replacement.started == 1
        assert [url for _, url in factory.calls] == [route(KEY_A), current_route]
    finally:
        old.permit_close()
        for _delay, future in cleanup_sleep.calls:
            if not future.done():
                future.set_result(None)
        await runtime.async_stop()


@pytest.mark.parametrize(
    "failure",
    [SystemExit("close-exit"), KeyboardInterrupt("close-interrupt")],
    ids=["system-exit", "keyboard-interrupt"],
)
async def test_source_close_control_flow_waits_for_success_then_reraises_exact(
    failure: BaseException,
) -> None:
    source = ControlFlowCloseSource(failure)
    source.push(RuntimeError("PRIVATE READ FAILURE"))
    factory = SourceFactory([source])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        factory,
        FakeStt(),
        cleanup_retry_sleep=no_delay_sleep,
        cleanup_retry_seconds=0.125,
    )
    worker = voice_runtime._Worker(runtime.config.targets[0], route(KEY_A), 1)
    runtime._started = True
    runtime._workers[KEY_A] = worker
    try:
        await runtime._worker_loop(worker)
    except BaseException as caught:  # noqa: BLE001 - exact identity is the contract
        assert caught is failure
    else:
        pytest.fail("source close control flow was swallowed")
    finally:
        runtime._started = False
        runtime._workers.clear()

    assert source.started == 1
    assert source.close_attempts == 2
    assert source.close_successes == 1
    assert len(factory.created) == 1
    assert factory.calls == [("ffmpeg", route(KEY_A))]


@pytest.mark.parametrize(
    "failure",
    [SystemExit("sleep-exit"), KeyboardInterrupt("sleep-interrupt")],
    ids=["system-exit", "keyboard-interrupt"],
)
async def test_cleanup_sleep_control_flow_waits_for_close_then_reraises_exact(
    failure: BaseException,
) -> None:
    source = RetryCloseSource(failures_before_success=1)
    source.push(RuntimeError("PRIVATE READ FAILURE"))
    cleanup_sleep = OneControlFlowSleep(failure)
    factory = SourceFactory([source])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        factory,
        FakeStt(),
        cleanup_retry_sleep=cleanup_sleep,
        cleanup_retry_seconds=0.001,
    )
    worker = voice_runtime._Worker(runtime.config.targets[0], route(KEY_A), 1)
    runtime._started = True
    runtime._workers[KEY_A] = worker
    try:
        await runtime._worker_loop(worker)
    except BaseException as caught:  # noqa: BLE001 - exact identity is the contract
        assert caught is failure
    else:
        pytest.fail("cleanup sleep control flow was swallowed")
    finally:
        runtime._started = False
        runtime._workers.clear()

    assert source.started == 1
    assert source.close_attempts == 2
    assert source.close_successes == 1
    assert cleanup_sleep.delays == [0.001]
    assert len(factory.created) == 1
    assert factory.calls == [("ffmpeg", route(KEY_A))]


class CleanupRetryControlFlow(BaseException):
    pass


async def test_cleanup_sleeper_failure_permanently_switches_to_yielding_retry() -> None:
    failure = CleanupRetryControlFlow("first-cleanup-interruption")
    heartbeat = asyncio.Event()

    class YieldObservedCloseSource(RetryCloseSource):
        def __init__(self) -> None:
            super().__init__(failures_before_success=1)
            self.heartbeat_before_second_attempt: bool | None = None

        async def async_close(self) -> None:
            if self.close_attempts == 1:
                self.heartbeat_before_second_attempt = heartbeat.is_set()
            await super().async_close()

    source = YieldObservedCloseSource()
    source.push(RuntimeError("PRIVATE READ FAILURE"))
    cleanup_sleep = BurstControlFlowSleep(failure, heartbeat)
    factory = SourceFactory([source])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        factory,
        FakeStt(),
        cleanup_retry_sleep=cleanup_sleep,
        cleanup_retry_seconds=0.01,
    )
    worker = voice_runtime._Worker(runtime.config.targets[0], route(KEY_A), 1)
    runtime._started = True
    runtime._workers[KEY_A] = worker
    worker_task = asyncio.create_task(runtime._worker_loop(worker))
    try:
        await heartbeat.wait()
        assert cleanup_sleep.calls == 1
        for attempt in range(3):
            worker_task.cancel(f"later-cleanup-cancellation-{attempt}")
            await asyncio.sleep(0)
            assert worker_task.done() is False
            assert source.close_attempts == 1
        try:
            await worker_task
        except BaseException as caught:  # noqa: BLE001 - exact identity is the contract
            assert caught is failure
        else:
            pytest.fail("cleanup sleeper control flow was swallowed")
    finally:
        runtime._started = False
        runtime._workers.clear()
        if not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except BaseException:  # noqa: BLE001, S110 - test cleanup owns task
                pass

    heartbeat_task = cleanup_sleep.heartbeat_task
    assert heartbeat_task is not None
    await heartbeat_task
    assert cleanup_sleep.calls == 1
    assert source.started == 1
    assert source.close_attempts == 2
    assert source.close_successes == 1
    assert source.heartbeat_before_second_attempt is True
    assert factory.created == [source]
    assert factory.calls == [("ffmpeg", route(KEY_A))]


@pytest.mark.parametrize(
    "cleanup_failure",
    [SystemExit("close-exit"), KeyboardInterrupt("close-interrupt")],
    ids=["system-exit", "keyboard-interrupt"],
)
async def test_existing_cancellation_precedes_cleanup_control_flow_after_close(
    cleanup_failure: BaseException,
) -> None:
    cancellation = asyncio.CancelledError("original-cancellation")
    source = ControlFlowCloseSource(cleanup_failure)
    source.push(cancellation)
    factory = SourceFactory([source])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        factory,
        FakeStt(),
        cleanup_retry_sleep=no_delay_sleep,
    )
    worker = voice_runtime._Worker(runtime.config.targets[0], route(KEY_A), 1)
    runtime._started = True
    runtime._workers[KEY_A] = worker
    runtime._available[KEY_A] = True
    listener_failure = dirty_control_flow(
        SttPipelineControlFlow("PRIVATE SOURCE LISTENER"), "SOURCE LISTENER"
    )
    later_writes: list[tuple[bool, bool]] = []

    def fail_source_cleanup() -> None:
        raise listener_failure

    runtime.add_listener(fail_source_cleanup)
    runtime.add_listener(
        lambda: later_writes.append(
            (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
        )
    )
    try:
        await runtime._worker_loop(worker)
    except BaseException as caught:  # noqa: BLE001 - exact identity is the contract
        assert caught is cancellation
    else:
        pytest.fail("worker cancellation was swallowed")
    finally:
        runtime._started = False
        runtime._workers.clear()

    assert source.close_attempts == 2
    assert source.close_successes == 1
    assert len(factory.created) == 1
    assert factory.calls == [("ffmpeg", route(KEY_A))]
    assert later_writes == [(False, False)]
    assert_sanitized_listener_failure(listener_failure)


async def test_persistent_close_retry_is_single_bounded_cleanup_and_stop_waits() -> (
    None
):
    current_route = route(KEY_A)
    old = RetryCloseSource(failures_before_success=None)
    restarted = QueuedSource()
    factory = SourceFactory([old, restarted])
    cleanup_sleep = ControlledSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        lambda _key: current_route,
        factory,
        FakeStt(),
        cleanup_retry_sleep=cleanup_sleep,
        cleanup_retry_seconds=0.125,
    )
    stop_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await old.read_started.wait()
        current_route = f"rtsp://127.0.0.1:18555/{KEY_A}"
        runtime.reconcile()

        for retry in range(20):
            await wait_until(lambda retry=retry: len(cleanup_sleep.calls) > retry)
            assert old.close_attempts == retry + 1
            assert old.close_successes == 0
            assert len(factory.created) == 1
            assert sum(not task.done() for task in runtime._all_worker_tasks) == 1
            assert sum(not future.done() for _delay, future in cleanup_sleep.calls) == 1
            assert cleanup_sleep.calls[retry][0] == 0.125
            cleanup_sleep.fire(retry)

        await wait_until(lambda: len(cleanup_sleep.calls) == 21)
        assert old.close_attempts == 21
        stop_task = asyncio.create_task(runtime.async_stop())
        await settle()
        assert stop_task.done() is False
        assert len(factory.created) == 1
        assert sum(not task.done() for task in runtime._all_worker_tasks) == 1
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False

        old.permit_close()
        cleanup_sleep.fire(20)
        await stop_task
        stop_task = None
        assert old.close_attempts == 22
        assert old.close_successes == 1
        assert not runtime._all_worker_tasks
        assert not runtime._retiring_by_key

        await runtime.async_start()
        await restarted.read_started.wait()
        assert len(factory.created) == 2
        assert restarted.started == 1
    finally:
        old.permit_close()
        for _delay, future in cleanup_sleep.calls:
            if not future.done():
                future.set_result(None)
        if stop_task is not None:
            await stop_task
        await runtime.async_stop()


class CancellationResistantStt:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = 0

    async def transcribe_pcm(self, _pcm: bytes) -> str:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            await self.release.wait()
        return PHRASE


async def test_obsolete_late_stt_result_is_ignored_after_removal() -> None:
    snapshot: dict[str, DiscoveredDoor] = {KEY_A: door()}
    frames = [VOICE] * MIN_VOICED_FRAMES + [SILENCE] * END_SILENCE_FRAMES
    probabilities = [1.0] * MIN_VOICED_FRAMES + [0.0] * END_SILENCE_FRAMES
    stt = CancellationResistantStt()
    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        route,
        SourceFactory([FakeSource(frames)]),
        stt,
        vad_factory=lambda: FakeVad(probabilities),
    )
    await runtime.async_start()
    await stt.started.wait()
    snapshot.clear()
    runtime.reconcile()
    await settle()
    assert stt.cancelled == 1
    stt.release.set()
    await settle()
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    await runtime.async_stop()


async def test_stop_cancels_and_awaits_cancellation_resistant_stt() -> None:
    frames = [VOICE] * MIN_VOICED_FRAMES + [SILENCE] * END_SILENCE_FRAMES
    probabilities = [1.0] * MIN_VOICED_FRAMES + [0.0] * END_SILENCE_FRAMES
    stt = CancellationResistantStt()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([FakeSource(frames)]),
        stt,
        vad_factory=lambda: FakeVad(probabilities),
    )
    await runtime.async_start()
    await stt.started.wait()

    stop_task = asyncio.create_task(runtime.async_stop())
    await settle()
    assert stt.cancelled == 1
    assert not stop_task.done()
    stt.release.set()
    await stop_task
    assert runtime.worker_count == 0
    assert runtime.in_flight_count == 0
    assert runtime.timer_count == 0


async def test_stop_defers_exact_caller_cancellation_until_source_cleanup() -> None:
    close_gate = asyncio.Event()
    source = QueuedSource(close_gate=close_gate)
    sleep = ControlledSleep()
    runtime, writes, pulse = await start_active_pulse(
        snapshot=lambda: {KEY_A: door()},
        routes=route,
        factory=SourceFactory([source]),
        source=source,
        stt=FakeStt([PHRASE]),
        sleep=sleep,
    )
    listener_failure = dirty_control_flow(
        asyncio.CancelledError("PRIVATE STOP LISTENER"), "STOP LISTENER"
    )
    later_writes: list[tuple[bool, bool]] = []

    def fail_stop_cleanup() -> None:
        raise listener_failure

    runtime.add_listener(fail_stop_cleanup)
    runtime.add_listener(
        lambda: later_writes.append(
            (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
        )
    )

    stop_task = asyncio.create_task(runtime.async_stop())
    await wait_until(lambda: source.closed == 1)
    stop_task.cancel("preserve-me")
    await settle()
    assert not stop_task.done()
    assert runtime.available_for(KEY_A) is False
    assert runtime.is_on_for(KEY_A) is False
    assert pulse.cancelled()
    assert writes[-1] == (False, False)
    assert later_writes == [(False, False)]
    assert_sanitized_listener_failure(listener_failure)
    notifications = len(writes)
    await settle()
    assert len(writes) == notifications

    close_gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await stop_task
    assert caught.value.args == ("preserve-me",)
    assert runtime.worker_count == 0
    assert runtime.in_flight_count == 0
    assert runtime.available_count == 0
    assert runtime.on_count == 0
    assert runtime.timer_count == 0


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize(
    "cleanup_cancel_timing", ["immediate", "active"], ids=["immediate", "active"]
)
async def test_owned_cleanup_task_cancellation_cannot_abandon_stop(
    monkeypatch: pytest.MonkeyPatch,
    task_factory_name: str,
    cleanup_cancel_timing: str,
) -> None:
    """Private cleanup cancellation is quarantined from every public stop caller."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    close_gate = asyncio.Event()
    source = QueuedSource(close_gate=close_gate)
    stt = PulseThenResistantStt()
    sleep = CancellationResistantPulseSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        vad_factory=lambda: FakeVad([1.0, 1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
        min_stt_interval_seconds=0,
    )
    private_cleanup_cancel = "PRIVATE OWNED CLEANUP TASK CANCELLATION"
    caller_cancel_message = "EXACT PUBLIC STOP CALLER CANCELLATION"
    original_raise_sanitized = voice_runtime._raise_sanitized
    raised_cancellations: list[asyncio.CancelledError] = []
    cleanup_tasks: list[asyncio.Task[None]] = []
    stop_task: asyncio.Task[None] | None = None
    concurrent_stop: asyncio.Task[None] | None = None

    def capture_sanitized_raise(error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            raised_cancellations.append(error)
        original_raise_sanitized(error)

    monkeypatch.setattr(voice_runtime, "_raise_sanitized", capture_sanitized_raise)
    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        source.push(VOICE)
        await stt.old_started.wait()
        worker = runtime._workers[KEY_A]
        pulse = worker.pulse_task
        assert pulse is not None

        if cleanup_cancel_timing == "immediate":
            original_shield = asyncio.shield

            def cancel_published_cleanup(awaitable: Any) -> asyncio.Future[Any]:
                if awaitable is runtime._stop_task and not cleanup_tasks:
                    assert isinstance(awaitable, asyncio.Task)
                    cleanup_tasks.append(awaitable)
                    awaitable.cancel(private_cleanup_cancel)
                return original_shield(awaitable)

            monkeypatch.setattr(asyncio, "shield", cancel_published_cleanup)

        stop_task = loop.create_task(runtime.async_stop())
        await wait_until(lambda: runtime._stop_task is not None)
        cleanup = runtime._stop_task
        assert cleanup is not None
        if cleanup_cancel_timing == "active":
            await wait_until(lambda: source.closed == 1)
            await stt.old_cancelled.wait()
            await sleep.cancelled.wait()
            cleanup_tasks.append(cleanup)
            cleanup.cancel(private_cleanup_cancel)

        await wait_until(lambda: source.closed == 1)
        await stt.old_cancelled.wait()
        await sleep.cancelled.wait()
        await settle()
        assert cleanup_tasks == [cleanup]
        assert stop_task.done() is False
        assert cleanup.done() is False

        concurrent_stop = loop.create_task(runtime.async_stop())
        await settle()
        assert runtime._stop_task is cleanup
        assert concurrent_stop.done() is False

        stop_task.cancel(caller_cancel_message)
        await settle()
        assert stop_task.done() is False
        assert concurrent_stop.done() is False
        assert cleanup.done() is False

        close_gate.set()
        stt.old_release.set()
        sleep.release_all()
        await concurrent_stop
        concurrent_stop = None
        with pytest.raises(asyncio.CancelledError) as caught:
            await stop_task
        stop_task = None

        caller_cancellations = [
            cancellation
            for cancellation in raised_cancellations
            if cancellation.args == (caller_cancel_message,)
        ]
        assert len(caller_cancellations) == 1
        assert caught.value is caller_cancellations[0]
        assert caught.value.args == (caller_cancel_message,)
        assert caught.value.args != (private_cleanup_cancel,)

        assert cleanup.done() is True
        assert cleanup.cancelled() is False
        assert cleanup.cancelling() == 0
        assert cleanup.exception() is None
        assert cleanup.result() is None
        assert cleanup.get_stack() == []
        assert getattr(cleanup.get_coro(), "cr_frame", None) is None
        assert getattr(cleanup, "_cancel_message", None) is None
        assert private_cleanup_cancel not in repr(cleanup)
        assert not any(
            referent is private_cleanup_cancel
            or (type(referent) is str and referent == private_cleanup_cancel)
            for referent in gc.get_referents(cleanup)
        )

        assert source.closed == 1
        assert stt.active == 0
        assert sleep.active == 0
        assert pulse.done() is True
        assert runtime._listeners == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_count == 0
        assert runtime.on_count == 0
        assert all(value is False for value in runtime._available.values())
        assert all(value is False for value in runtime._on.values())
    finally:
        close_gate.set()
        stt.old_release.set()
        stt.replacement_release.set()
        sleep.release_all()
        for pending_stop in (concurrent_stop, stop_task):
            if pending_stop is not None:
                try:
                    await pending_stop
                except asyncio.CancelledError:
                    pass
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_dependency_cleanup_cancellation_survives_terminal_active_stop(
    task_factory_name: str,
) -> None:
    """Dependency cancellation is public only after active cleanup is terminal."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    close_gate = asyncio.Event()
    source = QueuedSource(close_gate=close_gate)
    sleep = ControlledSleep()
    runtime: VoicePhraseManager | None = None
    stop_task: asyncio.Task[None] | None = None
    try:
        runtime, writes, pulse = await start_active_pulse(
            snapshot=lambda: {KEY_A: door()},
            routes=route,
            factory=SourceFactory([source]),
            source=source,
            stt=FakeStt([PHRASE]),
            sleep=sleep,
        )
        worker = runtime._workers[KEY_A]
        original_cleanup = runtime._cleanup
        second_attempt = asyncio.Event()
        failure = dirty_control_flow(
            asyncio.CancelledError("DEPENDENCY CLEANUP CONTROL FLOW"),
            "DEPENDENCY CLEANUP",
        )
        cleanup_calls = 0

        async def cleanup_with_dependency_cancellation() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                raise failure
            second_attempt.set()
            await original_cleanup()

        runtime._cleanup = cleanup_with_dependency_cancellation  # type: ignore[method-assign]
        stop_task = loop.create_task(runtime.async_stop())
        await second_attempt.wait()
        cleanup = runtime._stop_task
        assert cleanup is not None
        await wait_until(lambda: source.closed == 1)
        await settle()

        assert cleanup_calls == 2
        assert stop_task.done() is False
        assert cleanup.done() is False
        assert runtime.available_for(KEY_A) is False
        assert runtime.is_on_for(KEY_A) is False
        assert writes[-1] == (False, False)

        close_gate.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await stop_task
        stop_task = None

        assert caught.value is failure
        assert caught.value.args == ("DEPENDENCY CLEANUP CONTROL FLOW",)
        assert physical_exception_slot(caught.value, "__context__") is None
        assert physical_exception_slot(caught.value, "__cause__") is None
        runtime_tracebacks = [
            traceback
            for traceback in physical_traceback_chain(caught.value)
            if traceback.tb_frame.f_globals.get("__name__") == voice_runtime.__name__
        ]
        assert "_cleanup" not in {
            traceback.tb_frame.f_code.co_name for traceback in runtime_tracebacks
        }
        assert "_cleanup_owned_task" not in {
            traceback.tb_frame.f_code.co_name for traceback in runtime_tracebacks
        }
        protected_ids = {
            id(runtime),
            id(runtime.config),
            id(worker),
            id(source),
            id(sleep),
            id(pulse),
        }
        for traceback in runtime_tracebacks:
            assert not _contains_protected_referent(
                traceback.tb_frame.f_locals, protected_ids, set()
            )

        assert cleanup.done() is True
        assert cleanup.cancelled() is False
        assert cleanup.cancelling() == 0
        assert cleanup.exception() is None
        assert cleanup.result() is None
        assert cleanup.get_stack() == []
        assert getattr(cleanup.get_coro(), "cr_frame", None) is None
        assert failure not in gc.get_referents(cleanup)
        assert "DEPENDENCY CLEANUP CONTROL FLOW" not in repr(cleanup)
        assert source.closed == 1
        assert pulse.done() is True
        assert runtime._listeners == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_count == 0
        assert runtime.on_count == 0
    finally:
        close_gate.set()
        if stop_task is not None:
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
        if runtime is not None:
            await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize("direct_order", ["before", "after"])
async def test_direct_and_dependency_cleanup_cancellations_keep_first_dependency(
    task_factory_name: str, direct_order: str
) -> None:
    """Direct Task cancellation is swallowed around multiple dependency outcomes."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    original_cleanup = runtime._cleanup
    direct_attempt_started = asyncio.Event()
    never_release = asyncio.Event()
    first_failure = dirty_control_flow(
        asyncio.CancelledError("FIRST DEPENDENCY CLEANUP CONTROL FLOW"),
        "FIRST DEPENDENCY CLEANUP",
    )
    later_failure = dirty_control_flow(
        asyncio.CancelledError("LATER DEPENDENCY CLEANUP CONTROL FLOW"),
        "LATER DEPENDENCY CLEANUP",
    )
    direct_attempt = 1 if direct_order == "before" else 2
    first_failure_attempt = 2 if direct_order == "before" else 1
    cleanup_calls = 0
    stop_task: asyncio.Task[None] | None = None
    try:

        async def cleanup_with_mixed_cancellations() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == direct_attempt:
                direct_attempt_started.set()
                await never_release.wait()
            if cleanup_calls == first_failure_attempt:
                raise first_failure
            if cleanup_calls == 3:
                raise later_failure
            await original_cleanup()

        runtime._cleanup = cleanup_with_mixed_cancellations  # type: ignore[method-assign]
        stop_task = loop.create_task(runtime.async_stop())
        await direct_attempt_started.wait()
        cleanup = runtime._stop_task
        assert cleanup is not None
        assert cleanup.cancel("PRIVATE OWNED CLEANUP") is True

        with pytest.raises(asyncio.CancelledError) as caught:
            await stop_task
        stop_task = None

        assert caught.value is first_failure
        assert caught.value.args == ("FIRST DEPENDENCY CLEANUP CONTROL FLOW",)
        assert cleanup_calls == 4
        assert_sanitized_listener_failure(later_failure)
        assert cleanup.done() is True
        assert cleanup.cancelled() is False
        assert cleanup.cancelling() == 0
        assert cleanup.exception() is None
        assert cleanup.result() is None
        assert cleanup.get_stack() == []
        assert getattr(cleanup.get_coro(), "cr_frame", None) is None
        assert getattr(cleanup, "_cancel_message", None) is None
        assert "PRIVATE OWNED CLEANUP" not in repr(cleanup)
        assert not any(
            type(referent) is str and referent == "PRIVATE OWNED CLEANUP"
            for referent in gc.get_referents(cleanup)
        )
        assert first_failure not in gc.get_referents(cleanup)
        assert later_failure not in gc.get_referents(cleanup)
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
    finally:
        never_release.set()
        if stop_task is not None:
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


async def test_caller_cancellation_precedes_dependency_and_direct_cleanup_cancel() -> (
    None
):
    """A later public caller cancellation wins after mixed cleanup cancellation."""

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    original_cleanup = runtime._cleanup
    direct_attempt_started = asyncio.Event()
    terminal_attempt_started = asyncio.Event()
    terminal_release = asyncio.Event()
    dependency = dirty_control_flow(
        asyncio.CancelledError("DEPENDENCY CLEANUP CONTROL FLOW"),
        "CALLER PRECEDENCE DEPENDENCY",
    )
    cleanup_calls = 0
    raised_cancellations: list[asyncio.CancelledError] = []
    original_raise_sanitized = voice_runtime._raise_sanitized

    def capture_sanitized_raise(error: BaseException) -> None:
        if type(error) is asyncio.CancelledError:
            raised_cancellations.append(error)
        original_raise_sanitized(error)

    async def cleanup_with_mixed_cancellations() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise dependency
        if cleanup_calls == 2:
            direct_attempt_started.set()
            await terminal_release.wait()
        terminal_attempt_started.set()
        await terminal_release.wait()
        await original_cleanup()

    runtime._cleanup = cleanup_with_mixed_cancellations  # type: ignore[method-assign]
    stop_task: asyncio.Task[None] | None = None
    try:
        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(voice_runtime, "_raise_sanitized", capture_sanitized_raise)
            stop_task = asyncio.create_task(runtime.async_stop())
            await direct_attempt_started.wait()
            cleanup = runtime._stop_task
            assert cleanup is not None
            assert cleanup.cancel("PRIVATE OWNED CLEANUP") is True
            await terminal_attempt_started.wait()

            stop_task.cancel("EXACT CALLER CLEANUP CANCELLATION")
            await settle()
            assert stop_task.done() is False
            assert cleanup.done() is False

            terminal_release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await stop_task
            stop_task = None

        caller_outcomes = [
            error
            for error in raised_cancellations
            if error.args == ("EXACT CALLER CLEANUP CANCELLATION",)
        ]
        assert len(caller_outcomes) == 1
        assert caught.value is caller_outcomes[0]
        assert caught.value is not dependency
        assert caught.value.args == ("EXACT CALLER CLEANUP CANCELLATION",)
        assert cleanup_calls == 3
        assert_sanitized_listener_failure(dependency)
        assert cleanup.done() is True
        assert cleanup.cancelled() is False
        assert cleanup.cancelling() == 0
        assert cleanup.exception() is None
        assert cleanup.result() is None
        assert cleanup.get_stack() == []
        assert getattr(cleanup.get_coro(), "cr_frame", None) is None
        assert getattr(cleanup, "_cancel_message", None) is None
        assert dependency not in gc.get_referents(cleanup)
        assert "PRIVATE OWNED CLEANUP" not in repr(cleanup)
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
    finally:
        terminal_release.set()
        if stop_task is not None:
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
        await runtime.async_stop()


async def test_cleanup_control_flow_is_external_to_its_successful_owned_task() -> None:
    """A public cleanup outcome cannot remain in the shared owned Task."""

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    original_cleanup = runtime._cleanup
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    failure = SttPipelineControlFlow("PRIVATE CLEANUP CONTROL FLOW")
    cleanup_calls = 0

    async def cleanup_with_one_control_flow() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            cleanup_started.set()
            await cleanup_release.wait()
            await original_cleanup()
            raise failure
        await original_cleanup()

    runtime._cleanup = cleanup_with_one_control_flow  # type: ignore[method-assign]
    stop_task = asyncio.create_task(runtime.async_stop())
    await cleanup_started.wait()
    cleanup = runtime._stop_task
    assert cleanup is not None
    cleanup_release.set()

    with pytest.raises(SttPipelineControlFlow) as caught:
        await stop_task

    assert caught.value is failure
    assert cleanup_calls == 2
    assert cleanup.done() is True
    assert cleanup.cancelled() is False
    assert cleanup.cancelling() == 0
    assert cleanup.exception() is None
    assert cleanup.result() is None
    assert cleanup.get_stack() == []
    assert getattr(cleanup.get_coro(), "cr_frame", None) is None
    assert failure not in gc.get_referents(cleanup)
    assert "PRIVATE CLEANUP CONTROL FLOW" not in repr(cleanup)
    assert runtime._stop_task is None
    assert runtime._stop_outcome is None


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
async def test_stop_inline_fallback_caller_cancellation_precedes_dependency(
    monkeypatch: pytest.MonkeyPatch, task_factory_name: str
) -> None:
    """A later caller cancellation wins over earlier inline dependency control flow."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    close_gate = asyncio.Event()
    source = QueuedSource(close_gate=close_gate)
    stt = PulseThenResistantStt()
    sleep = CancellationResistantPulseSleep()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        vad_factory=lambda: FakeVad([1.0, 1.0]),
        segmenter_factory=EveryFrameSegmenter,
        sleep=sleep,
        min_stt_interval_seconds=0,
    )
    original_cleanup = runtime._cleanup
    dependency = dirty_control_flow(
        asyncio.CancelledError("EXACT INLINE DEPENDENCY CANCELLATION"),
        "INLINE DEPENDENCY CANCELLATION",
    )
    second_attempt_started = asyncio.Event()
    cleanup_calls = 0

    async def cleanup_with_dependency_then_caller() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise dependency
        second_attempt_started.set()
        await original_cleanup()

    runtime._cleanup = cleanup_with_dependency_then_caller  # type: ignore[method-assign]
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    original_constructor = voice_runtime._OWNED_TASK_CONSTRUCTOR
    original_raise_sanitized = voice_runtime._raise_sanitized
    cleanup_create_failures = 0
    raised_cancellations: list[asyncio.CancelledError] = []
    stop_task: asyncio.Task[None] | None = None
    second_stop: asyncio.Task[None] | None = None

    def fail_first_cleanup_task(
        coroutine: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        eager_start: bool,
    ) -> asyncio.Task[Any]:
        nonlocal cleanup_create_failures
        if (
            getattr(coroutine, "cr_code", None)
            is voice_runtime._cleanup_owned_task.__code__
            and cleanup_create_failures == 0
        ):
            cleanup_create_failures += 1
            raise RuntimeError("PRIVATE CLEANUP TASK CREATION FAILURE")
        return original_constructor(coroutine, loop=loop, eager_start=eager_start)

    def capture_sanitized_raise(error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            raised_cancellations.append(error)
        original_raise_sanitized(error)

    try:
        await runtime.async_start()
        await source.read_started.wait()
        source.push(VOICE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        source.push(VOICE)
        await stt.old_started.wait()
        worker = runtime._workers[KEY_A]
        pulse = worker.pulse_task
        assert pulse is not None

        with monkeypatch.context() as scoped:
            scoped.setattr(
                voice_runtime, "_OWNED_TASK_CONSTRUCTOR", fail_first_cleanup_task
            )
            scoped.setattr(voice_runtime, "_raise_sanitized", capture_sanitized_raise)
            stop_task = loop.create_task(runtime.async_stop())
            await second_attempt_started.wait()
            await wait_until(lambda: source.closed >= 1)
            await sleep.cancelled.wait()
            private_cancel = "PRIVATE INLINE STOP CANCELLATION"
            stop_task.cancel(private_cancel)
            await settle()

            assert stop_task.done() is False
            stop_task.cancel("LATER PRIVATE INLINE STOP CANCELLATION")
            await settle()
            assert stop_task.done() is False
            assert cleanup_create_failures == 1
            assert runtime.available_for(KEY_A) is False
            assert runtime.is_on_for(KEY_A) is False

            close_gate.set()
            stt.old_release.set()
            sleep.release_all()
            completed_stop = stop_task
            with pytest.raises(asyncio.CancelledError) as caught:
                await stop_task
            stop_task = None
            assert cleanup_calls == 3

            second_stop = loop.create_task(runtime.async_stop())
            await wait_until(lambda: runtime._stop_task is not None)
            await second_stop
            second_stop = None
            assert cleanup_calls == 4

        cancellations = [
            cancellation
            for cancellation in raised_cancellations
            if cancellation.args == (private_cancel,)
        ]
        assert len(cancellations) == 2
        cancellation = cancellations[0]
        assert cancellations[1] is cancellation
        assert caught.value is cancellation
        assert caught.value is not dependency
        assert caught.value.args == (private_cancel,)
        assert completed_stop.cancelling() == 0
        assert cleanup_calls == 4
        assert_sanitized_listener_failure(dependency)
        assert physical_exception_slot(caught.value, "__context__") is None
        assert physical_exception_slot(caught.value, "__cause__") is None
        runtime_tracebacks = [
            traceback
            for traceback in physical_traceback_chain(caught.value)
            if traceback.tb_frame.f_globals.get("__name__") == voice_runtime.__name__
        ]
        assert "_cleanup" not in {
            traceback.tb_frame.f_code.co_name for traceback in runtime_tracebacks
        }
        protected_ids = {
            id(runtime),
            id(runtime.config),
            id(worker),
            id(source),
            id(stt),
            id(sleep),
            id(pulse),
        }
        for traceback in runtime_tracebacks:
            assert not _contains_protected_referent(
                traceback.tb_frame.f_locals, protected_ids, set()
            )
        assert source.close_gate is close_gate
        assert source.closed == 1
        assert stt.active == 0
        assert sleep.active == 0
        assert pulse.done() is True
        assert runtime._listeners == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_count == 0
        assert runtime.on_count == 0
        assert all(value is False for value in runtime._available.values())
        assert all(value is False for value in runtime._on.values())
        assert writes[-1] == (False, False)
    finally:
        close_gate.set()
        stt.old_release.set()
        stt.replacement_release.set()
        sleep.release_all()
        for pending_stop in (second_stop, stop_task):
            if pending_stop is not None:
                try:
                    await pending_stop
                except asyncio.CancelledError:
                    pass
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize(
    ("scenario", "expected_role"),
    [
        ("caller-then-dependency", "caller"),
        ("multiple-callers-and-dependencies", "caller"),
        ("dependency-only", "cleanup"),
        ("non-cancel-then-caller", "caller"),
        ("dependency-then-non-cancel", "cleanup"),
        ("non-cancel-then-dependency", "cleanup"),
    ],
)
async def test_stop_inline_fallback_separates_caller_and_cleanup_control_flow(
    monkeypatch: pytest.MonkeyPatch,
    task_factory_name: str,
    scenario: str,
    expected_role: str,
) -> None:
    """Inline retry keeps exact first errors in separate precedence classes."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    runtime._available[KEY_A] = True
    runtime._on[KEY_A] = True
    writes: list[tuple[bool, bool]] = []
    runtime.add_listener(
        lambda: writes.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    original_cleanup = runtime._cleanup
    first_dependency = dirty_control_flow(
        asyncio.CancelledError("FIRST INLINE DEPENDENCY"),
        "FIRST INLINE DEPENDENCY",
    )
    later_dependency = dirty_control_flow(
        asyncio.CancelledError("LATER INLINE DEPENDENCY"),
        "LATER INLINE DEPENDENCY",
    )
    ordinary_failure = dirty_control_flow(
        SttPipelineControlFlow("FIRST INLINE NON-CANCEL CONTROL FLOW"),
        "INLINE NON-CANCEL CONTROL FLOW",
    )
    later_ordinary_failure = dirty_control_flow(
        SttPipelineControlFlow("LATER INLINE NON-CANCEL CONTROL FLOW"),
        "LATER INLINE NON-CANCEL CONTROL FLOW",
    )
    actions_by_scenario: dict[str, list[object]] = {
        "caller-then-dependency": ["caller-0", first_dependency, "success"],
        "multiple-callers-and-dependencies": [
            "caller-0",
            first_dependency,
            "caller-1",
            later_dependency,
            "success",
        ],
        "dependency-only": [first_dependency, later_dependency, "success"],
        "non-cancel-then-caller": [ordinary_failure, "caller-0", "success"],
        "dependency-then-non-cancel": [
            first_dependency,
            later_ordinary_failure,
            "success",
        ],
        "non-cancel-then-dependency": [
            ordinary_failure,
            later_dependency,
            "success",
        ],
    }
    actions = actions_by_scenario[scenario]
    caller_messages = [
        "EXACT FIRST INLINE CALLER CANCELLATION",
        "LATER INLINE CALLER CANCELLATION",
    ]
    caller_started = [asyncio.Event() for action in actions if action == "caller-0"]
    if "caller-1" in actions:
        caller_started.append(asyncio.Event())
    never_release = [asyncio.Event() for _ in caller_started]
    seen_caller_errors: list[asyncio.CancelledError] = []
    cleanup_calls = 0
    loop_turn = 0
    stop_ticker = False
    attempt_turns: list[int] = []

    async def count_loop_turns() -> None:
        nonlocal loop_turn
        while not stop_ticker:
            loop_turn += 1
            await asyncio.sleep(0)

    async def cleanup_with_scripted_control_flow() -> None:
        nonlocal cleanup_calls
        action = actions[cleanup_calls]
        cleanup_calls += 1
        attempt_turns.append(loop_turn)
        if isinstance(action, BaseException):
            raise action
        if action == "success":
            await original_cleanup()
            return
        caller_index = int(str(action).rsplit("-", maxsplit=1)[1])
        caller_started[caller_index].set()
        try:
            await never_release[caller_index].wait()
        except asyncio.CancelledError as error:
            seen_caller_errors.append(error)
            raise

    def reject_cleanup_task(coroutine: Any) -> None:
        assert (
            getattr(coroutine, "cr_code", None)
            is voice_runtime._cleanup_owned_task.__code__
        )
        coroutine.close()

    runtime._cleanup = cleanup_with_scripted_control_flow  # type: ignore[method-assign]
    stop_task: asyncio.Task[None] | None = None
    ticker_task: asyncio.Task[None] | None = None
    try:
        monkeypatch.setattr(voice_runtime, "_create_owned_task", reject_cleanup_task)
        ticker_task = loop.create_task(count_loop_turns())
        stop_task = loop.create_task(runtime.async_stop())
        for caller_index, started in enumerate(caller_started):
            await asyncio.wait_for(started.wait(), timeout=1.0)
            assert stop_task.cancel(caller_messages[caller_index]) is True
            await wait_until(
                lambda caller_index=caller_index: len(seen_caller_errors) > caller_index
            )

        completed_stop = stop_task
        if expected_role == "caller":
            with pytest.raises(asyncio.CancelledError) as caught:
                await stop_task
            expected = seen_caller_errors[0]
            assert caught.value is expected
            assert caught.value.args == (caller_messages[0],)
        else:
            expected = (
                first_dependency if actions[0] is first_dependency else ordinary_failure
            )
            with pytest.raises(type(expected)) as caught:
                await stop_task
            assert caught.value is expected
        stop_task = None

        assert completed_stop.cancelling() == 0
        assert cleanup_calls == len(actions)
        assert all(
            later_turn > earlier_turn
            for earlier_turn, later_turn in pairwise(attempt_turns)
        )
        if len(seen_caller_errors) > 1:
            assert seen_caller_errors[1].args == ()
            assert_sanitized_listener_failure(seen_caller_errors[1])
        for action in actions:
            if isinstance(action, BaseException) and action is not expected:
                assert_sanitized_listener_failure(action)
        assert physical_exception_slot(caught.value, "__context__") is None
        assert physical_exception_slot(caught.value, "__cause__") is None
        protected_ids = {id(runtime), id(runtime.config)}
        protected_values: set[str | bytes] = {
            TOKEN,
            MODEL,
            ENDPOINT,
            KEY_A,
            BINDING_A,
        }
        for traceback in physical_traceback_chain(caught.value):
            if traceback.tb_frame.f_globals.get("__name__") == voice_runtime.__name__:
                assert traceback.tb_frame.f_code.co_name != "_cleanup"
                assert not _contains_protected_referent(
                    traceback.tb_frame.f_locals,
                    protected_ids,
                    protected_values,
                )
        assert runtime._listeners == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime._stop_task is None
        assert runtime._stop_outcome is None
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_count == 0
        assert runtime.on_count == 0
        assert all(value is False for value in runtime._available.values())
        assert all(value is False for value in runtime._on.values())
        assert writes == [(False, False)]
    finally:
        stop_ticker = True
        for release in never_release:
            release.set()
        if stop_task is not None:
            try:
                await stop_task
            except BaseException as error:  # noqa: BLE001 - test cleanup
                voice_runtime._sanitize_exception_chain(error)
        if ticker_task is not None:
            await ticker_task
        runtime._cleanup = original_cleanup  # type: ignore[method-assign]
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize(
    ("scenario", "expected_index"),
    [
        ("ordinary-then-dependency", 0),
        ("dependency-only", 0),
        ("success", None),
    ],
)
async def test_stop_inline_fallback_preserves_entry_cancellation_baseline(
    monkeypatch: pytest.MonkeyPatch,
    task_factory_name: str,
    scenario: str,
    expected_index: int | None,
) -> None:
    """Residual cancellation cannot reclassify dependency cleanup control flow."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    runtime._available[KEY_A] = True
    runtime._on[KEY_A] = True
    original_cleanup = runtime._cleanup
    ordinary_failure = dirty_control_flow(
        SttPipelineControlFlow("FIRST ORDINARY INLINE FLOW"),
        "FIRST ORDINARY INLINE FLOW",
    )
    first_dependency = dirty_control_flow(
        asyncio.CancelledError("FIRST BASELINE DEPENDENCY"),
        "FIRST BASELINE DEPENDENCY",
    )
    later_dependency = dirty_control_flow(
        asyncio.CancelledError("LATER BASELINE DEPENDENCY"),
        "LATER BASELINE DEPENDENCY",
    )
    actions_by_scenario: dict[str, list[BaseException]] = {
        "ordinary-then-dependency": [ordinary_failure, later_dependency],
        "dependency-only": [first_dependency, later_dependency],
        "success": [],
    }
    actions = actions_by_scenario[scenario]
    cleanup_calls = 0

    async def cleanup_with_scripted_failures() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls <= len(actions):
            raise actions[cleanup_calls - 1]
        await original_cleanup()

    def reject_cleanup_task(coroutine: Any) -> None:
        assert (
            getattr(coroutine, "cr_code", None)
            is voice_runtime._cleanup_owned_task.__code__
        )
        coroutine.close()

    async def stop_with_residual_cancellation() -> tuple[BaseException | None, int]:
        current = asyncio.current_task()
        assert current is not None
        assert current.cancel("PREEXISTING ENTRY CANCELLATION") is True
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as residual:
            voice_runtime._sanitize_exception_chain(residual)
        assert current.cancelling() == 1
        try:
            await runtime.async_stop()
        except BaseException as error:  # noqa: BLE001 - inspect exact public outcome
            return error, current.cancelling()
        return None, current.cancelling()

    runtime._cleanup = cleanup_with_scripted_failures  # type: ignore[method-assign]
    residual_task: asyncio.Task[tuple[BaseException | None, int]] | None = None
    try:
        monkeypatch.setattr(voice_runtime, "_create_owned_task", reject_cleanup_task)
        residual_task = loop.create_task(stop_with_residual_cancellation())
        outcome, residual_count = await residual_task

        expected = actions[expected_index] if expected_index is not None else None
        assert outcome is expected
        assert residual_count == 1
        assert residual_task.cancelling() == 1
        assert cleanup_calls == len(actions) + 1
        for action in actions:
            if action is not expected:
                assert_sanitized_listener_failure(action)
        assert runtime._listeners == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_count == 0
        assert runtime.on_count == 0
    finally:
        runtime._cleanup = original_cleanup  # type: ignore[method-assign]
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("task_factory_name", ["default", "eager"])
@pytest.mark.parametrize("burst_site", ["pacing", "cleanup"])
async def test_stop_inline_fallback_preserves_first_burst_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    task_factory_name: str,
    burst_site: str,
) -> None:
    """A real fallback checkpoint retains the first same-turn cancel message."""

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    if task_factory_name == "eager":
        eager_factory = getattr(asyncio, "eager_task_factory", None)
        if eager_factory is None:
            pytest.skip("standard eager task factory unavailable")
        loop.set_task_factory(eager_factory)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(),
        FakeStt(),
    )
    runtime._available[KEY_A] = True
    runtime._on[KEY_A] = True
    original_cleanup = runtime._cleanup
    first_failure = dirty_control_flow(
        SttPipelineControlFlow("FIRST INLINE CLEANUP FLOW"),
        "FIRST INLINE CLEANUP FLOW",
    )
    later_dependency = dirty_control_flow(
        asyncio.CancelledError("LATER INLINE CLEANUP DEPENDENCY"),
        "LATER INLINE CLEANUP DEPENDENCY",
    )
    cleanup_started = asyncio.Event()
    never_release = asyncio.Event()
    cleanup_calls = 0
    stop_task: asyncio.Task[None] | None = None
    raised_cancellations: list[asyncio.CancelledError] = []
    scrubbed_cancellations: list[asyncio.CancelledError] = []
    callback_errors: list[dict[str, Any]] = []
    original_raise_sanitized = voice_runtime._raise_sanitized
    original_sanitize_owned = voice_runtime._sanitize_owned_cancellation

    def cancel_burst(first: str, second: str) -> None:
        assert stop_task is not None
        assert stop_task.cancel(first) is True
        assert stop_task.cancel(second) is True

    async def cleanup_with_bursts() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            if burst_site == "pacing":
                loop.call_soon(cancel_burst, "FIRST PACING", "SECOND PACING")
                raise first_failure
            cleanup_started.set()
            await never_release.wait()
        if cleanup_calls == 2:
            loop.call_soon(cancel_burst, "LATER FIRST", "LATER SECOND")
            raise later_dependency
        await original_cleanup()

    def reject_cleanup_task(coroutine: Any) -> None:
        assert (
            getattr(coroutine, "cr_code", None)
            is voice_runtime._cleanup_owned_task.__code__
        )
        coroutine.close()

    def capture_sanitized_raise(error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            raised_cancellations.append(error)
        original_raise_sanitized(error)

    def capture_scrubbed_cancellation(
        error: asyncio.CancelledError,
        current: asyncio.Task[object],
        cancellation_count: int,
    ) -> None:
        scrubbed_cancellations.append(error)
        original_sanitize_owned(error, current, cancellation_count)

    runtime._cleanup = cleanup_with_bursts  # type: ignore[method-assign]
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))
    try:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            monkeypatch.setattr(
                voice_runtime, "_create_owned_task", reject_cleanup_task
            )
            monkeypatch.setattr(
                voice_runtime, "_raise_sanitized", capture_sanitized_raise
            )
            monkeypatch.setattr(
                voice_runtime,
                "_sanitize_owned_cancellation",
                capture_scrubbed_cancellation,
            )
            stop_task = loop.create_task(runtime.async_stop())
            if burst_site == "cleanup":
                await cleanup_started.wait()
                cancel_burst("FIRST CLEANUP", "SECOND CLEANUP")

            completed_stop = stop_task
            with pytest.raises(asyncio.CancelledError) as caught:
                await stop_task
            stop_task = None
            await settle()

        first_message = "FIRST PACING" if burst_site == "pacing" else "FIRST CLEANUP"
        assert raised_cancellations == [caught.value]
        assert caught.value.args == (first_message,)
        assert completed_stop.cancelling() == 0
        protected_ids = {id(runtime), id(runtime.config), id(completed_stop)}
        runtime_tracebacks = [
            traceback
            for traceback in physical_traceback_chain(caught.value)
            if traceback.tb_frame.f_globals.get("__name__") == voice_runtime.__name__
        ]
        assert "_cleanup" not in {
            traceback.tb_frame.f_code.co_name for traceback in runtime_tracebacks
        }
        assert "_inline_stop_checkpoint" not in {
            traceback.tb_frame.f_code.co_name for traceback in runtime_tracebacks
        }
        for traceback in runtime_tracebacks:
            assert not _contains_protected_referent(
                traceback.tb_frame.f_locals, protected_ids, set()
            )
        assert len(scrubbed_cancellations) == 1
        assert scrubbed_cancellations[0].args == ()
        assert_sanitized_listener_failure(scrubbed_cancellations[0])
        assert cleanup_calls == 3
        if burst_site == "pacing":
            assert_sanitized_listener_failure(first_failure)
        assert_sanitized_listener_failure(later_dependency)
        assert callback_errors == []
        assert caught_warnings == []
        assert runtime._listeners == []
        assert runtime._workers == {}
        assert runtime._retiring_by_key == {}
        assert runtime._all_worker_tasks == set()
        assert runtime._all_stt_tasks == set()
        assert runtime._all_timer_tasks == set()
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
        assert runtime.available_count == 0
        assert runtime.on_count == 0
    finally:
        never_release.set()
        if stop_task is not None:
            try:
                await stop_task
            except BaseException as error:  # noqa: BLE001 - test cleanup
                voice_runtime._sanitize_exception_chain(error)
        runtime._cleanup = original_cleanup  # type: ignore[method-assign]
        loop.set_exception_handler(previous_handler)
        await runtime.async_stop()
        loop.set_task_factory(previous_factory)


async def test_async_start_cancellation_is_not_a_listener_cancellation() -> None:
    close_gate = asyncio.Event()
    source = QueuedSource(close_gate=close_gate)
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        FakeStt(),
    )
    stop_task: asyncio.Task[None] | None = None
    try:
        await runtime.async_start()
        await source.read_started.wait()
        runtime._set_available(KEY_A, True)
        listener_failure = dirty_control_flow(
            asyncio.CancelledError("PRIVATE START LISTENER"), "START LISTENER"
        )
        later_writes: list[tuple[bool, bool]] = []

        def fail_stop_notification() -> None:
            raise listener_failure

        runtime.add_listener(fail_stop_notification)
        runtime.add_listener(
            lambda: later_writes.append(
                (runtime.available_for(KEY_A), runtime.is_on_for(KEY_A))
            )
        )

        stop_task = asyncio.create_task(runtime.async_stop())
        await wait_until(lambda: source.closed == 1)
        assert later_writes == [(False, False)]
        assert_sanitized_listener_failure(listener_failure)

        start_task = asyncio.create_task(runtime.async_start())
        await settle()
        assert start_task.done() is False
        start_task.cancel("exact-start-cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await start_task
        assert caught.value is not listener_failure
        assert caught.value.args == ("exact-start-cancellation",)
        assert stop_task.done() is False

        close_gate.set()
        await stop_task
        stop_task = None
        assert runtime.worker_count == 0
        assert runtime.in_flight_count == 0
        assert runtime.timer_count == 0
    finally:
        close_gate.set()
        if stop_task is not None:
            await stop_task
        await runtime.async_stop()


def test_repr_errors_state_and_surface_never_expose_content_or_physical_action() -> (
    None
):
    config = parse_enabled()
    runtime = manager_for(config, dict, route, SourceFactory(), FakeStt())
    rendered = repr(runtime) + repr(config) + repr(config.targets)
    for secret in (
        PHRASE,
        PHRASE.casefold(),
        TOKEN,
        MODEL,
        ENDPOINT,
        KEY_A,
        BINDING_A,
        "AAECAwQFBgcICQoLDA0ODw==",
    ):
        assert secret not in rendered
    assert repr(runtime) == (
        "VoicePhraseManager(configured=1, workers=0, available=0, on=0, in_flight=0)"
    )
    for forbidden in (
        "open",
        "async_open",
        "open_door",
        "mqtt",
        "frigate",
        "button",
        "transcript",
    ):
        assert not hasattr(runtime, forbidden)

    source = Path(voice_runtime.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "homeassistant" not in roots
    lowered = source.casefold()
    assert "mqtt" not in lowered
    assert "frigate" not in lowered


class FreshnessClock:
    """Monotonic test clock for ordinary pipeline delays and start intervals."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("queue_delay", "stt_delay", "matcher_delay", "expected_calls", "expected_on"),
    [
        (2.0, 3.0, 4.0, (1, 1), True),
        (2.0, 3.0, 5.0, (1, 1), False),
        (2.0, 8.0, 0.0, (1, 0), False),
        (10.0, 0.0, 0.0, (0, 0), False),
    ],
)
async def test_freshness_default_deadline_covers_queue_stt_and_matcher(
    queue_delay: float,
    stt_delay: float,
    matcher_delay: float,
    expected_calls: tuple[int, int],
    expected_on: bool,
) -> None:
    """The same ten-second budget starts at completion, not STT admission."""

    clock = FreshnessClock()
    calls = [0, 0]
    source = QueuedSource()

    class DelayedStt:
        async def transcribe_pcm(self, _pcm: bytes) -> str:
            calls[0] += 1
            clock.now += stt_delay
            return PHRASE

    async def executor(function: Callable[[object], object], value: object) -> object:
        calls[1] += 1
        clock.now += matcher_delay
        return function(value)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        DelayedStt(),
        async_executor=executor,
        segmenter_factory=EveryFrameSegmenter,
        monotonic=clock,
    )
    limiter = voice_runtime._loop_limiter()
    await limiter.acquire()
    await limiter.acquire()
    try:
        await runtime.async_start()
        source.push(VOICE)
        await wait_until(lambda: runtime.in_flight_count == 1)
        await settle()
        clock.now += queue_delay
    finally:
        limiter.release()
        limiter.release()
    try:
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert tuple(calls) == expected_calls
        assert runtime.is_on_for(KEY_A) is expected_on
    finally:
        await runtime.async_stop()


async def test_freshness_queued_clip_expires_while_unrelated_stt_stays_busy() -> None:
    """Waiting PCM is released without waiting for either occupied global permit."""

    gate = GateStt(PHRASE)
    blockers = [
        manager_for(
            parse_enabled(),
            lambda: {KEY_A: door()},
            route,
            SourceFactory(queued_voice_sources(1)),
            gate,
            segmenter_factory=EveryFrameSegmenter,
        )
        for _ in range(2)
    ]
    source = QueuedSource()
    stt = FakeStt([PHRASE])
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        segmenter_factory=EveryFrameSegmenter,
        utterance_freshness_seconds=0.05,
    )
    try:
        await asyncio.gather(*(item.async_start() for item in blockers))
        await wait_until(lambda: gate.active == 2)
        await runtime.async_start()
        source.push(VOICE)
        await wait_until(lambda: runtime.in_flight_count == 1)
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert gate.active == 2
        assert stt.pcm == []
        assert runtime.on_count == 0
        assert runtime.available_for(KEY_A) is True
        gate.release.set()
        await wait_until(lambda: all(item.in_flight_count == 0 for item in blockers))
        assert stt.pcm == []
        # Expired waiting work consumes no rate slot; a new complete clip can run.
        source.push(SILENCE)
        await wait_until(lambda: runtime.is_on_for(KEY_A))
        assert stt.pcm == [SILENCE]
    finally:
        gate.release.set()
        await asyncio.gather(
            runtime.async_stop(), *(item.async_stop() for item in blockers)
        )


async def test_freshness_actual_stt_timeout_drops_before_matching() -> None:
    gate = GateStt(PHRASE)
    executor = GatedExecutor()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory(queued_voice_sources(1)),
        gate,
        segmenter_factory=EveryFrameSegmenter,
        async_executor=executor,
        utterance_freshness_seconds=0.05,
    )
    try:
        await runtime.async_start()
        await gate.started.wait()
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert gate.active == 0
        assert executor.calls == 0
        assert runtime.on_count == runtime.timer_count == 0
    finally:
        gate.release.set()
        executor.release.set()
        await runtime.async_stop()


@pytest.mark.parametrize("transition", ["none", "remove", "stop"])
async def test_freshness_delayed_matcher_keeps_ownership_but_never_pulses_late(
    transition: str,
) -> None:
    snapshot = {KEY_A: door()}
    started = asyncio.Event()
    release = asyncio.Event()

    async def executor(function: Callable[[object], object], value: object) -> object:
        started.set()
        await release.wait()
        return function(value)

    runtime = manager_for(
        parse_enabled(),
        snapshot.copy,
        route,
        SourceFactory(queued_voice_sources(1)),
        FakeStt([PHRASE]),
        segmenter_factory=EveryFrameSegmenter,
        async_executor=executor,
        utterance_freshness_seconds=0.05,
    )
    pulses: list[bool] = []
    runtime.add_listener(lambda: pulses.append(runtime.is_on_for(KEY_A)))
    stopping = None
    try:
        await runtime.async_start()
        await started.wait()
        # Let the real deadline expire while the ordinary matcher is still busy.
        await asyncio.sleep(0.08)
        assert runtime.in_flight_count == 1
        if transition == "remove":
            snapshot.clear()
            runtime.reconcile()
        elif transition == "stop":
            stopping = asyncio.create_task(runtime.async_stop())
        await settle()
        assert runtime.in_flight_count == 1
        if stopping is not None:
            assert not stopping.done()
        release.set()
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert runtime.on_count == runtime.timer_count == 0
        assert not any(pulses)
    finally:
        release.set()
        if stopping is not None:
            await stopping
        await runtime.async_stop()


@pytest.mark.parametrize("result", [PHRASE, "wrong phrase", RuntimeError()])
async def test_stt_pacing_drops_surplus_and_uses_request_start_not_completion(
    result: str | BaseException,
) -> None:
    clock = FreshnessClock()
    source = QueuedSource()
    vad = FakeVad()
    stt = FakeStt([result, PHRASE])

    async def executor(function: Callable[[object], object], value: object) -> object:
        # Time spent matching must not shift the next request's start interval.
        clock.now += 2.0
        return function(value)

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        segmenter_factory=EveryFrameSegmenter,
        vad_factory=lambda: vad,
        async_executor=executor,
        monotonic=clock,
    )
    try:
        await runtime.async_start()
        source.push(VOICE)
        await wait_until(lambda: len(stt.pcm) == 1 and runtime.in_flight_count == 0)
        assert runtime.is_on_for(KEY_A) is (result == PHRASE)
        clock.now = 104.999
        for _ in range(20):
            source.push(SILENCE)
        await wait_until(lambda: len(vad.frames) == 21)
        await settle()
        assert stt.pcm == [VOICE]
        assert runtime.in_flight_count == 0
        clock.now = 105.0
        await settle()
        assert stt.pcm == [VOICE]  # No queued surplus wakes when the interval passes.
        source.push(VOICE)
        await wait_until(lambda: len(stt.pcm) == 2 and runtime.in_flight_count == 0)
        assert runtime.is_on_for(KEY_A)
    finally:
        await runtime.async_stop()


async def test_stt_pacing_is_per_target_and_survives_worker_replacement() -> None:
    clock = FreshnessClock()
    snapshot = {KEY_A: door(), KEY_B: door(KEY_B, BINDING_B)}
    stored = phrase_storage()
    config = parse_enabled({KEY_A: (BINDING_A, stored), KEY_B: (BINDING_B, stored)})
    sources = [QueuedSource() for _ in range(3)]
    factory = SourceFactory(list(sources))
    stt = FakeStt()
    runtime = manager_for(
        config,
        snapshot.copy,
        route,
        factory,
        stt,
        segmenter_factory=EveryFrameSegmenter,
        monotonic=clock,
    )
    try:
        await runtime.async_start()
        await wait_until(lambda: len(factory.created) == 2)
        sources[0].push(VOICE)
        sources[1].push(SILENCE)
        await wait_until(lambda: len(stt.pcm) == 2 and runtime.in_flight_count == 0)
        snapshot.pop(KEY_A)
        runtime.reconcile()
        await wait_until(lambda: sources[0].closed == 1)
        snapshot[KEY_A] = door()
        runtime.reconcile()
        await wait_until(lambda: len(factory.created) == 3)
        clock.now = 104.0
        sources[2].push(VOICE)
        await settle()
        assert len(stt.pcm) == 2
        clock.now = 105.0
        sources[2].push(VOICE)
        await wait_until(lambda: len(stt.pcm) == 3)
    finally:
        await runtime.async_stop()


async def test_stt_pacing_starts_interval_after_global_limiter_wait() -> None:
    clock = FreshnessClock()
    source = QueuedSource()
    stt = FakeStt()
    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        SourceFactory([source]),
        stt,
        segmenter_factory=EveryFrameSegmenter,
        monotonic=clock,
    )
    limiter = voice_runtime._loop_limiter()
    await limiter.acquire()
    await limiter.acquire()
    try:
        await runtime.async_start()
        source.push(VOICE)
        await wait_until(lambda: runtime.in_flight_count == 1)
        await settle()
        clock.now = 104.0
    finally:
        limiter.release()
        limiter.release()
    try:
        await wait_until(lambda: len(stt.pcm) == 1 and runtime.in_flight_count == 0)
        clock.now = 105.0
        source.push(SILENCE)
        await settle()
        assert stt.pcm == [VOICE]
        assert runtime.in_flight_count == 0
        clock.now = 109.0
        source.push(VOICE)
        await wait_until(lambda: len(stt.pcm) == 2 and runtime.in_flight_count == 0)
    finally:
        await runtime.async_stop()
