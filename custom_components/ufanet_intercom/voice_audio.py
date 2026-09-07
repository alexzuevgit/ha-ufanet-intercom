"""Bounded 16 kHz PCM framing, segmentation, and detector adapters."""

from __future__ import annotations

import asyncio
import math
import re
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final, NoReturn, Protocol
from urllib.parse import urlsplit

SAMPLE_RATE: Final = 16_000
CHANNELS: Final = 1
SAMPLE_WIDTH_BYTES: Final = 2
FRAME_DURATION_MS: Final = 10
FRAME_SAMPLES: Final = SAMPLE_RATE * FRAME_DURATION_MS // 1_000
FRAME_BYTES: Final = FRAME_SAMPLES * CHANNELS * SAMPLE_WIDTH_BYTES

PRE_ROLL_FRAMES: Final = 60  # 400 ms before the 200 ms sustained-speech trigger.
START_VOICED_FRAMES: Final = 20
MIN_VOICED_FRAMES: Final = 30
END_SILENCE_FRAMES: Final = 120  # Preserve short internal pauses; still bounded by 8 s.
MAX_SEGMENT_FRAMES: Final = 800
VOICE_THRESHOLD: Final = 0.5

FFMPEG_AUDIO_ARGUMENTS: Final = (
    "-map",
    "0:a:0",
    "-vn",
    "-sn",
    "-dn",
    "-acodec",
    "pcm_s16le",
    "-ar",
    str(SAMPLE_RATE),
    "-ac",
    str(CHANNELS),
    "-f",
    "s16le",
    "pipe:1",
)

PROCESS_REAP_TIMEOUT_SECONDS = 2.0
_OPAQUE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


class VoiceAudioError(RuntimeError):
    """Fixed-detail failure at a voice-audio trust boundary."""


def _raise_fixed_error(message: str) -> NoReturn:
    try:
        raise VoiceAudioError(message)
    except VoiceAudioError as error:
        error.__cause__ = None
        error.__context__ = None
        error.__notes__ = []
        raise


class PcmSegmenter:
    """Build bounded utterances from exact 10 ms PCM/VAD observations."""

    def __init__(self) -> None:
        self._pre_roll: deque[tuple[bytes, bool]] = deque(maxlen=PRE_ROLL_FRAMES)
        self._active: list[bytes] = []
        self._start_voiced_frames = 0
        self._voiced_frames = 0
        self._consecutive_silence_frames = 0

    @property
    def pre_roll_frame_count(self) -> int:
        """Return the bounded number of idle frames retained."""

        return len(self._pre_roll)

    @property
    def active_frame_count(self) -> int:
        """Return the bounded number of active frames retained."""

        return len(self._active)

    @property
    def start_voiced_frame_count(self) -> int:
        """Return the current bounded start-trigger run length."""

        return self._start_voiced_frames

    @property
    def voiced_frame_count(self) -> int:
        """Return the number of voiced frames in the active segment."""

        return self._voiced_frames

    @property
    def consecutive_silence_frame_count(self) -> int:
        """Return the current bounded active trailing-silence count."""

        return self._consecutive_silence_frames

    def _reset(self) -> None:
        self._pre_roll.clear()
        self._active.clear()
        self._start_voiced_frames = 0
        self._voiced_frames = 0
        self._consecutive_silence_frames = 0

    def _finish(self) -> bytes | None:
        should_emit = self._voiced_frames >= MIN_VOICED_FRAMES
        frames = self._active
        try:
            return b"".join(frames) if should_emit else None
        finally:
            self._reset()

    def _process_valid(self, frame: bytes, speech_probability: float) -> bytes | None:
        voiced = speech_probability >= VOICE_THRESHOLD

        if not self._active:
            self._pre_roll.append((frame, voiced))
            if voiced:
                self._start_voiced_frames += 1
            else:
                self._start_voiced_frames = 0
            if self._start_voiced_frames < START_VOICED_FRAMES:
                return None

            self._active = [retained for retained, _ in self._pre_roll]
            self._voiced_frames = sum(
                1 for _, retained_voiced in self._pre_roll if retained_voiced
            )
            self._pre_roll.clear()
            self._start_voiced_frames = 0
            self._consecutive_silence_frames = 0
            if len(self._active) >= MAX_SEGMENT_FRAMES:
                return self._finish()
            return None

        self._active.append(frame)
        if voiced:
            self._voiced_frames += 1
            self._consecutive_silence_frames = 0
        else:
            self._consecutive_silence_frames += 1

        if len(self._active) >= MAX_SEGMENT_FRAMES:
            return self._finish()
        if self._consecutive_silence_frames >= END_SILENCE_FRAMES:
            return self._finish()
        return None

    def process(self, frame: object, speech_probability: object) -> bytes | None:
        """Consume one exact PCM frame and one trusted probability scalar.

        Invalid input is rejected before it can be retained. Any exception resets
        all segment state so a later utterance cannot inherit stale audio.
        """

        try:
            if (
                type(frame) is not bytes
                or len(frame) != FRAME_BYTES
                or type(speech_probability) is not float
                or not math.isfinite(speech_probability)
                or not 0.0 <= speech_probability <= 1.0
            ):
                _raise_fixed_error("PCM segment input is invalid")
            return self._process_valid(frame, speech_probability)
        except Exception:
            self._reset()
            raise


class _MicroVad(Protocol):
    def Process10ms(self, frame: bytes) -> object: ...


MicroVadFactory = Callable[[], _MicroVad]


def _default_microvad_factory() -> _MicroVad:
    from pymicro_vad import MicroVad

    return MicroVad()  # type: ignore[no-any-return]


class MicroVadAdapter:
    """Validate exact v1 ``pymicro-vad`` input and output boundaries."""

    def __init__(self, micro_vad_factory: MicroVadFactory | None = None) -> None:
        if micro_vad_factory is not None and not callable(micro_vad_factory):
            _raise_fixed_error("microVAD initialization failed")
        self._factory = (
            micro_vad_factory
            if micro_vad_factory is not None
            else _default_microvad_factory
        )
        self._detector: _MicroVad | None = None
        self.reset()

    def reset(self) -> None:
        """Discard detector history by constructing a fresh v1 instance."""

        self._detector = None
        try:
            detector = self._factory()
            if not callable(getattr(detector, "Process10ms", None)):
                raise TypeError
            self._detector = detector
        except Exception:  # noqa: BLE001 - expected detector failures are sanitized
            self._detector = None
        else:
            return
        _raise_fixed_error("microVAD initialization failed")

    def process(self, frame: object) -> float | None:
        """Return a normalized speech probability or ``None`` during warmup."""

        if type(frame) is not bytes or len(frame) != FRAME_BYTES:
            _raise_fixed_error("microVAD frame is invalid")
        detector = self._detector
        if detector is None:
            _raise_fixed_error("microVAD processing failed")
        try:
            result = detector.Process10ms(frame)
            if type(result) not in (int, float) or not math.isfinite(result):
                raise ValueError
            if result < 0:
                return None
            if result > 1:
                raise ValueError
            return float(result)
        except Exception:  # noqa: BLE001, S110 - return only fixed detail
            pass
        _raise_fixed_error("microVAD processing failed")


class _Reader(Protocol):
    async def read(self, size: int) -> bytes: ...


class _Process(Protocol):
    stdout: _Reader | None
    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


SubprocessFactory = Callable[..., Awaitable[_Process]]
_TaskOutcome = tuple[object | None, BaseException | None]


async def _capture_async_outcome(operation: Awaitable[object]) -> _TaskOutcome:
    """Make a child operation's exact terminal outcome inspectable."""

    try:
        return await operation, None
    except BaseException as error:  # noqa: BLE001 - includes cancellation identity
        return None, error


async def _await_terminal_outcome(
    task: asyncio.Task[_TaskOutcome],
    *,
    first_cancellation: asyncio.CancelledError | None = None,
    on_cancel: Callable[[], object] | None = None,
) -> tuple[object | None, BaseException | None, asyncio.CancelledError | None]:
    """Shield one child task while retaining the first caller cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if first_cancellation is None:
                first_cancellation = error
            if on_cancel is not None:
                on_cancel()
    result, error = task.result()
    return result, error, first_cancellation


async def _bounded_process_wait(
    process: _Process,
) -> tuple[bool, BaseException | None, bool]:
    """Wait once without leaving the process-wait task behind."""

    wait_task = asyncio.create_task(process.wait())
    done, _ = await asyncio.wait((wait_task,), timeout=PROCESS_REAP_TIMEOUT_SECONDS)
    if not done:
        wait_task.cancel()
        try:
            await wait_task
        except asyncio.CancelledError:
            pass
        except BaseException as error:  # noqa: BLE001 - preserve process control flow
            return False, error, True
        return False, None, True

    try:
        wait_task.result()
    except BaseException as error:  # noqa: BLE001 - preserve process control flow
        return False, error, False
    if process.returncode is None:
        return False, RuntimeError("process wait returned before exit"), False
    return True, None, False


async def _reap_process(process: _Process) -> BaseException | None:
    """Terminate, escalate if needed, and prove the exact process was reaped."""

    first_error: BaseException | None = None

    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except BaseException as error:  # noqa: BLE001 - cleanup must continue
            first_error = error

    verified, wait_error, _ = await _bounded_process_wait(process)
    if wait_error is not None and first_error is None:
        first_error = wait_error
    if verified:
        return first_error

    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except BaseException as error:  # noqa: BLE001 - cleanup must continue
            if first_error is None:
                first_error = error

    verified, wait_error, timed_out = await _bounded_process_wait(process)
    if wait_error is not None and first_error is None:
        first_error = wait_error
    if not verified:
        if first_error is not None:
            raise first_error
        if timed_out:
            raise TimeoutError("process did not exit")
        raise RuntimeError("process was not reaped")
    return first_error


def _valid_loopback_rtsp_url(value: object) -> bool:
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
    ):
        return False

    if not parsed.path.startswith("/") or parsed.path.count("/") != 1:
        return False
    segment = parsed.path[1:]
    return bool(
        segment
        and segment not in {".", ".."}
        and _OPAQUE_PATH_SEGMENT.fullmatch(segment) is not None
    )


class FfmpegPcmSource:
    """Read exact PCM frames from one explicitly reaped FFmpeg subprocess."""

    def __init__(
        self,
        ffmpeg_binary: str,
        source_url: str,
        subprocess_factory: SubprocessFactory | None = None,
    ) -> None:
        if (
            type(ffmpeg_binary) is not str
            or not ffmpeg_binary
            or "\x00" in ffmpeg_binary
            or not _valid_loopback_rtsp_url(source_url)
            or (subprocess_factory is not None and not callable(subprocess_factory))
        ):
            _raise_fixed_error("FFmpeg PCM source is invalid")
        self._ffmpeg_binary = ffmpeg_binary
        self._source_url = source_url
        self._factory: SubprocessFactory = (
            subprocess_factory
            if subprocess_factory is not None
            else asyncio.create_subprocess_exec  # type: ignore[assignment]
        )
        self._process: _Process | None = None
        self._reader: _Reader | None = None
        self._reader_generation = 0
        self._generation = 0
        self._start_task: asyncio.Task[_TaskOutcome] | None = None
        self._close_task: asyncio.Task[_TaskOutcome] | None = None
        self._close_requested = False
        self._start_attempted = False
        self._read_failure_callback: Callable[[], None] | None = None

    def set_read_failure_callback(self, callback: Callable[[], None]) -> None:
        """Register a trusted synchronous invalidator, without read-error details.

        Notification revokes evidence before cleanup; reads still own and await
        the exact child until reap. Explicit close is not a read failure.
        """

        self._read_failure_callback = callback

    @property
    def reader_attached(self) -> bool:
        """Return only whether a reader is attached, never its input details."""

        return self._reader is not None

    def _command(self) -> tuple[str, ...]:
        return (
            self._ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self._source_url,
            *FFMPEG_AUDIO_ARGUMENTS,
        )

    async def _run_start(self) -> bool:
        process = await self._factory(
            *self._command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if (
            not callable(getattr(process, "terminate", None))
            or not callable(getattr(process, "kill", None))
            or not callable(getattr(process, "wait", None))
            or not hasattr(process, "returncode")
        ):
            raise TypeError

        self._process = process
        self._generation += 1
        generation = self._generation
        reader = process.stdout
        if reader is None or not callable(getattr(reader, "read", None)):
            raise TypeError
        if self._close_requested:
            return False
        self._reader = reader
        self._reader_generation = generation
        return True

    async def _run_close(self) -> None:
        start_task = self._start_task
        if start_task is not None and not start_task.done():
            await asyncio.shield(start_task)

        self._reader = None
        process = self._process
        if process is None:
            return

        process_error = await _reap_process(process)
        if self._process is process:
            self._process = None
        if process_error is not None:
            raise process_error

    def _begin_close(
        self, expected_process: _Process | None = None
    ) -> asyncio.Task[_TaskOutcome] | None:
        if expected_process is not None and self._process is not expected_process:
            return None
        self._close_requested = True
        self._reader = None
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(_capture_async_outcome(self._run_close()))
            self._close_task = close_task
        return close_task

    async def _finish_close(
        self,
        *,
        expected_process: _Process | None = None,
        first_cancellation: asyncio.CancelledError | None = None,
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        close_task = self._begin_close(expected_process)
        if close_task is None:
            return None, first_cancellation

        _, close_error, cancellation = await _await_terminal_outcome(
            close_task,
            first_cancellation=first_cancellation,
        )
        if self._close_task is close_task:
            self._close_task = None
            if self._process is None and (
                self._start_task is None or self._start_task.done()
            ):
                self._start_task = None
                self._close_requested = False
        return close_error, cancellation

    @staticmethod
    def _raise_close_outcome(
        error: BaseException | None,
        cancellation: asyncio.CancelledError | None,
    ) -> None:
        if cancellation is not None:
            raise cancellation
        if isinstance(error, asyncio.CancelledError):
            raise error
        if error is not None and not isinstance(error, Exception):
            raise error
        if error is not None:
            _raise_fixed_error("FFmpeg PCM source failed")

    async def async_start(self) -> None:
        """Spawn one fixed-argv FFmpeg process and attach only its stdout."""

        if self._reader is not None:
            return
        if (
            self._close_requested
            or self._close_task is not None
            or self._process is not None
        ):
            _raise_fixed_error("FFmpeg PCM source failed")

        start_task = self._start_task
        if start_task is None:
            self._start_attempted = True
            start_task = asyncio.create_task(_capture_async_outcome(self._run_start()))
            self._start_task = start_task
        elif start_task.done():
            _raise_fixed_error("FFmpeg PCM source failed")

        started, start_error, cancellation = await _await_terminal_outcome(
            start_task,
            on_cancel=self._begin_close,
        )
        if (
            cancellation is None
            and start_error is None
            and started is True
            and self._start_task is start_task
            and self._reader is not None
            and self._process is not None
            and not self._close_requested
            and self._close_task is None
        ):
            return

        close_error: BaseException | None = None
        if self._start_task is start_task:
            close_error, cancellation = await self._finish_close(
                first_cancellation=cancellation
            )
        if cancellation is not None:
            raise cancellation
        if isinstance(start_error, asyncio.CancelledError):
            raise start_error
        if start_error is not None and not isinstance(start_error, Exception):
            raise start_error
        if isinstance(close_error, asyncio.CancelledError):
            raise close_error
        if close_error is not None and not isinstance(close_error, Exception):
            raise close_error
        _raise_fixed_error("FFmpeg PCM source failed")

    async def _close_after_read_outcome(
        self,
        process: _Process,
        original_error: BaseException | None,
    ) -> None:
        if self._process is process and not self._close_requested:
            callback = self._read_failure_callback
            self._read_failure_callback = None
            self._reader = None
            if callback is not None:
                callback()
        initial_cancellation = (
            original_error
            if isinstance(original_error, asyncio.CancelledError)
            else None
        )
        close_error, cancellation = await self._finish_close(
            expected_process=process,
            first_cancellation=initial_cancellation,
        )
        if original_error is not None and not isinstance(original_error, Exception):
            raise original_error
        if cancellation is not None:
            raise cancellation
        if isinstance(close_error, asyncio.CancelledError):
            raise close_error
        if close_error is not None and not isinstance(close_error, Exception):
            raise close_error
        if original_error is not None or close_error is not None:
            _raise_fixed_error("FFmpeg PCM source failed")

    async def async_read_frame(self) -> bytes | None:
        """Return one current complete frame and discard every stale partial read."""

        reader = self._reader
        process = self._process
        generation = self._reader_generation
        if reader is None or process is None:
            if self._start_attempted:
                return None
            _raise_fixed_error("FFmpeg PCM source failed")

        frame = bytearray()
        try:
            while len(frame) < FRAME_BYTES:
                remaining = FRAME_BYTES - len(frame)
                chunk = await reader.read(remaining)
                if (
                    self._reader is not reader
                    or self._process is not process
                    or self._reader_generation != generation
                    or self._close_requested
                ):
                    frame.clear()
                    return None
                if type(chunk) is not bytes or len(chunk) > remaining:
                    raise ValueError
                if not chunk:
                    frame.clear()
                    await self._close_after_read_outcome(process, None)
                    return None
                frame.extend(chunk)
            if (
                self._reader is not reader
                or self._process is not process
                or self._reader_generation != generation
                or self._close_requested
            ):
                frame.clear()
                return None
            return bytes(frame)
        except BaseException as error:  # noqa: BLE001 - preserve exact control flow
            frame.clear()
            stale = (
                self._reader is not reader
                or self._process is not process
                or self._reader_generation != generation
                or self._close_requested
            )
            if stale and isinstance(error, Exception):
                return None
            await self._close_after_read_outcome(process, error)
        raise AssertionError("unreachable")

    async def async_close(self) -> None:
        """Detach immediately, then terminate and explicitly reap exactly once."""

        self._read_failure_callback = None
        close_error, cancellation = await self._finish_close()
        self._raise_close_outcome(close_error, cancellation)
