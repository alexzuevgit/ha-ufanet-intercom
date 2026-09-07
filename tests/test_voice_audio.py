"""Pure tests for bounded intercom PCM capture and segmentation.

Synthetic PCM only; no test imports Home Assistant, runs ffmpeg, or uses a camera.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import math
import sys
import types
from collections.abc import Callable, Sequence

import pytest

from custom_components.ufanet_intercom import voice_audio
from custom_components.ufanet_intercom.voice_audio import (
    END_SILENCE_FRAMES,
    FFMPEG_AUDIO_ARGUMENTS,
    FRAME_BYTES,
    FRAME_DURATION_MS,
    FRAME_SAMPLES,
    MAX_SEGMENT_FRAMES,
    MIN_VOICED_FRAMES,
    PRE_ROLL_FRAMES,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    START_VOICED_FRAMES,
    VOICE_THRESHOLD,
    FfmpegPcmSource,
    MicroVadAdapter,
    PcmSegmenter,
    VoiceAudioError,
)

SILENCE = bytes(FRAME_BYTES)
VOICE = b"\x01\x00" * FRAME_SAMPLES
SOURCE = "rtsp://127.0.0.1:18554/opaque-stream-alias"


def feed(
    segmenter: PcmSegmenter,
    count: int,
    frame: bytes,
    probability: float,
) -> list[bytes]:
    emitted: list[bytes] = []
    for _ in range(count):
        result = segmenter.process(frame, probability)
        if result is not None:
            emitted.append(result)
    return emitted


def assert_reset(segmenter: PcmSegmenter) -> None:
    assert segmenter.pre_roll_frame_count == 0
    assert segmenter.active_frame_count == 0
    assert segmenter.voiced_frame_count == 0
    assert segmenter.consecutive_silence_frame_count == 0
    assert segmenter.start_voiced_frame_count == 0


def assert_fixed_error(error: VoiceAudioError, message: str) -> None:
    assert type(error) is VoiceAudioError
    assert error.args == (message,)
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []


def assert_fixed_error_under_active_exception(
    operation: Callable[[], object], message: str
) -> None:
    try:
        raise LookupError("PRIVATE OUTER ERROR")
    except LookupError:
        with pytest.raises(VoiceAudioError) as caught:
            operation()

    assert_fixed_error(caught.value, message)


def test_pcm_geometry_and_bounds_are_exact() -> None:
    assert SAMPLE_RATE == 16_000
    assert SAMPLE_WIDTH_BYTES == 2
    assert FRAME_DURATION_MS == 10
    assert FRAME_SAMPLES == 160
    assert FRAME_BYTES == 320
    assert PRE_ROLL_FRAMES == 60
    assert START_VOICED_FRAMES == 20
    assert MIN_VOICED_FRAMES == 30
    assert END_SILENCE_FRAMES == 120
    assert MAX_SEGMENT_FRAMES == 800
    assert VOICE_THRESHOLD == 0.5


def test_ffmpeg_audio_arguments_are_exact() -> None:
    assert FFMPEG_AUDIO_ARGUMENTS == (
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
    )


class BytesSubclass(bytes):
    """A bytes subclass must not cross the exact PCM boundary."""


class FloatSubclass(float):
    """A float subclass must not cross the exact probability boundary."""


@pytest.mark.parametrize(
    ("frame", "probability"),
    [
        (SILENCE[:-1], 0.0),
        (SILENCE + b"\x00", 0.0),
        (bytearray(SILENCE), 0.0),
        (memoryview(SILENCE), 0.0),
        (BytesSubclass(SILENCE), 0.0),
        (SILENCE, 0),
        (SILENCE, 1),
        (SILENCE, False),
        (SILENCE, None),
        (SILENCE, FloatSubclass(0.5)),
        (SILENCE, -0.0001),
        (SILENCE, 1.0001),
        (SILENCE, math.nan),
        (SILENCE, math.inf),
        (SILENCE, -math.inf),
    ],
)
def test_segmenter_rejects_off_by_one_types_and_probabilities_and_resets(
    frame: object, probability: object
) -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, START_VOICED_FRAMES, VOICE, 1.0)
    assert segmenter.active_frame_count > 0

    with pytest.raises(VoiceAudioError, match=r"^PCM segment input is invalid$"):
        segmenter.process(frame, probability)

    assert_reset(segmenter)


def test_long_idle_is_strictly_bounded() -> None:
    segmenter = PcmSegmenter()
    assert feed(segmenter, 10_000, SILENCE, 0.0) == []
    assert segmenter.pre_roll_frame_count == PRE_ROLL_FRAMES
    assert segmenter.active_frame_count == 0
    assert segmenter.voiced_frame_count == 0
    assert segmenter.consecutive_silence_frame_count == 0
    assert segmenter.start_voiced_frame_count == 0


def test_start_requires_exact_voiced_run_and_sustained_silence_resets_it() -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, START_VOICED_FRAMES - 1, VOICE, 1.0)
    assert segmenter.active_frame_count == 0
    assert segmenter.start_voiced_frame_count == START_VOICED_FRAMES - 1

    feed(segmenter, END_SILENCE_FRAMES, SILENCE, 0.0)
    assert segmenter.start_voiced_frame_count == 0
    assert segmenter.pre_roll_frame_count == PRE_ROLL_FRAMES

    feed(segmenter, START_VOICED_FRAMES - 1, VOICE, 1.0)
    assert segmenter.active_frame_count == 0
    assert segmenter.process(VOICE, 1.0) is None
    assert segmenter.active_frame_count == PRE_ROLL_FRAMES
    assert segmenter.voiced_frame_count == START_VOICED_FRAMES


def test_threshold_is_inclusive_at_one_half() -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, START_VOICED_FRAMES, VOICE, 0.499999)
    assert segmenter.active_frame_count == 0
    assert segmenter.process(VOICE, VOICE_THRESHOLD) is None
    assert segmenter.start_voiced_frame_count == 1
    feed(segmenter, START_VOICED_FRAMES - 1, VOICE, VOICE_THRESHOLD)
    assert segmenter.active_frame_count > 0


def test_minimum_voiced_and_end_silence_boundaries_emit_and_reset() -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, MIN_VOICED_FRAMES, VOICE, 1.0)
    assert feed(segmenter, END_SILENCE_FRAMES - 1, SILENCE, 0.0) == []
    assert segmenter.consecutive_silence_frame_count == END_SILENCE_FRAMES - 1
    result = segmenter.process(SILENCE, 0.0)

    assert result == (VOICE * MIN_VOICED_FRAMES) + (SILENCE * END_SILENCE_FRAMES)
    assert len(result) == (MIN_VOICED_FRAMES + END_SILENCE_FRAMES) * FRAME_BYTES
    assert_reset(segmenter)


def test_too_short_segment_is_discarded_and_reset() -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, MIN_VOICED_FRAMES - 1, VOICE, 1.0)
    assert feed(segmenter, END_SILENCE_FRAMES - 1, SILENCE, 0.0) == []
    assert segmenter.process(SILENCE, 0.0) is None
    assert_reset(segmenter)


def test_pre_roll_is_included_but_does_not_count_as_voiced() -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, PRE_ROLL_FRAMES - START_VOICED_FRAMES, SILENCE, 0.0)
    feed(segmenter, MIN_VOICED_FRAMES, VOICE, 1.0)
    result = feed(segmenter, END_SILENCE_FRAMES, SILENCE, 0.0)

    assert result == [
        (SILENCE * (PRE_ROLL_FRAMES - START_VOICED_FRAMES))
        + (VOICE * MIN_VOICED_FRAMES)
        + (SILENCE * END_SILENCE_FRAMES)
    ]


def test_active_hard_cap_includes_pre_roll_and_resets_at_exact_boundary() -> None:
    segmenter = PcmSegmenter()
    feed(segmenter, PRE_ROLL_FRAMES - START_VOICED_FRAMES, SILENCE, 0.0)
    feed(segmenter, START_VOICED_FRAMES, VOICE, 1.0)
    assert segmenter.active_frame_count == PRE_ROLL_FRAMES

    assert (
        feed(
            segmenter,
            MAX_SEGMENT_FRAMES - PRE_ROLL_FRAMES - 1,
            VOICE,
            1.0,
        )
        == []
    )
    assert segmenter.active_frame_count == MAX_SEGMENT_FRAMES - 1
    result = segmenter.process(VOICE, 1.0)

    assert type(result) is bytes
    assert len(result) == MAX_SEGMENT_FRAMES * FRAME_BYTES
    assert result.startswith(SILENCE * (PRE_ROLL_FRAMES - START_VOICED_FRAMES))
    assert_reset(segmenter)


def test_terminal_reset_does_not_duplicate_frames_into_next_segment() -> None:
    segmenter = PcmSegmenter()
    first = feed(segmenter, MAX_SEGMENT_FRAMES, VOICE, 1.0)
    assert len(first) == 1
    assert len(first[0]) == MAX_SEGMENT_FRAMES * FRAME_BYTES

    feed(segmenter, MIN_VOICED_FRAMES, VOICE, 1.0)
    second = feed(segmenter, END_SILENCE_FRAMES, SILENCE, 0.0)
    assert second == [(VOICE * MIN_VOICED_FRAMES) + (SILENCE * END_SILENCE_FRAMES)]


class FakeMicroVad:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.frames: list[bytes] = []

    def Process10ms(self, frame: bytes) -> object:
        self.frames.append(frame)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class DetectorBoundaryFailure(BaseException):
    """A detector failure that is deliberately outside Exception."""


class FalseyMicroVadFactory:
    def __init__(self, detector: FakeMicroVad) -> None:
        self.detector = detector
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self) -> FakeMicroVad:
        self.calls += 1
        return self.detector


def test_microvad_uses_exact_v1_api_and_normalizes_builtin_numbers() -> None:
    detector = FakeMicroVad([-1, -0.01, 0, 0.25, 1])
    adapter = MicroVadAdapter(lambda: detector)

    assert [adapter.process(VOICE) for _ in range(5)] == [
        None,
        None,
        0.0,
        0.25,
        1.0,
    ]
    assert detector.frames == [VOICE] * 5


@pytest.mark.parametrize(
    "result",
    [True, False, None, "0.5", object(), math.nan, math.inf, -math.inf, 1.0001],
)
def test_microvad_malformed_results_fail_with_fixed_safe_detail(result: object) -> None:
    adapter = MicroVadAdapter(lambda: FakeMicroVad([result]))
    with pytest.raises(
        VoiceAudioError, match=r"^microVAD processing failed$"
    ) as caught:
        adapter.process(VOICE)
    assert_fixed_error(caught.value, "microVAD processing failed")
    assert repr(result) not in str(caught.value)


def test_microvad_exception_is_wrapped_and_reset_creates_fresh_instance() -> None:
    detectors = [FakeMicroVad([RuntimeError("PRIVATE")]), FakeMicroVad([0.75])]
    adapter = MicroVadAdapter(lambda: detectors.pop(0))

    with pytest.raises(
        VoiceAudioError, match=r"^microVAD processing failed$"
    ) as caught:
        adapter.process(VOICE)
    assert_fixed_error(caught.value, "microVAD processing failed")
    assert "PRIVATE" not in str(caught.value)
    adapter.reset()
    assert adapter.process(VOICE) == 0.75
    assert detectors == []


@pytest.mark.parametrize(
    "failure",
    [
        DetectorBoundaryFailure("BOUNDARY"),
        SystemExit("SYSTEM EXIT"),
        KeyboardInterrupt("KEYBOARD INTERRUPT"),
        asyncio.CancelledError("CANCELLED"),
    ],
)
def test_microvad_factory_preserves_control_flow_base_exceptions(
    failure: BaseException,
) -> None:
    def factory() -> FakeMicroVad:
        raise failure

    with pytest.raises(type(failure)) as caught:
        MicroVadAdapter(factory)

    assert caught.value is failure


@pytest.mark.parametrize(
    "failure",
    [
        DetectorBoundaryFailure("BOUNDARY"),
        SystemExit("SYSTEM EXIT"),
        KeyboardInterrupt("KEYBOARD INTERRUPT"),
        asyncio.CancelledError("CANCELLED"),
    ],
)
def test_microvad_process_preserves_control_flow_base_exceptions(
    failure: BaseException,
) -> None:
    adapter = MicroVadAdapter(lambda: FakeMicroVad([failure]))

    with pytest.raises(type(failure)) as caught:
        adapter.process(VOICE)

    assert caught.value is failure


def test_microvad_honors_injected_falsey_callable_factory() -> None:
    detector = FakeMicroVad([0.5])
    factory = FalseyMicroVadFactory(detector)

    adapter = MicroVadAdapter(factory)

    assert adapter.process(VOICE) == 0.5
    assert factory.calls == 1


def test_sync_public_errors_are_sanitized_under_active_exception() -> None:
    def failed_factory() -> FakeMicroVad:
        raise RuntimeError("PRIVATE INNER ERROR")

    cases: list[tuple[Callable[[], object], str]] = [
        (
            lambda: PcmSegmenter().process(VOICE[:-1], 1.0),
            "PCM segment input is invalid",
        ),
        (
            lambda: MicroVadAdapter(object()),  # type: ignore[arg-type]
            "microVAD initialization failed",
        ),
        (lambda: MicroVadAdapter(failed_factory), "microVAD initialization failed"),
        (
            lambda: MicroVadAdapter(lambda: FakeMicroVad([])).process(VOICE[:-1]),
            "microVAD frame is invalid",
        ),
        (
            lambda: FfmpegPcmSource(
                "",
                SOURCE,
                subprocess_factory=lambda *_, **__: object(),  # type: ignore[arg-type]
            ),
            "FFmpeg PCM source is invalid",
        ),
    ]

    for operation, message in cases:
        assert_fixed_error_under_active_exception(operation, message)


def test_microvad_rejects_invalid_frame_before_detector_call() -> None:
    detector = FakeMicroVad([0.5])
    adapter = MicroVadAdapter(lambda: detector)
    with pytest.raises(VoiceAudioError, match=r"^microVAD frame is invalid$"):
        adapter.process(VOICE[:-1])
    assert detector.frames == []


def test_microvad_default_factory_is_imported_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detector = FakeMicroVad([0.5])
    module = types.ModuleType("pymicro_vad")
    module.MicroVad = lambda: detector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymicro_vad", module)

    adapter = MicroVadAdapter()
    assert adapter.process(VOICE) == 0.5
    assert detector.frames == [VOICE]


def test_microvad_default_factory_matches_pinned_dependency_api() -> None:
    pytest.importorskip("pymicro_vad")
    assert importlib.metadata.version("pymicro-vad") == "1.0.1"

    adapter = MicroVadAdapter()

    assert adapter.process(SILENCE) is None


class FakeReader:
    def __init__(
        self,
        chunks: Sequence[bytes | BaseException],
        *,
        read_started: asyncio.Event | None = None,
        release_read: asyncio.Event | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.requests: list[int] = []
        self.read_started = read_started
        self.release_read = release_read

    async def read(self, size: int) -> bytes:
        self.requests.append(size)
        if self.read_started is not None:
            self.read_started.set()
        if self.release_read is not None:
            await self.release_read.wait()
        chunk = self.chunks.pop(0) if self.chunks else b""
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


class FakeProcess:
    def __init__(
        self,
        reader: FakeReader,
        *,
        exit_on_terminate: bool = True,
        exit_on_kill: bool = True,
        terminate_error: BaseException | None = None,
        kill_error: BaseException | None = None,
        wait_errors: Sequence[BaseException] = (),
    ) -> None:
        self.stdout = reader
        self.returncode: int | None = None
        self.exit_on_terminate = exit_on_terminate
        self.exit_on_kill = exit_on_kill
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_errors = list(wait_errors)
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.terminate_called = asyncio.Event()
        self.kill_called = asyncio.Event()
        self.first_wait_started = asyncio.Event()
        self.second_wait_started = asyncio.Event()
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.terminate_called.set()
        if self.terminate_error is not None:
            raise self.terminate_error
        if self.exit_on_terminate:
            self.reap(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self.kill_called.set()
        if self.kill_error is not None:
            raise self.kill_error
        if self.exit_on_kill:
            self.reap(-9)

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            self.first_wait_started.set()
        if self.wait_calls == 2:
            self.second_wait_started.set()
        if self.wait_errors:
            raise self.wait_errors.pop(0)
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def reap(self, returncode: int) -> None:
        self.returncode = returncode
        self._exited.set()


class SubprocessFactory:
    def __init__(
        self,
        processes: FakeProcess | BaseException | Sequence[FakeProcess | BaseException],
        *,
        spawn_started: asyncio.Event | None = None,
        release_spawn: asyncio.Event | None = None,
    ) -> None:
        if isinstance(processes, Sequence) and not isinstance(processes, BaseException):
            self.processes = list(processes)
        else:
            self.processes = [processes]
        self.spawn_started = spawn_started
        self.release_spawn = release_spawn
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def __call__(self, *argv: object, **kwargs: object) -> FakeProcess:
        self.calls.append((argv, kwargs))
        if self.spawn_started is not None:
            self.spawn_started.set()
        if self.release_spawn is not None:
            await self.release_spawn.wait()
        result = self.processes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_source(
    process: FakeProcess,
    *,
    binary: str = "ffmpeg",
) -> tuple[FfmpegPcmSource, SubprocessFactory]:
    factory = SubprocessFactory(process)
    return (
        FfmpegPcmSource(binary, SOURCE, subprocess_factory=factory),
        factory,
    )


async def wait_until_task_done(task: asyncio.Task[object]) -> None:
    done, _ = await asyncio.wait((task,), timeout=1)
    assert done == {task}


@pytest.mark.asyncio
async def test_ffmpeg_start_uses_exact_argv_and_bounded_pipes_once() -> None:
    process = FakeProcess(FakeReader([VOICE]))
    source, factory = make_source(process, binary="/validated/ffmpeg")

    await source.async_start()
    await source.async_start()

    assert factory.calls == [
        (
            (
                "/validated/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                SOURCE,
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ),
            {
                "stdin": asyncio.subprocess.DEVNULL,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.DEVNULL,
            },
        )
    ]
    assert source.reader_attached is True
    assert await source.async_read_frame() == VOICE
    await source.async_close()


@pytest.mark.asyncio
async def test_ffmpeg_reader_requests_only_remaining_bytes() -> None:
    reader = FakeReader([VOICE[:3], VOICE[3:213], VOICE[213:]])
    process = FakeProcess(reader)
    source, _ = make_source(process)
    await source.async_start()

    assert await source.async_read_frame() == VOICE
    assert reader.requests == [FRAME_BYTES, FRAME_BYTES - 3, FRAME_BYTES - 213]
    await source.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [[b""], [VOICE[:17], b""]])
async def test_ffmpeg_eof_discards_partial_frame_and_reaps_once(
    chunks: list[bytes],
) -> None:
    process = FakeProcess(FakeReader(chunks))
    source, _ = make_source(process)
    await source.async_start()

    assert await source.async_read_frame() is None
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert source.reader_attached is False
    assert source._process is None
    await source.async_close()
    await source.async_close()
    assert process.terminate_calls == 1


@pytest.mark.asyncio
async def test_ffmpeg_terminate_then_wait_reaps_without_kill() -> None:
    process = FakeProcess(FakeReader([]))
    source, _ = make_source(process)
    await source.async_start()

    await source.async_close()

    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0
    assert process.returncode == -15
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_reap_timeout_escalates_to_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_audio, "PROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    process = FakeProcess(
        FakeReader([]),
        exit_on_terminate=False,
        exit_on_kill=True,
    )
    source, _ = make_source(process)
    await source.async_start()

    await source.async_close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert process.returncode == -9
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_close_during_spawn_waits_then_reaps_without_reader() -> None:
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    process = FakeProcess(FakeReader([VOICE]))
    factory = SubprocessFactory(
        process,
        spawn_started=spawn_started,
        release_spawn=release_spawn,
    )
    source = FfmpegPcmSource("ffmpeg", SOURCE, subprocess_factory=factory)

    starting = asyncio.create_task(source.async_start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    closing = asyncio.create_task(source.async_close())
    await asyncio.sleep(0)

    assert closing.done() is False
    assert source.reader_attached is False
    release_spawn.set()
    await asyncio.wait_for(closing, timeout=1)
    with pytest.raises(VoiceAudioError) as caught:
        await starting

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert source._process is None
    assert source.reader_attached is False


@pytest.mark.asyncio
async def test_ffmpeg_close_after_spawn_completion_prevents_start_success() -> None:
    process = FakeProcess(FakeReader([VOICE]))
    source: FfmpegPcmSource

    async def close_before_start_waiter_resumes(
        *argv: object, **kwargs: object
    ) -> FakeProcess:
        del argv, kwargs
        asyncio.get_running_loop().call_soon(source._begin_close)
        return process

    source = FfmpegPcmSource(
        "ffmpeg",
        SOURCE,
        subprocess_factory=close_before_start_waiter_resumes,
    )

    with pytest.raises(VoiceAudioError) as caught:
        await source.async_start()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert source._process is None
    assert source.reader_attached is False


@pytest.mark.asyncio
async def test_ffmpeg_caller_cancellation_during_spawn_reaps_then_reraises_first() -> (
    None
):
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    process = FakeProcess(FakeReader([VOICE]))
    factory = SubprocessFactory(
        process,
        spawn_started=spawn_started,
        release_spawn=release_spawn,
    )
    source = FfmpegPcmSource("ffmpeg", SOURCE, subprocess_factory=factory)

    starting = asyncio.create_task(source.async_start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    starting.cancel("FIRST SPAWN CANCELLATION")
    await asyncio.sleep(0)
    starting.cancel("SECOND SPAWN CANCELLATION")
    await asyncio.sleep(0)

    assert starting.done() is False
    release_spawn.set()
    await wait_until_task_done(starting)
    with pytest.raises(asyncio.CancelledError) as caught:
        starting.result()

    assert caught.value.args == ("FIRST SPAWN CANCELLATION",)
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert source._process is None
    assert source.reader_attached is False


@pytest.mark.asyncio
async def test_ffmpeg_close_is_shared_idempotent_and_retains_process_until_reap() -> (
    None
):
    process = FakeProcess(FakeReader([]), exit_on_terminate=False)
    source, _ = make_source(process)
    await source.async_start()

    first = asyncio.create_task(source.async_close())
    second = asyncio.create_task(source.async_close())
    await asyncio.wait_for(process.first_wait_started.wait(), timeout=1)

    assert first.done() is False
    assert second.done() is False
    assert source._process is process
    assert source.reader_attached is False
    process.reap(-15)
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    await source.async_close()

    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_close_cancellation_during_terminate_wait_is_deferred() -> None:
    process = FakeProcess(FakeReader([]), exit_on_terminate=False)
    source, _ = make_source(process)
    await source.async_start()

    closing = asyncio.create_task(source.async_close())
    await asyncio.wait_for(process.first_wait_started.wait(), timeout=1)
    closing.cancel("FIRST TERMINATE-WAIT CANCELLATION")
    await asyncio.sleep(0)
    closing.cancel("SECOND TERMINATE-WAIT CANCELLATION")
    await asyncio.sleep(0)

    assert closing.done() is False
    assert source._process is process
    process.reap(-15)
    await wait_until_task_done(closing)
    with pytest.raises(asyncio.CancelledError) as caught:
        closing.result()

    assert caught.value.args == ("FIRST TERMINATE-WAIT CANCELLATION",)
    assert process.wait_calls == 1
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_close_cancellation_during_kill_wait_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_audio, "PROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    process = FakeProcess(
        FakeReader([]),
        exit_on_terminate=False,
        exit_on_kill=False,
    )
    source, _ = make_source(process)
    await source.async_start()

    closing = asyncio.create_task(source.async_close())
    await asyncio.wait_for(process.second_wait_started.wait(), timeout=1)
    closing.cancel("KILL-WAIT CANCELLATION")
    await asyncio.sleep(0)

    assert closing.done() is False
    assert process.kill_calls == 1
    assert source._process is process
    process.reap(-9)
    await wait_until_task_done(closing)
    with pytest.raises(asyncio.CancelledError) as caught:
        closing.result()

    assert caught.value.args == ("KILL-WAIT CANCELLATION",)
    assert process.wait_calls == 2
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_partial_read_cancellation_discards_and_reaps_before_reraise() -> (
    None
):
    cancellation = asyncio.CancelledError("PARTIAL READ CANCELLED")
    cancellation.add_note("READ NOTE")
    reader = FakeReader([VOICE[:17], cancellation])
    process = FakeProcess(reader)
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(asyncio.CancelledError) as caught:
        await source.async_read_frame()

    assert caught.value is cancellation
    assert caught.value.__notes__ == ["READ NOTE"]
    assert reader.requests == [FRAME_BYTES, FRAME_BYTES - 17]
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_read_racing_completed_close_never_returns_late_frame() -> None:
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    reader = FakeReader(
        [VOICE],
        read_started=read_started,
        release_read=release_read,
    )
    process = FakeProcess(reader)
    source, _ = make_source(process)
    await source.async_start()

    reading = asyncio.create_task(source.async_read_frame())
    await asyncio.wait_for(read_started.wait(), timeout=1)
    await asyncio.wait_for(source.async_close(), timeout=1)
    assert reading.done() is False

    release_read.set()
    assert await asyncio.wait_for(reading, timeout=1) is None
    assert process.wait_calls == 1
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_stale_read_error_after_close_is_discarded() -> None:
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    reader = FakeReader(
        [RuntimeError("PRIVATE LATE READ FAILURE")],
        read_started=read_started,
        release_read=release_read,
    )
    process = FakeProcess(reader)
    source, _ = make_source(process)
    await source.async_start()

    reading = asyncio.create_task(source.async_read_frame())
    await asyncio.wait_for(read_started.wait(), timeout=1)
    await asyncio.wait_for(source.async_close(), timeout=1)
    release_read.set()

    assert await asyncio.wait_for(reading, timeout=1) is None
    assert process.wait_calls == 1
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_read_caller_cancellation_reaps_before_exact_reraise() -> None:
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    reader = FakeReader(
        [VOICE[:17]],
        read_started=read_started,
        release_read=release_read,
    )
    process = FakeProcess(reader)
    source, _ = make_source(process)
    await source.async_start()

    reading = asyncio.create_task(source.async_read_frame())
    await asyncio.wait_for(read_started.wait(), timeout=1)
    reading.cancel("READ CALLER CANCELLATION")
    await wait_until_task_done(reading)
    with pytest.raises(asyncio.CancelledError) as caught:
        reading.result()

    assert caught.value.args == ("READ CALLER CANCELLATION",)
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_restart_occurs_only_after_verified_reap() -> None:
    first_process = FakeProcess(FakeReader([]), exit_on_terminate=False)
    second_process = FakeProcess(FakeReader([]))
    factory = SubprocessFactory([first_process, second_process])
    source = FfmpegPcmSource("ffmpeg", SOURCE, subprocess_factory=factory)
    await source.async_start()

    closing = asyncio.create_task(source.async_close())
    await asyncio.wait_for(first_process.first_wait_started.wait(), timeout=1)
    with pytest.raises(VoiceAudioError) as caught:
        await source.async_start()
    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert len(factory.calls) == 1

    first_process.reap(-15)
    await asyncio.wait_for(closing, timeout=1)
    await source.async_start()

    assert len(factory.calls) == 2
    assert source._process is second_process
    await source.async_close()


@pytest.mark.asyncio
async def test_ffmpeg_failed_reap_is_sanitized_and_retains_handle_for_retry() -> None:
    process = FakeProcess(
        FakeReader([]),
        wait_errors=[
            RuntimeError("PRIVATE WAIT FAILURE"),
            RuntimeError("SECOND PRIVATE WAIT FAILURE"),
        ],
    )
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(VoiceAudioError) as caught:
        await source.async_close()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert "PRIVATE" not in repr(caught.value)
    assert source._process is process
    with pytest.raises(VoiceAudioError):
        await source.async_start()

    await source.async_close()
    assert process.wait_calls == 3
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_kill_wait_timeout_is_fixed_and_retains_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_audio, "PROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    process = FakeProcess(
        FakeReader([]),
        exit_on_terminate=False,
        exit_on_kill=False,
    )
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(VoiceAudioError) as caught:
        await source.async_close()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert source._process is process

    process.reap(-9)
    await source.async_close()
    assert process.wait_calls == 3
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_process_control_cancelled_error_is_preserved_after_reap() -> None:
    cancellation = asyncio.CancelledError("PROCESS WAIT CANCELLED")
    process = FakeProcess(FakeReader([]), wait_errors=[cancellation])
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(asyncio.CancelledError) as caught:
        await source.async_close()

    assert caught.value is cancellation
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_terminate_control_flow_is_preserved_after_kill_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_audio, "PROCESS_REAP_TIMEOUT_SECONDS", 0.01)
    control_flow = SystemExit("TERMINATE CONTROL FLOW")
    process = FakeProcess(
        FakeReader([]),
        terminate_error=control_flow,
    )
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(SystemExit) as caught:
        await source.async_close()

    assert caught.value is control_flow
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_spawn_cancelled_error_is_preserved_exactly() -> None:
    cancellation = asyncio.CancelledError("SPAWN CONTROL CANCELLATION")
    factory = SubprocessFactory(cancellation)
    source = FfmpegPcmSource("ffmpeg", SOURCE, subprocess_factory=factory)

    with pytest.raises(asyncio.CancelledError) as caught:
        await source.async_start()

    assert caught.value is cancellation
    assert source._process is None
    assert source.reader_attached is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spawned",
    [RuntimeError("PRIVATE SPAWN FAILURE"), object()],
)
async def test_ffmpeg_start_failure_is_fixed_detail_and_has_no_reader(
    spawned: object,
) -> None:
    factory = SubprocessFactory(spawned)  # type: ignore[arg-type]
    source = FfmpegPcmSource("ffmpeg", SOURCE, subprocess_factory=factory)

    with pytest.raises(VoiceAudioError) as caught:
        await source.async_start()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert "PRIVATE" not in repr(caught.value)
    assert source.reader_attached is False


@pytest.mark.asyncio
async def test_ffmpeg_error_is_sanitized_under_active_exception() -> None:
    factory = SubprocessFactory(RuntimeError("PRIVATE INNER ERROR"))
    source = FfmpegPcmSource("ffmpeg", SOURCE, subprocess_factory=factory)

    try:
        raise LookupError("PRIVATE OUTER ERROR")
    except LookupError:
        with pytest.raises(VoiceAudioError) as caught:
            await source.async_start()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")


@pytest.mark.asyncio
async def test_ffmpeg_rejects_oversized_reader_chunk_and_reaps() -> None:
    process = FakeProcess(FakeReader([VOICE + b"\x00"]))
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(VoiceAudioError) as caught:
        await source.async_read_frame()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert process.terminate_calls == 1
    assert source._process is None


@pytest.mark.asyncio
async def test_ffmpeg_read_exception_is_reaped_and_has_no_retained_context() -> None:
    process = FakeProcess(FakeReader([RuntimeError("PRIVATE")]))
    source, _ = make_source(process)
    await source.async_start()

    with pytest.raises(VoiceAudioError) as caught:
        await source.async_read_frame()

    assert_fixed_error(caught.value, "FFmpeg PCM source failed")
    assert "PRIVATE" not in repr(caught.value)
    assert process.wait_calls == 1
    assert source._process is None


@pytest.mark.parametrize(
    "source_url",
    [
        "",
        "http://127.0.0.1:18554/opaque",
        "rtsp://localhost:18554/opaque",
        "rtsp://127.0.0.2:18554/opaque",
        "rtsp://127.0.0.1/opaque",
        "rtsp://127.0.0.1:0/opaque",
        "rtsp://127.0.0.1:65536/opaque",
        "rtsp://127.0.0.1:notaport/opaque",
        "rtsp://user@127.0.0.1:18554/opaque",
        "rtsp://127.0.0.1:18554",
        "rtsp://127.0.0.1:18554/",
        "rtsp://127.0.0.1:18554/two/segments",
        "rtsp://127.0.0.1:18554/opaque/",
        "rtsp://127.0.0.1:18554/opaque?token=SECRET",
        "rtsp://127.0.0.1:18554/opaque#fragment",
        "rtsp://127.0.0.1:18554/opaque?",
        "rtsp://127.0.0.1:18554/opaque#",
        "rtsp://127.0.0.1:18554/opaque?#",
        "rtsp://127.0.0.1:18554/opaque alias",
        "rtsp://127.0.0.1:18554/%2F",
        "rtsp://[0:0:0:0:0:0:0:1]:18554/opaque",
    ],
)
def test_ffmpeg_source_rejects_noncanonical_or_unsafe_urls(source_url: str) -> None:
    with pytest.raises(VoiceAudioError, match=r"^FFmpeg PCM source is invalid$"):
        FfmpegPcmSource(
            "ffmpeg",
            source_url,
            subprocess_factory=lambda *_, **__: object(),
        )


@pytest.mark.parametrize(
    "source_url",
    [
        SOURCE,
        "rtsp://[::1]:65535/opaque-stream-alias",
    ],
)
def test_ffmpeg_source_accepts_exact_ipv4_and_ipv6_loopback_urls(
    source_url: str,
) -> None:
    FfmpegPcmSource(
        "ffmpeg",
        source_url,
        subprocess_factory=lambda *_, **__: object(),
    )


@pytest.mark.parametrize(
    ("binary", "source_url", "factory"),
    [
        ("", SOURCE, lambda *_, **__: object()),
        (b"ffmpeg", SOURCE, lambda *_, **__: object()),
        (
            "ffmpeg",
            b"rtsp://127.0.0.1:18554/opaque",
            lambda *_, **__: object(),
        ),
        ("ffmpeg", SOURCE, object()),
    ],
)
def test_ffmpeg_constructor_requires_exact_types_and_callable_factory(
    binary: object, source_url: object, factory: object
) -> None:
    with pytest.raises(VoiceAudioError, match=r"^FFmpeg PCM source is invalid$"):
        FfmpegPcmSource(  # type: ignore[arg-type]
            binary,
            source_url,
            subprocess_factory=factory,
        )
