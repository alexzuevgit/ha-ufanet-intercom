"""Read-loss must revoke evidence before the decoder's terminal reap."""

from __future__ import annotations

import asyncio
import sys

import pytest

from custom_components.ufanet_intercom.voice_audio import (
    END_SILENCE_FRAMES,
    FfmpegPcmSource,
    PcmSegmenter,
)
from test_voice_audio import FakeProcess, SubprocessFactory
from test_voice_runtime import (
    KEY_A,
    PHRASE,
    VOICE,
    EveryFrameSegmenter,
    FakeSource,
    FakeStt,
    FakeVad,
    door,
    manager_for,
    parse_enabled,
    route,
    wait_until,
)


class QueuedReader:
    def __init__(self) -> None:
        self.items: asyncio.Queue[bytes | BaseException] = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def read(self, _size: int) -> bytes:
        self.task = asyncio.current_task()
        item = await self.items.get()
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.parametrize("outcome", ["eof", "error", "cancel", "timeout"])
@pytest.mark.parametrize("already_on", [False, True])
async def test_read_loss_invalidates_before_held_reap(
    outcome: str, already_on: bool
) -> None:
    reader = QueuedReader()
    process = FakeProcess(reader, exit_on_terminate=False)
    source = FfmpegPcmSource("ffmpeg", route(KEY_A), SubprocessFactory(process))
    stt_started = asyncio.Event()
    stt_cancelled = asyncio.Event()
    release_stt = asyncio.Event()
    factory_calls = []
    snapshot = {KEY_A: door()}

    class Stt:
        calls = 0

        async def transcribe_pcm(self, _pcm: bytes) -> str:
            self.calls += 1
            if already_on and self.calls == 1:
                return PHRASE
            stt_started.set()
            try:
                await release_stt.wait()
            except asyncio.CancelledError:
                stt_cancelled.set()
                await release_stt.wait()
            return PHRASE

    def factory(*_args):
        factory_calls.append(None)
        return source if len(factory_calls) == 1 else FakeSource([VOICE])

    runtime = manager_for(
        parse_enabled(),
        lambda: snapshot,
        route,
        factory,
        Stt(),
        segmenter_factory=EveryFrameSegmenter,
        frame_timeout_seconds=0.2 if outcome == "timeout" else 10,
        pulse_seconds=30,
        min_stt_interval_seconds=0,
    )
    states = []
    runtime.add_listener(
        lambda: states.append((runtime.available_for(KEY_A), runtime.is_on_for(KEY_A)))
    )
    try:
        await runtime.async_start()
        reader.items.put_nowait(VOICE)
        if already_on:
            await wait_until(lambda: runtime.is_on_for(KEY_A))
            await wait_until(lambda: runtime.in_flight_count == 0)
            reader.items.put_nowait(VOICE)
        await asyncio.wait_for(stt_started.wait(), 1)
        worker = runtime._workers[KEY_A]
        epoch = worker.audio_epoch
        stale_callback = source._read_failure_callback
        assert runtime.available_for(KEY_A)
        assert runtime.is_on_for(KEY_A) is already_on
        if outcome == "eof":
            reader.items.put_nowait(b"")
        elif outcome == "error":
            reader.items.put_nowait(OSError("private read failure"))
        elif outcome == "cancel":
            reader.task.cancel("first read cancellation")
        # Timeout uses the real manager deadline; cleanup is event-held, not timed.
        await asyncio.wait_for(process.first_wait_started.wait(), 1)
        assert source.reader_attached is False
        assert process.returncode is None
        assert source._process is process
        assert not worker.task.done()
        assert worker.audio_epoch > epoch
        assert not runtime.available_for(KEY_A)
        assert not runtime.is_on_for(KEY_A)
        await asyncio.wait_for(stt_cancelled.wait(), 1)
        assert runtime.in_flight_count == 1
        failure_state_index = len(states)
        release_stt.set()
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert not any(on for _, on in states[failure_state_index:])
        assert not runtime.available_for(KEY_A)
        assert not runtime.is_on_for(KEY_A)
        # Route churn cannot replace a source while it still owns a live child.
        snapshot.clear()
        runtime.reconcile()
        snapshot[KEY_A] = door()
        runtime.reconcile()
        assert len(factory_calls) == 1
        assert source._process is process
        process.reap(-15)
        await wait_until(lambda: len(factory_calls) == 2)
        await wait_until(lambda: runtime.available_for(KEY_A))
        assert source._process is None
        successor = runtime._workers[KEY_A]
        successor_epoch = successor.audio_epoch
        stale_callback()
        assert runtime.available_for(KEY_A)
        assert successor.audio_epoch == successor_epoch
    finally:
        process.reap(-15)
        release_stt.set()
        await asyncio.wait_for(runtime.async_stop(), 3)
    assert runtime.worker_count == runtime.in_flight_count == runtime.timer_count == 0


async def test_explicit_source_close_does_not_report_read_failure() -> None:
    process = FakeProcess(QueuedReader(), exit_on_terminate=False)
    source = FfmpegPcmSource("ffmpeg", route(KEY_A), SubprocessFactory(process))
    notifications = []
    source.set_read_failure_callback(lambda: notifications.append(None))
    await source.async_start()
    closing = asyncio.create_task(source.async_close())
    try:
        await asyncio.wait_for(process.first_wait_started.wait(), 1)
        assert not closing.done()
        assert source._process is process
        assert notifications == []
        assert source._read_failure_callback is None
    finally:
        process.reap(-15)
        await closing


async def test_manager_stop_keeps_reap_owned_and_old_callback_inert() -> None:
    reader = QueuedReader()
    process = FakeProcess(reader, exit_on_terminate=False)
    source = FfmpegPcmSource("ffmpeg", route(KEY_A), SubprocessFactory(process))
    calls = []

    def factory(*_args):
        calls.append(None)
        return source if len(calls) == 1 else FakeSource([VOICE])

    runtime = manager_for(
        parse_enabled(), lambda: {KEY_A: door()}, route, factory, FakeStt()
    )
    stopping = None
    try:
        await runtime.async_start()
        reader.items.put_nowait(VOICE)
        await wait_until(lambda: runtime.available_for(KEY_A))
        old_callback = source._read_failure_callback
        stopping = asyncio.create_task(runtime.async_stop())
        await asyncio.wait_for(process.first_wait_started.wait(), 1)
        old_callback()
        runtime.reconcile()
        assert not stopping.done()
        assert not runtime.available_for(KEY_A)
        assert len(calls) == 1
        assert source._process is process
        process.reap(-15)
        await stopping
        await runtime.async_start()
        await wait_until(lambda: runtime.available_for(KEY_A))
        old_callback()
        assert runtime.available_for(KEY_A)
        assert len(calls) == 2
    finally:
        process.reap(-15)
        if stopping is not None:
            await stopping
        await runtime.async_stop()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stdout/TERM probe")
async def test_real_subprocess_eof_cannot_publish_during_term_kill_reap() -> None:
    """Original production-source/segmenter repro, without audio or network IO."""
    child = None
    stt_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def spawn(*_argv, **kwargs):
        nonlocal child
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"os.write(1, b'\\x01\\x00'*160*40 + b'\\x00\\x00'*160*{END_SILENCE_FRAMES}); "
            "os.close(1); time.sleep(60)",
            **kwargs,
        )
        return child

    source = FfmpegPcmSource("ffmpeg", route(KEY_A), subprocess_factory=spawn)

    class Stt:
        async def transcribe_pcm(self, _pcm):
            stt_started.set()
            await release_stt.wait()
            return PHRASE

    runtime = manager_for(
        parse_enabled(),
        lambda: {KEY_A: door()},
        route,
        lambda *_: source,
        Stt(),
        vad_factory=lambda: FakeVad([1.0] * 40 + [0.0] * END_SILENCE_FRAMES),
        segmenter_factory=PcmSegmenter,
    )
    states = []
    runtime.add_listener(lambda: states.append(runtime.is_on_for(KEY_A)))
    try:
        await runtime.async_start()
        await asyncio.wait_for(stt_started.wait(), 2)
        await wait_until(lambda: not source.reader_attached)
        assert child.returncode is None
        assert source._process is child
        assert not runtime.available_for(KEY_A)
        assert not runtime.is_on_for(KEY_A)
        release_stt.set()
        await wait_until(lambda: runtime.in_flight_count == 0)
        assert not any(states)
        assert await asyncio.wait_for(child.wait(), 3) == -9
        await wait_until(lambda: source._process is None)
        assert not any(states)
    finally:
        release_stt.set()
        await asyncio.wait_for(runtime.async_stop(), 6)
    assert child.returncode == -9
    assert runtime.worker_count == runtime.in_flight_count == runtime.timer_count == 0
