"""Finite synthetic Russian speech acceptance; never connects to HA/camera.

See controlled_vad_probe.md. Generation is offline after official downloads.
Only `evaluate --send-owner-stt` sends the saved artificial PCM to an explicitly
provided owner GigaAM endpoint, sequentially, without retries. HTTP is diagnostic-only;
production SttClient and all segmentation parameters remain unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
from array import array
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import types
import unicodedata
from urllib.parse import urlsplit
import wave

import aiohttp

APPROVED_SHA = "32a9a3a0598ce22002b5b8d0d6e571c9aa3b62f2"
REPO = Path(__file__).resolve().parents[1]
MODEL = "gigaam-v3-e2e-rnnt"

RATE = 16000
FRAME = 160
MAX_SAMPLES = 24
MAX_REQUESTS = 80
REFERENCES = (
    ("Добрый день.", "Я принёс посылку."),
    ("Синий велосипед.", "Стоит возле подъезда."),
    ("Пожалуйста, подождите.", "Я сейчас спущусь."),
    ("Сегодня тёплая погода.", "Можно гулять во дворе."),
    ("Вечером мы вернёмся.", "Примерно через час."),
    ("Спасибо за помощь.", "Всего вам доброго."),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def append_json(path, data):
    with path.open("a") as stream:
        stream.write(json.dumps(data, ensure_ascii=False) + "\n")
        stream.flush()


def pcm_values(data):
    values = array("h", data)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def pcm_bytes(values):
    values = array("h", values)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(pcm)


def read_wav(path):
    with wave.open(str(path), "rb") as reader:
        assert reader.getparams()[:3] == (1, 2, RATE)
        return reader.readframes(reader.getnframes())


def words(text):
    return re.findall(
        r"\w+", unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    )


def word_errors(reference, hypothesis):
    """Standard ordered word Levenshtein distance; WER may exceed one."""
    reference, hypothesis = words(reference), words(hypothesis)
    row = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, 1):
        new = [i]
        for j, hyp in enumerate(hypothesis, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (ref != hyp)))
        row = new
    return {
        "errors": row[-1],
        "reference_words": len(reference),
        "wer": row[-1] / max(1, len(reference)),
    }


def run_command(*args, input=None):
    return subprocess.run(
        args, input=input, capture_output=True, check=True, timeout=60
    ).stdout


def generate(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    assert not (out / "manifest.json").exists(), "use a fresh corpus directory"
    manifest = {
        "synthetic_only": True,
        "tts": "piper-tts==1.8.0; local ru_RU-irina-medium; CPU",
        "model_sha256": digest(args.model.read_bytes()),
        "model_config_sha256": digest(Path(str(args.model) + ".json").read_bytes()),
        "model_path": str(args.model),
        "tts_options": {"noise_scale": 0, "noise_w_scale": 0, "length_scale": 1},
        "dry_chunk_peak_pcm16": 12000,
        "ffmpeg": run_command("ffmpeg", "-version").decode().splitlines()[0],
        "format": "16000 Hz mono signed PCM16LE; exact 10 ms frames",
        "lead_s": 1.2,
        "tail_s": 1.3,
        "noise": "seeded Gaussian, one-pole lowpass 3500 Hz, global dry-speech RMS SNR 20 dB; NOT recorded courtyard noise",
        "chunk_support": "complete TTS chunk spans; acoustic support derived from dry PCM energy, NOT human word alignment",
        "samples": [],
    }
    for number, chunks in enumerate(REFERENCES, 1):
        synthesized = []
        for chunk_index, text in enumerate(chunks):
            source = out / f"source-{number}-{chunk_index}.wav"
            run_command(
                str(args.piper_python),
                "-m",
                "piper",
                "-m",
                str(args.model),
                "--noise-scale",
                "0",
                "--noise-w-scale",
                "0",
                "--length-scale",
                "1",
                "-f",
                str(source),
                input=(text + "\n").encode(),
            )
            raw = run_command(
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-ar",
                str(RATE),
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            )
            # Pad, never trim speech. The identical generated chunk is reused.
            raw += bytes((-len(raw)) % (FRAME * 2))
            values = pcm_values(raw)
            # Leave fixed headroom before noise; never clip or alter one A/B lane.
            gain = 12000 / max(abs(v) for v in values)
            synthesized.append(array("h", (round(v * gain) for v in values)))
        for variant, gap in (("clean", 0.15), ("noise_pause", 0.90)):
            values = array("h", [0] * int(1.2 * RATE))
            spans = []
            for chunk_index, (text, chunk) in enumerate(
                zip(chunks, synthesized, strict=True)
            ):
                start = len(values)
                values.extend(chunk)
                spans.append(
                    {"text": text, "start_sample": start, "end_sample": len(values)}
                )
                if chunk_index == 0:
                    values.extend([0] * int(gap * RATE))
            values.extend([0] * int(1.3 * RATE))
            assert len(values) <= 8 * RATE, (
                "reference exceeds production bounded-WAV contract"
            )
            dry = pcm_bytes(values)
            rms = math.sqrt(
                sum(v * v for chunk in synthesized for v in chunk)
                / sum(map(len, synthesized))
            )
            clipped = 0
            if variant == "noise_pause":
                rng = random.Random(73100 + number)
                alpha = 1 - math.exp(-2 * math.pi * 3500 / RATE)
                previous = 0.0
                noise = []
                for _ in values:
                    previous += alpha * (rng.gauss(0, 1) - previous)
                    noise.append(previous)
                scale = rms / 10 / math.sqrt(sum(v * v for v in noise) / len(noise))
                mixed = []
                for value, noise_value in zip(values, noise, strict=True):
                    value = round(value + noise_value * scale)
                    clipped += not -32768 <= value <= 32767
                    mixed.append(max(-32768, min(32767, value)))
                values = array("h", mixed)
            assert clipped == 0
            sample_id = f"ru{number:02d}-{variant}"
            pcm = pcm_bytes(values)
            write_wav(out / f"{sample_id}.wav", pcm)
            write_wav(out / f"{sample_id}-dry.wav", dry)
            manifest["samples"].append(
                {
                    "id": sample_id,
                    "variant": variant,
                    "reference": " ".join(chunks),
                    "wav": f"{sample_id}.wav",
                    "dry_wav": f"{sample_id}-dry.wav",
                    "pcm_sha256": digest(pcm),
                    "dry_pcm_sha256": digest(dry),
                    "duration_s": len(values) / RATE,
                    "pause_s": gap,
                    "noise_seed": 73100 + number if variant == "noise_pause" else None,
                    "clipped_samples": clipped,
                    "chunks": spans,
                }
            )
        save_json(out / "manifest.partial.json", manifest)
    assert len(manifest["samples"]) == 12 <= MAX_SAMPLES
    save_json(out / "manifest.json", manifest)
    print(
        json.dumps({"corpus": str(out), "samples": len(manifest["samples"])}),
        flush=True,
    )


def load_production():
    root = REPO / "custom_components/ufanet_intercom"
    hashes = {}
    for name in ("voice_audio", "voice_stt"):
        path = root / f"{name}.py"
        frozen = run_command(
            "git",
            "-C",
            str(REPO),
            "show",
            f"{APPROVED_SHA}:custom_components/ufanet_intercom/{name}.py",
        )
        assert path.read_bytes() == frozen, (
            "production source differs from frozen approved baseline"
        )
        hashes[name] = digest(frozen)
    package = types.ModuleType("controlled_ufanet")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    return (
        importlib.import_module(package.__name__ + ".voice_audio"),
        importlib.import_module(package.__name__ + ".voice_stt"),
        hashes,
    )


def segment(pcm, audio):
    detector, segmenter = audio.MicroVadAdapter(), audio.PcmSegmenter()
    clips, scores = [], []
    for end in range(audio.FRAME_BYTES, len(pcm) + 1, audio.FRAME_BYTES):
        frame = pcm[end - audio.FRAME_BYTES : end]
        probability = detector.process(frame)
        scores.append(probability)
        if probability is None:
            continue  # exact production warmup policy; prefix pays warmup
        clip = segmenter.process(frame, probability)
        if clip is not None:
            start = end - len(clip)
            assert pcm[start:end] == clip, (
                "emitted clip is not an exact contiguous source crop"
            )
            assert len(clip) <= audio.MAX_SEGMENT_FRAMES * audio.FRAME_BYTES
            clips.append(
                {
                    "start_sample": start // 2,
                    "end_sample": end // 2,
                    "pcm_sha256": digest(clip),
                    "pcm": clip,
                }
            )
    return clips, scores, segmenter.active_frame_count


def crop_coverage(dry, clips, chunks):
    """Measure lost original speech energy, ignoring injected noise."""
    values = pcm_values(dry)
    retained = bytearray(len(values))
    for clip in clips:
        start, end = clip["start_sample"], clip["end_sample"]
        retained[start:end] = bytes([1]) * (end - start)
    results = []
    for chunk in chunks:
        start, end = chunk["start_sample"], chunk["end_sample"]
        energy = sum(values[i] ** 2 for i in range(start, end))
        lost = sum(values[i] ** 2 for i in range(start, end) if not retained[i])
        frame_energies = [
            (i, sum(v * v for v in values[i : i + FRAME]))
            for i in range(start, end, FRAME)
        ]
        threshold = (
            max(e for _, e in frame_energies) * 0.001
        )  # -30 dB relative to peak frame energy
        active = [i for i, energy in frame_energies if energy > threshold]
        missing = [i for i in active if not all(retained[i : i + FRAME])]
        results.append(
            {
                **chunk,
                "lost_energy_fraction": lost / max(1, energy),
                "active_frames": len(active),
                "missing_active_frames": len(missing),
                "active_start_sample": active[0],
                "active_end_sample": active[-1] + FRAME,
                "missing_active_frame_starts": missing,
            }
        )
    return results


class DiagnosticStt:
    """Explicit test-only bearer-over-HTTP adapter, never production SttClient."""

    def __init__(self, session, token, encoder, output, endpoint):
        self.endpoint = endpoint
        self.session, self.token, self.encoder, self.output = (
            session,
            token,
            encoder,
            output,
        )
        self.requests = 0

    async def transcribe(self, pcm, sample_id, lane):
        assert self.requests < MAX_REQUESTS
        self.requests += 1
        row = {
            "request": self.requests,
            "sample": sample_id,
            "lane": lane,
            "pcm_sha256": digest(pcm),
            "pcm_seconds": len(pcm) / 32000,
        }
        started = time.monotonic()
        data = aiohttp.FormData()
        data.add_field(
            "file",
            self.encoder(pcm),
            filename="synthetic.wav",
            content_type="audio/wav",
        )
        data.add_field("model", MODEL)
        data.add_field("language", "ru")
        data.add_field("response_format", "json")
        try:
            async with self.session.post(
                self.endpoint,
                data=data,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                auto_decompress=False,
            ) as response:
                row["http_status"] = response.status
                if response.status != 200:
                    raise RuntimeError("diagnostic STT failed")
                body = bytearray()
                async for part in response.content.iter_chunked(4096):
                    body.extend(part)
                    if len(body) > 65536:
                        raise ValueError("response too large")
                payload = json.loads(body)
                assert type(payload) is dict and type(payload.get("text")) is str
                assert len(payload["text"].encode()) <= 4096
                assert payload.get("model", MODEL) == MODEL
                row.update(
                    status="ok", text=payload["text"], model_echo=payload.get("model")
                )
        except Exception as error:
            row.update(status="error", error_class=type(error).__name__)
        row["elapsed_s"] = round(time.monotonic() - started, 4)
        append_json(self.output / "requests.jsonl", row)
        return row


async def evaluate(args):
    assert args.send_owner_stt, "explicit --send-owner-stt acknowledgement required"
    endpoint = urlsplit(args.endpoint)
    assert endpoint.scheme in ("http", "https") and endpoint.hostname
    assert not (
        endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
    )
    assert endpoint.scheme == "https" or args.allow_diagnostic_http
    args.output.mkdir(parents=True, exist_ok=True)
    assert not (args.output / "requests.jsonl").exists(), (
        "use a fresh results directory"
    )
    manifest = json.loads((args.corpus / "manifest.json").read_text())
    assert manifest["synthetic_only"] is True
    samples = manifest["samples"]
    assert 1 <= len(samples) <= MAX_SAMPLES
    assert len({s["id"] for s in samples}) == len(samples)
    audio, stt, hashes = load_production()
    planned = []
    for sample in samples:
        pcm, dry = (
            read_wav(args.corpus / sample["wav"]),
            read_wav(args.corpus / sample["dry_wav"]),
        )
        assert (
            digest(pcm) == sample["pcm_sha256"]
            and digest(dry) == sample["dry_pcm_sha256"]
        )
        assert (
            len(pcm) == len(dry) <= stt.MAX_PCM_BYTES
            and len(pcm) % audio.FRAME_BYTES == 0
        )
        clips, scores, pending = segment(pcm, audio)
        coverage = crop_coverage(dry, clips, sample["chunks"])
        save_json(
            args.output / (sample["id"] + "-boundaries.json"),
            {
                "sample": sample,
                "scores_10ms": scores,
                "pending_active_frames": pending,
                "clips": [{k: v for k, v in c.items() if k != "pcm"} for c in clips],
                "dry_speech_coverage": coverage,
                "exact_contiguous_pcm_crops_verified": True,
            },
        )
        for index, clip in enumerate(clips):
            write_wav(args.output / f"{sample['id']}-crop-{index}.wav", clip["pcm"])
        planned.append((sample, pcm, clips, coverage, pending))
    request_plan = sum(1 + len(clips) for _, _, clips, _, _ in planned)
    assert request_plan <= MAX_REQUESTS
    report = {
        "status": "running",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "approved_sha": APPROVED_SHA,
        "production_source_hashes": hashes,
        "corpus_manifest_sha256": digest((args.corpus / "manifest.json").read_bytes()),
        "samples_planned": len(samples),
        "requests_planned": request_plan,
        "limits": {
            "samples": MAX_SAMPLES,
            "requests": MAX_REQUESTS,
            "concurrency": 1,
            "request_timeout_s": 15,
            "evaluate_timeout_s": 600,
            "pcm_seconds": 8,
            "retries": 0,
        },
        "transport": f"explicit diagnostic bearer-over-{endpoint.scheme.upper()} adapter; NOT production SttClient acceptance",
        "endpoint": args.endpoint,
        "model_requested": MODEL,
        "versions": {p: version(p) for p in ("pymicro-vad", "aiohttp")},
        "scope": "local artificial TTS only; no HA, real camera audio, network settings, entity or physical calls",
        "baseline_gate": "full-WAV ordered reference WER <= 0.20; otherwise synthetic TTS/ASR baseline mismatch, not proved VAD failure",
        "interpretation": "Boundary loss is direct dry-PCM evidence. ASR differences without missing active frames are ASR context sensitivity, not proved clipping. No real-courtyard accuracy claim.",
    }
    save_json(args.output / "summary.json", report)
    started = time.monotonic()
    token = args.token_file.read_text().strip()
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15),
        connector=aiohttp.TCPConnector(limit=1),
        trust_env=False,
        middlewares=(),
    ) as session:
        session._retry_connection = False
        client = DiagnosticStt(
            session, token, stt.pcm_to_wav, args.output, args.endpoint
        )
        async with asyncio.timeout(600):
            for sample, pcm, clips, coverage, pending in planned:
                baseline = await client.transcribe(pcm, sample["id"], "full")
                gated = []
                for index, clip in enumerate(clips):
                    gated.append(
                        await client.transcribe(
                            clip["pcm"], sample["id"], f"crop-{index}"
                        )
                    )
                row = {
                    "id": sample["id"],
                    "variant": sample["variant"],
                    "reference": sample["reference"],
                    "baseline": baseline,
                    "gated": gated,
                    "coverage": coverage,
                    "pending_active_frames": pending,
                    "segments": len(clips),
                }
                if any(r["status"] != "ok" for r in [baseline, *gated]):
                    row["classification"] = "transport_error_unscorable"
                else:
                    joined = " ".join(r["text"] for r in gated)
                    row["full_wer"] = word_errors(sample["reference"], baseline["text"])
                    row["gated_wer"] = word_errors(sample["reference"], joined)
                    row["gated_joined"] = joined
                    row["baseline_eligible"] = row["full_wer"]["wer"] <= 0.2
                    missing = sum(c["missing_active_frames"] for c in coverage)
                    if not row["baseline_eligible"]:
                        kind = "synthetic_baseline_mismatch_not_vad_verdict"
                    elif not clips:
                        kind = "eligible_speech_without_vad_clip"
                    elif missing:
                        kind = "eligible_with_acoustic_boundary_loss_inspect"
                    elif row["gated_wer"]["errors"] > row["full_wer"]["errors"]:
                        kind = "asr_context_difference_without_active_speech_loss"
                    else:
                        kind = "eligible_preserved"
                    row["classification"] = kind
                append_json(args.output / "results.jsonl", row)
                print(
                    json.dumps(
                        {
                            "id": row["id"],
                            "class": row["classification"],
                            "full": row.get("full_wer"),
                            "gated": row.get("gated_wer"),
                            "segments": len(clips),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        report["requests_executed"] = client.requests
    rows = [
        json.loads(line)
        for line in (args.output / "results.jsonl").read_text().splitlines()
    ]
    requests = [
        json.loads(line)
        for line in (args.output / "requests.jsonl").read_text().splitlines()
    ]
    assert len(rows) == len(samples)
    assert len(requests) == report["requests_executed"] == request_plan
    report.update(
        status="completed",
        samples_completed=len(rows),
        successful_requests=sum(r["status"] == "ok" for r in requests),
        classifications=dict(Counter(r["classification"] for r in rows)),
        elapsed_s=round(time.monotonic() - started, 3),
    )
    report["by_variant"] = {}
    for variant in sorted({r["variant"] for r in rows}):
        selected = [r for r in rows if r["variant"] == variant]
        report["by_variant"][variant] = {
            "samples": len(selected),
            "segments": sum(r["segments"] for r in selected),
            "baseline_eligible": sum(
                r.get("baseline_eligible", False) for r in selected
            ),
            "full_exact": sum(
                r.get("full_wer", {}).get("errors") == 0 for r in selected
            ),
            "gated_exact": sum(
                r.get("gated_wer", {}).get("errors") == 0 for r in selected
            ),
            "missing_active_frames": sum(
                c["missing_active_frames"] for r in selected for c in r["coverage"]
            ),
            "pending_active_frames": sum(r["pending_active_frames"] for r in selected),
        }
    save_json(args.output / "summary.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generation = commands.add_parser("generate")
    generation.add_argument("--piper-python", type=Path, required=True)
    generation.add_argument("--model", type=Path, required=True)
    generation.add_argument("--output", type=Path, required=True)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--corpus", type=Path, required=True)
    evaluation.add_argument("--output", type=Path, required=True)
    evaluation.add_argument("--endpoint", required=True)
    evaluation.add_argument("--token-file", type=Path, required=True)
    evaluation.add_argument("--allow-diagnostic-http", action="store_true")
    evaluation.add_argument("--send-owner-stt", action="store_true")
    args = parser.parse_args()
    if args.command == "generate":
        generate(args)
    else:
        asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
