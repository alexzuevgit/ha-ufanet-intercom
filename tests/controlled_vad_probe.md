# Known-text Russian VAD acceptance probe

`controlled_vad_probe.py` is an opt-in diagnostic, not a pytest network test and
not integration runtime code. It verifies frozen production `voice_audio.py` and
`voice_stt.py` against `32a9a3a0598ce22002b5b8d0d6e571c9aa3b62f2` before evaluation.
It never imports HA setup, opens a camera, publishes an entity or calls an action.

## Recorded outcome (historical 2.1.0b1 baseline)

Reproduce this historical probe from commit `eef1759229d15c996ec25d51bd2355f99c460fcb`; its source-freeze guard deliberately rejects later production policies.

The 2.1.0b2 policy retains 600 ms including the 200 ms start trigger and ends
an utterance after 1200 ms of VAD-negative frames. The 8-second clip cap remains.
For a continuous microphone experiment, continue feeding genuine quiet PCM
after the known speech: a file ending only 1.3 seconds later is insufficient
for this hold plus detector response. Never force a flush or count an unissued
active buffer as an STT request. EOF still revokes publication authority.

The initial bounded experiment used **12 artificial inputs / 27 STT requests**:
six references, each clean and with one seeded 20 dB SNR noise / longer-pause
variant. GigaAM echoed `gigaam-v3-e2e-rnnt` on every successful response.

| Check | Clean | Noise + pause |
| --- | ---: | ---: |
| Inputs | 6 | 6 |
| Full-WAV reference WER = 0 | 6 | 6 |
| Ordered joined-crop reference WER = 0 | 6 | 6 |
| Emitted crops | 6 | 9 |
| Full reference in a single crop response | 6 | 3 |
| Inputs with some omitted dry acoustic onset | 6 | 6 |

**Lexical gate passes on these synthetic references; acoustic-boundary gate does
not pass.** Original dry speech at chunk starts is omitted by 10–190 ms where
loss occurs. The largest omitted energy is 39.4344% of one first chunk, not 39%
of all corpus speech. Exact ASR output therefore must not be read as proof that
the PCM start was preserved.

All emitted clips were verified byte-for-byte as contiguous source crops. An
independent offline replay reproduced every detector score and crop hash. Initial
crop starts equal the configured 40-frame pre-roll ending at the 20-frame
sustained-speech trigger. This leaves 200 ms before the detected run, insufficient
for every acoustic onset here. No additional off-by-one, PCM corruption or
implementation error relative to those existing semantics was demonstrated.
No production code or parameters were changed. Changing onset policy needs its
own justified acceptance target; this probe is not a parameter sweep.

Three longer-pause cases split into two requests. Joining responses is useful for
ordered word accounting, **not** proof that one complete exact phrase reaches the
HA phrase matcher. Runtime pacing, dropped requests, HA entities, cold-start
speech, TLS and real-courtyard recognition are outside this probe.

The earlier incidental-speech observation had no retained real PCM or known text;
its STT disagreements remain candidates, not proven VAD failures.

## Reproduction

Use a separate TTS environment; do not install into Hermes or an HA environment.
Python 3.13.5, `pymicro-vad==1.0.1`, `aiohttp==3.14.3`, `piper-tts==1.8.0` were used.
The TTS environment resolved `onnxruntime==1.29.0` and `numpy==2.5.3`.
FFmpeg is needed only for offline source resampling.

The official model files are:

- `https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx`
- the same URL with `.json` appended
- `MODEL_CARD` in that directory

Verify downloaded bytes before reuse:

- ONNX SHA-256: `8ff38212d23da300bbe3705c645e6e5b9475f0bfde01558eb17813e22acaaaaa`
- JSON SHA-256: `c2ec28bb38e2b59e93b959b3e40348c1afebbd272f30fed5d41205d08e98a9d7`

The model card identifies the RHVoice dataset and lists its license as unknown;
model binaries and generated audio are not committed or redistributed here.
Official downloads are the only outbound activity during preparation; synthesis,
resampling and noise generation are local. TTS is not a natural human benchmark.

From the repository root, supply paths through environment variables:

```sh
# PY: existing read-only Python with aiohttp and pymicro-vad
# TTS_PY: separate Python with the pinned Piper runtime
# MODEL_PATH: downloaded .onnx with adjacent .onnx.json
# CORPUS and RESULTS: new private output directories
"$PY" -B tests/controlled_vad_probe.py generate \
  --piper-python "$TTS_PY" --model "$MODEL_PATH" --output "$CORPUS"

# Explicitly authorized owner's STT endpoint, not a public third-party service.
# TOKEN_FILE contains the token; its contents never appear in argv or output.
"$PY" -B tests/controlled_vad_probe.py evaluate \
  --corpus "$CORPUS" --output "$RESULTS" \
  --endpoint "$OWNER_STT_ENDPOINT" --token-file "$TOKEN_FILE" \
  --send-owner-stt
```

For an explicitly authorized existing internal HTTP diagnostic endpoint, also
pass `--allow-diagnostic-http`. The recorded experiment used that diagnostic
HTTP transport, not production `SttClient`; it proves neither TLS nor HA
transport. HTTPS uses normal certificate validation, never an insecure SSL
setting. No network/server/HA configuration is modified by this tool.

Run normally, not with Python optimization (`-O`): probe assertions are guards.
The evaluator caps 24 samples, 80 requests, concurrency 1, zero retries,
15 seconds per request and 600 seconds for the request loop. Each full input and
crop is at most 8 seconds. The recorded corpus contains 74.8 seconds total, with
individual inputs 5.21–7.31 seconds long. Repeated manual runs count against any
operator-authorized total budget; the guard is per invocation.

## Evidence and interpretation

- `manifest.json`: fixed Russian texts, source/dry PCM hashes, chunk spans,
  synthesis and augmentation parameters. TTS chunks are reused unchanged across
  variants; only the inserted gap and additive noise change. Peak is normalized
  to 12000 PCM16 units before augmentation; no samples are clipped.
- Source TTS WAVs, final 16 kHz WAVs and dry counterparts: artificial-only audio
  allowing independent verification. Initial failed preparation was stopped by
  the clipping guard before any STT request; the accepted corpus adds headroom.
- `*-boundaries.json`: actual probabilities for each 10 ms frame, exact crop
  indices/hashes, pending-active state and dry-audio loss measurements.
- `*-crop-*.wav`: actual submitted artificial crops, not resynthesized audio.
- `requests.jsonl`: one append-only row per actual request, its PCM hash, model
  echo, synthetic transcript and result. No token or provider credentials.
- `results.jsonl` and `summary.json`: per-input reference WER and aggregate counts.

Both A/B lanes consume **identical saved PCM**, using the production WAV encoder.
The detector is fresh per input and receives 1.2 seconds of prefix audio before
speech; warmup `None` scores are skipped exactly as in production. The final
1.3-second tail completes all active segments without a special flush.

The full-WAV eligibility gate is ordered reference WER <= 0.20; failed baseline
cases must be labeled synthetic TTS/ASR mismatch instead of a VAD verdict.
WER normalizes punctuation, Unicode case and `ё/е`, but preserves word order.

Boundary support is a transparent dry-PCM proxy: 10 ms frames above -30 dB
relative to the chunk's peak frame energy. Lost energy includes the entire dry
chunk and excludes added noise. This is not forced word/phoneme alignment or
human annotation. Inspect retained PCM and both metrics before treating an
acoustic loss as an ASR failure. Correct transcript reconstruction can mask a
substantial omitted vowel onset.

`test_controlled_vad_probe.py` verifies ordered WER, little-endian PCM, direct
boundary-loss measurement and the warmup/current-frame crop mapping without
network calls, downloads or installed TTS.
