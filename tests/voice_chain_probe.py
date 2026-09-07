"""Local synthetic audio -> real decoder/VAD/STT client -> native HA smoke.

Run from the repository root (FFmpeg with libflite, or espeak[-ng], is required):

    PYTHONPATH=. uv run --no-project --python 3.14.5 \
      --with homeassistant==2026.8.1 --with PyTurboJPEG==1.8.3 \
      --with av==17.0.1 --with numpy==2.3.2 --with pymicro-vad==1.0.1 \
      --with argon2-cffi==25.1.0 --with httpx==0.28.1 \
      python tests/voice_chain_probe.py --report /tmp/voice-chain-report.json

Only the source boundary and STT *response* are fixtures. Local espeak speech is
preferred; FFmpeg's offline flite/slt voice is the fallback. Seeded quiet white
noise is a negative fixture, not a representative acoustic/VAD quality benchmark.
Real FFmpeg decodes a temporary 44.1 kHz stereo WAV to production 16 kHz mono PCM16.
Its bounded stdout is delivered at 10 ms monotonic deadlines. No RTSP server or
production FfmpegPcmSource is exercised: its loopback-only URL contract is not
weakened. The manager's injected source maps one synthetic opaque route to a file.

The localhost aiohttp fixture checks the actual multipart WAV against real
segmenter output, then returns SCRIPTED text, including a deliberately held last
response. This proves transport, exact matching, HA state and cleanup, NOT STT
recognition accuracy. No provider client, physical action, microphone/user audio,
remote TTS/STT, MQTT or Frigate is constructed. Temporary audio/HA files are removed;
only aggregate timings/counters/hashes can be written to the requested report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import platform as host_platform
import random
import resource
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from collections import Counter
from datetime import timedelta
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType

import aiohttp
import psutil
from aiohttp import web
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.ufanet_intercom.binary_sensor import UfanetCodePhraseBinarySensor
from custom_components.ufanet_intercom.const import DOMAIN, DiscoveredDoor
from custom_components.ufanet_intercom.voice_audio import (
    FFMPEG_AUDIO_ARGUMENTS,
    FRAME_BYTES,
    FRAME_DURATION_MS,
    MAX_SEGMENT_FRAMES,
    PRE_ROLL_FRAMES,
    MicroVadAdapter,
    PcmSegmenter,
)
from custom_components.ufanet_intercom.voice_phrase import (
    PhraseMatcher,
    encode_phrase_set,
)
from custom_components.ufanet_intercom.voice_runtime import (
    VoicePhraseManager,
    VoiceRuntimeConfig,
    VoiceTargetConfig,
)
from custom_components.ufanet_intercom.voice_stt import (
    MAX_PCM_BYTES,
    SttClient,
    SttConfig,
)

TARGET_PHRASE = "orange garden"
SYNTHETIC_SPEECH = "Synthetic visitor is speaking the orange garden phrase"
INPUT_RATE = 44_100
INPUT_CHANNELS = 2
MAX_SOURCE_SECONDS = 65
FRAME_SECONDS = FRAME_DURATION_MS / 1000


def run_command(*command: str) -> bytes:
    """Bound every local fixture-generation subprocess, without a shell."""
    return subprocess.run(command, check=True, capture_output=True, timeout=30).stdout


def make_audio(directory: Path, ffmpeg: str) -> tuple[Path, dict]:
    """Produce speech locally, and write a finite stereo resampling fixture."""
    speech_path = directory / "synthetic-speech.wav"
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    if espeak:
        raw_path = directory / "espeak.wav"
        run_command(
            espeak, "-v", "en", "-s", "150", "-w", str(raw_path), SYNTHETIC_SPEECH
        )
        input_args = ("-i", str(raw_path))
        engine = "local espeak English voice (synthetic, not user audio)"
    else:
        input_args = ("-f", "lavfi", "-i", f"flite=text='{SYNTHETIC_SPEECH}':voice=slt")
        engine = "local FFmpeg libflite/slt English voice (offline fallback)"
    run_command(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *input_args,
        "-t",
        "6",
        "-ar",
        str(INPUT_RATE),
        "-ac",
        str(INPUT_CHANNELS),
        "-c:a",
        "pcm_s16le",
        str(speech_path),
    )
    with wave.open(str(speech_path), "rb") as reader:
        assert reader.getparams()[:3] == (INPUT_CHANNELS, 2, INPUT_RATE)
        speech = reader.readframes(reader.getnframes())
    sample_frames = len(speech) // (2 * INPUT_CHANNELS)
    speech_seconds = sample_frames / INPUT_RATE
    assert 1 < speech_seconds <= 6, "local speech generation failed"

    rng = random.Random(49017)
    # +/- 100 PCM16 units: this is ONLY a quiet white-noise negative control.
    noise = b"".join(
        struct.pack("<hh", value, value)
        for value in (rng.randint(-100, 100) for _ in range(INPUT_RATE * 2))
    )
    parts = [bytes(INPUT_RATE * 4 * 2), noise, bytes(INPUT_RATE * 4 * 2)]
    cursor = 6.0
    windows = []
    for label in ("scripted_nonmatch", "scripted_match", "held_response_stop"):
        windows.append(
            {"label": label, "start_s": cursor, "end_s": cursor + speech_seconds}
        )
        parts.extend((speech, bytes(INPUT_RATE * 4 * 7)))
        cursor += speech_seconds + 7
    parts.append(bytes(INPUT_RATE * 4 * 15))
    timeline = b"".join(parts)
    duration = len(timeline) / (INPUT_RATE * 4)
    assert duration <= MAX_SOURCE_SECONDS
    source = directory / "synthetic-timeline.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(INPUT_CHANNELS)
        writer.setsampwidth(2)
        writer.setframerate(INPUT_RATE)
        writer.writeframes(timeline)
    return source, {
        "engine": engine,
        "input_rate_hz": INPUT_RATE,
        "input_channels": INPUT_CHANNELS,
        "duration_s": duration,
        "speech_duration_s": speech_seconds,
        "noise_window_s": [2, 4],
        "noise_peak_pcm16": 100,
        "speech_windows": windows,
    }


class LocalDecodedSource:
    """TEST ONLY: finite file input and real FFmpeg, not production RTSP source."""

    def __init__(self, binary: str, path: Path) -> None:
        self.binary, self.path = binary, path
        self.process = None
        self.frames = 0
        self.started_at = 0.0
        self.last_frame_at = 0.0
        self.max_lateness_s = 0.0
        self.closed = False

    async def async_start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            self.binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(self.path),
            *FFMPEG_AUDIO_ARGUMENTS,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=FRAME_BYTES * 4,
        )
        self.started_at = time.monotonic()

    async def async_read_frame(self) -> bytes | None:
        assert self.process is not None and self.process.stdout is not None
        assert self.frames < MAX_SOURCE_SECONDS / FRAME_SECONDS, "fixture time bound"
        try:
            frame = await self.process.stdout.readexactly(FRAME_BYTES)
        except asyncio.IncompleteReadError:
            return None
        deadline = self.started_at + (self.frames + 1) * FRAME_SECONDS
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        self.last_frame_at = time.monotonic()
        self.max_lateness_s = max(self.max_lateness_s, self.last_frame_at - deadline)
        self.frames += 1
        return frame

    async def async_close(self) -> None:
        if self.closed:
            return
        process = self.process
        if process is not None:
            if process.returncode is None:
                process.terminate()
            # Drain bounded stdout after the manager stops consuming it; otherwise
            # a full StreamReader can keep subprocess.wait() waiting on pipe close.
            try:
                await asyncio.wait_for(process.communicate(), timeout=2)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.communicate(), timeout=2)
            assert process.returncode is not None
        self.closed = True


class Coordinator:
    """No-I/O coordinator seam; real CoordinatorEntity subscription contract."""

    def __init__(self, entry: ConfigEntry, targets: list[DiscoveredDoor]) -> None:
        self.config_entry = entry
        self.data = MappingProxyType({target.key: target for target in targets})
        self.last_update_success = True
        self.last_exception = None
        self.listeners = []

    def async_add_listener(self, listener, context=None):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def async_request_refresh(self) -> None:
        raise AssertionError("no provider refresh belongs in this probe")


async def until(predicate, label: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out: {label}"
        await asyncio.sleep(0.01)


def usage() -> dict:
    current = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "process_cpu_s": current.ru_utime + current.ru_stime,
        "reaped_children_cpu_s": children.ru_utime + children.ru_stime,
        "process_lifetime_peak_rss_kib": current.ru_maxrss,
    }


async def probe(
    directory: Path, source_path: Path, fixture: dict, report: dict
) -> None:
    assert version("homeassistant") == "2026.8.1", "requires exact HA 2026.8.1"
    loop = asyncio.get_running_loop()
    baseline_tasks = set(asyncio.all_tasks())
    loop_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: loop_errors.append(context["message"])
    )
    started = time.monotonic()

    def relative():
        return time.monotonic() - started

    sources = []
    segments = []
    receipts = []
    matches = []
    events = []
    vad_counts = Counter()
    bounds = {"pre_roll_frames": 0, "active_frames": 0}
    held_release = asyncio.Event()
    held_finished = asyncio.Event()
    errors = []
    monitors = []
    resources = []
    target = DiscoveredDoor(
        key="a" * 64,
        binding="b" * 64,
        shared_id=40_001,
        door=0,
        model=21,
        display_name="Synthetic audio probe",
        trusted=True,
        openable=False,
        cctv_number="SYNTHETIC-NO-PROVIDER",
    )
    witness = DiscoveredDoor(
        key="d" * 64,
        binding="e" * 64,
        shared_id=40_002,
        door=0,
        model=21,
        display_name="Synthetic isolation witness",
        trusted=True,
        openable=False,
    )

    class ObservedVad(MicroVadAdapter):
        def process(self, frame):
            result = super().process(frame)
            vad_counts["frames"] += 1
            vad_counts[
                "warmup"
                if result is None
                else "voiced"
                if result >= 0.5
                else "unvoiced"
            ] += 1
            return result

    class ObservedSegmenter(PcmSegmenter):
        def process(self, frame, speech_probability):
            pcm = super().process(frame, speech_probability)
            bounds["pre_roll_frames"] = max(
                bounds["pre_roll_frames"], self.pre_roll_frame_count
            )
            bounds["active_frames"] = max(
                bounds["active_frames"], self.active_frame_count
            )
            if pcm is not None:
                source = sources[-1]
                segments.append(
                    {
                        "emitted_s": relative(),
                        "pcm_bytes": len(pcm),
                        "sha256": hashlib.sha256(pcm).hexdigest(),
                        "duration_s": len(pcm) / 32_000,
                        "feed_position_s": source.frames * FRAME_SECONDS,
                    }
                )
            return pcm

    def source_factory(binary, route):
        assert route == f"rtsp://127.0.0.1:9/{target.key}"
        assert not sources, "unexpected decoder restart"
        source = LocalDecodedSource(binary, source_path)
        sources.append(source)
        return source

    async def fake_stt(request):
        # Append before parsing so even malformed/extra requests are actual receipts.
        row = {"received_s": relative(), "fixture": "SCRIPTED TEXT, NO RECOGNITION"}
        receipts.append(row)
        index = len(receipts) - 1
        try:
            assert request.remote == "127.0.0.1" and request.method == "POST"
            assert index < 3, "unexpected actual HTTP receipt"
            assert request.headers.get("Authorization") is None
            form = await request.multipart()
            fields = {}
            async for field in form:
                assert field.name not in fields
                data = bytes(await field.read())
                assert len(data) <= MAX_PCM_BYTES + 44
                fields[field.name] = data
                if field.name == "file":
                    assert field.filename == "audio.wav"
                    assert field.headers["Content-Type"] == "audio/wav"
            assert set(fields) == {"file", "language", "model"}
            assert (
                fields["language"] == b"ru" and fields["model"] == b"synthetic-fixture"
            )
            with wave.open(io.BytesIO(fields["file"]), "rb") as reader:
                assert reader.getparams()[:3] == (1, 2, 16_000)
                pcm = reader.readframes(reader.getnframes())
            assert 0 < len(pcm) <= MAX_PCM_BYTES
            digest = hashlib.sha256(pcm).hexdigest()
            assert digest == segments[index]["sha256"], (
                "POST differs from real VAD segment"
            )
            row.update(wav_bytes=len(fields["file"]), pcm_bytes=len(pcm), sha256=digest)
            if index == 2:
                row["held"] = True
                await held_release.wait()
            row["response_s"] = relative()
            return web.json_response(
                {"text": "please orange garden" if index == 0 else TARGET_PHRASE}
            )
        except Exception as error:
            errors.append(str(error) or type(error).__name__)
            raise
        finally:
            if index == 2:
                held_finished.set()

    app = web.Application(client_max_size=MAX_PCM_BYTES + 8192)
    app.router.add_post("/v1/audio/transcriptions", fake_stt)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    stt_config = SttConfig(
        f"http://127.0.0.1:{port}/v1/audio/transcriptions", model="synthetic-fixture"
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30), trust_env=False
    )
    connector = session.connector
    hass = HomeAssistant(str(directory / "ha"))
    Path(hass.config.config_dir).mkdir()
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    entry = ConfigEntry(
        data={},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options={},
        source="user",
        subentries_data=(),
        title="Synthetic voice chain",
        unique_id="c" * 64,
        version=1,
    )
    # Native HA registries, but deliberately no integration/provider setup.
    hass.config_entries._entries[entry.entry_id] = entry
    coordinator = Coordinator(entry, [target, witness])
    matcher = await hass.async_add_executor_job(
        lambda: PhraseMatcher.from_stored(encode_phrase_set([TARGET_PHRASE]))
    )

    async def executor(function, value):
        before = relative()
        result = await hass.async_add_executor_job(function, value)
        matches.append(
            {"started_s": before, "finished_s": relative(), "matched": result}
        )
        return result

    manager = VoicePhraseManager(
        VoiceRuntimeConfig(
            True, stt_config, (VoiceTargetConfig(target.key, target.binding, matcher),)
        ),
        snapshot_provider=lambda: coordinator.data,
        stream_url_provider=lambda key: (
            f"rtsp://127.0.0.1:9/{key}" if key == target.key else None
        ),
        ffmpeg_binary=shutil.which("ffmpeg"),
        stt_client=SttClient(session, stt_config),
        source_factory=source_factory,
        vad_factory=ObservedVad,
        segmenter_factory=ObservedSegmenter,
        async_executor=executor,
    )
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger("voice-chain-probe"),
        domain="binary_sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    entities = [
        UfanetCodePhraseBinarySensor(coordinator, manager, item)
        for item in (target, witness)
    ]
    entity, witness_entity = entities
    registry = er.async_get(hass)

    @callback
    def state_changed(event):
        if event.data["entity_id"] in {item.entity_id for item in entities}:
            state = event.data["new_state"]
            events.append(
                {
                    "at_s": relative(),
                    "entity_id": event.data["entity_id"],
                    "state": state.state if state else None,
                }
            )

    unsubscribe = hass.bus.async_listen("state_changed", state_changed)
    process = psutil.Process()

    async def monitor():
        while True:
            children = process.children()
            resources.append(
                {
                    "rss_kib": process.memory_info().rss / 1024,
                    "child_rss_kib": sum(child.memory_info().rss for child in children)
                    / 1024,
                    "fds": process.num_fds(),
                    "threads": process.num_threads(),
                    "tasks": len(asyncio.all_tasks()),
                }
            )
            await asyncio.sleep(0.1)

    before_usage = usage()
    pipeline_started = time.monotonic()
    try:
        for item, expected_target in zip(entities, (target, witness), strict=True):
            await platform._async_add_entity(item, False, registry, None)
            registered = registry.async_get(item.entity_id)
            assert registered.unique_id == f"{expected_target.key}_code_phrase"
            device = dr.async_get(hass).async_get(registered.device_id)
            assert (DOMAIN, expected_target.key) in device.identifiers
            assert item.extra_state_attributes is None
        assert (
            registry.async_get(entity.entity_id).device_id
            != registry.async_get(witness_entity.entity_id).device_id
        )
        monitors.append(asyncio.create_task(monitor()))
        await manager.async_start()
        await until(
            lambda: sources and sources[0].frames >= 550,
            "noise and silence delivered",
            10,
        )
        assert not receipts and not segments and manager.on_count == 0
        assert hass.states.get(entity.entity_id).state == "off"
        report["noise_no_post_no_pulse"] = True
        await until(lambda: len(matches) >= 1, "nonmatch processed")
        assert matches[0]["matched"] is False
        assert (
            hass.states.get(entity.entity_id).state == "off" and manager.on_count == 0
        )
        report["substring_nonmatch_no_pulse"] = True
        await until(
            lambda: hass.states.get(entity.entity_id).state == "on", "exact match HA ON"
        )
        assert len(receipts) == 2 and [row["matched"] for row in matches] == [
            False,
            True,
        ]
        assert manager.timer_count == 1
        await until(
            lambda: hass.states.get(entity.entity_id).state == "off",
            "natural HA OFF after 5s",
            7,
        )
        own_events = [row for row in events if row["entity_id"] == entity.entity_id]
        on_event = next(row for row in own_events if row["state"] == "on")
        off_event = next(
            row
            for row in own_events
            if row["state"] == "off" and row["at_s"] > on_event["at_s"]
        )
        pulse_s = off_event["at_s"] - on_event["at_s"]
        assert 4.9 <= pulse_s <= 5.5, f"natural pulse duration: {pulse_s}"
        await until(
            lambda: len(receipts) == 3 and receipts[2].get("held"),
            "real in-flight POST",
        )
        assert manager.in_flight_count == 1
        assert sources[0].process.returncode is None, "stop must exercise a live FFmpeg"
        frames_before = sources[0].frames
        await asyncio.sleep(0.3)
        assert sources[0].frames > frames_before, (
            "audio must continue while HTTP is held"
        )
        stop_started = time.monotonic()
        await asyncio.wait_for(manager.async_stop(), timeout=5)
        stop_seconds = time.monotonic() - stop_started
        assert (
            manager.worker_count,
            manager.in_flight_count,
            manager.timer_count,
            manager.on_count,
        ) == (0, 0, 0, 0)
        assert sources[0].closed and sources[0].process.returncode is not None
        assert hass.states.get(entity.entity_id).state == "unavailable"
        held_release.set()
        await asyncio.wait_for(held_finished.wait(), timeout=2)
        await asyncio.sleep(0.1)
        assert len(matches) == 2 and manager.on_count == 0, "late result after stop"
        assert not any(
            row["entity_id"] == witness_entity.entity_id and row["state"] == "on"
            for row in events
        )
        assert sum(row["state"] == "on" for row in events) == 1
        assert len(segments) == len(receipts) == 3 and len(sources) == 1
        assert not errors
        assert bounds["pre_roll_frames"] <= PRE_ROLL_FRAMES
        assert bounds["active_frames"] <= MAX_SEGMENT_FRAMES
        # Disabled config is a separate manager, as in disable/reload: no calls to
        # any infrastructure/factories. Options Flow preservation is NOT tested.
        disabled_calls = []

        def forbidden(*args):
            disabled_calls.append(True)
            raise AssertionError("disabled runtime must allocate nothing")

        disabled = VoicePhraseManager(
            VoiceRuntimeConfig(False),
            snapshot_provider=forbidden,
            stream_url_provider=forbidden,
            source_factory=forbidden,
            vad_factory=forbidden,
            segmenter_factory=forbidden,
        )
        await disabled.async_start()
        disabled.reconcile()
        await asyncio.sleep(0.1)
        await disabled.async_stop()
        assert not disabled_calls and disabled.worker_count == 0
        source = sources[0]
        report.update(
            {
                "pulse_s": pulse_s,
                "stop_inflight_s": stop_seconds,
                "actual_post_receipts": len(receipts),
                "segments": segments,
                "http_receipts": receipts,
                "matcher_results": matches,
                "ha_state_events": events,
                "vad": dict(vad_counts),
                "observed_segmenter_bounds": bounds,
                "disabled_zero_factory_calls": True,
                "exact_target_isolation": "unconfigured sibling entity never pulsed",
                "stopped_request_no_late_pulse": True,
                "feed": {
                    "frames": source.frames,
                    "audio_s": source.frames * FRAME_SECONDS,
                    "wall_s": source.last_frame_at - source.started_at,
                    "max_deadline_lateness_s": source.max_lateness_s,
                },
                "latencies_s": {
                    "actual_receipt_intervals": [
                        right["received_s"] - left["received_s"]
                        for left, right in pairwise(receipts)
                    ],
                    "segment_to_http_receipt": [
                        row["received_s"] - segment["emitted_s"]
                        for row, segment in zip(receipts, segments, strict=True)
                    ],
                    "http_response_to_ha_on": on_event["at_s"]
                    - receipts[1]["response_s"],
                    "generated_speech_wav_end_to_segment": [
                        segment["feed_position_s"] - window["end_s"]
                        for segment, window in zip(
                            segments, fixture["speech_windows"], strict=True
                        )
                    ],
                    "generated_speech_wav_end_to_ha_on": on_event["at_s"]
                    - (
                        source.started_at
                        - started
                        + fixture["speech_windows"][1]["end_s"]
                    ),
                },
            }
        )
    finally:
        await asyncio.wait_for(manager.async_stop(), timeout=5)
        held_release.set()
        await session.close()
        await runner.cleanup()
        for item in entities:
            if item.hass is not None:
                await item.async_remove()
        unsubscribe()
        await platform.async_reset()
        await hass.async_stop(force=True)
        for task in monitors:
            task.cancel()
        outcomes = await asyncio.gather(*monitors, return_exceptions=True)
        assert all(
            isinstance(outcome, asyncio.CancelledError) for outcome in outcomes
        ), outcomes
        await asyncio.sleep(0.1)
        loop.set_exception_handler(previous_handler)
    after_usage = usage()
    wall_s = time.monotonic() - pipeline_started
    resource_delta = {
        key: after_usage[key] - before_usage[key]
        for key in ("process_cpu_s", "reaped_children_cpu_s")
    }
    report["resources"] = {
        "scope": "whole HA probe process plus FFmpeg; fixture generation/imports excluded from CPU delta, not RSS; not weak-host capacity proof",
        "wall_s": wall_s,
        **resource_delta,
        "cpu_percent_one_core_equivalent": 100 * sum(resource_delta.values()) / wall_s,
        "sample_interval_s": 0.1,
        "sampled_peaks": {
            key: max(row[key] for row in resources) for key in resources[0]
        },
        "process_lifetime_peak_rss_kib": after_usage["process_lifetime_peak_rss_kib"],
    }
    remaining = [
        task for task in asyncio.all_tasks() - baseline_tasks if not task.done()
    ]
    assert not remaining, (
        f"pending tasks after cleanup: {[task.get_coro().__qualname__ for task in remaining]}"
    )
    assert not coordinator.listeners and not manager._listeners
    assert session.closed and connector.closed
    assert not runner.sites
    assert all(
        source.closed and source.process.returncode is not None for source in sources
    )
    assert not loop_errors, loop_errors
    report["cleanup"] = {
        "pending_new_tasks": len(remaining),
        "session_closed": session.closed,
        "workers": manager.worker_count,
        "stt_tasks": manager.in_flight_count,
        "pulse_timers": manager.timer_count,
        "ffmpeg_processes_started": len(sources),
        "ffmpeg_returncodes": [source.process.returncode for source in sources],
        "connector_closed": connector.closed,
        "ffmpeg_reaped": True,
        "coordinator_and_manager_listeners": 0,
        "http_sites": len(runner.sites),
        "loop_errors": loop_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional aggregate JSON evidence outside repository",
    )
    arguments = parser.parse_args()
    assert __debug__, "assertions must not be disabled"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "FFmpeg is required"
    report = {
        "status": "FAIL",
        "scope": "SYNTHETIC FILE SOURCE / SCRIPTED STT RESPONSE; not RTSP, provider audio or recognition accuracy",
        "git_head": run_command("git", "rev-parse", "HEAD").decode().strip(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": host_platform.python_version(),
        "host": host_platform.platform(),
        "versions": {
            name: version(name)
            for name in ("homeassistant", "pymicro-vad", "aiohttp", "argon2-cffi")
        },
        "ffmpeg": run_command(ffmpeg, "-version").decode().splitlines()[0],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="ufanet-voice-chain-") as directory:
            source, fixture = make_audio(Path(directory), ffmpeg)
            report["fixture"] = fixture
            asyncio.run(probe(Path(directory), source, fixture, report))
        report["temporary_audio_and_ha_directory_removed"] = not Path(
            directory
        ).exists()
        assert report["temporary_audio_and_ha_directory_removed"]
        report["status"] = "PASS"
    except BaseException as error:
        report["failure"] = str(error) or type(error).__name__
        raise
    finally:
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if arguments.report:
            arguments.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)


if __name__ == "__main__":
    main()
