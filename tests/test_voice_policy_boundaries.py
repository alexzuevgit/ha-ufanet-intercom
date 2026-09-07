"""Regressions for the measured onset and ordinary internal-pause policy."""

from custom_components.ufanet_intercom.voice_audio import FRAME_BYTES, PcmSegmenter


def run_frames(groups):
    segmenter = PcmSegmenter()
    clips = []
    for count, frame, probability in groups:
        for _ in range(count):
            clip = segmenter.process(frame, probability)
            if clip is not None:
                clips.append(clip)
    return clips


def test_preserves_four_hundred_ms_before_sustained_detection():
    quiet = bytes(FRAME_BYTES)
    onset = b"\x02\x00" * (FRAME_BYTES // 2)
    detected = b"\x03\x00" * (FRAME_BYTES // 2)
    clips = run_frames(
        [(80, quiet, 0.0), (40, onset, 0.0), (40, detected, 1.0), (150, quiet, 0.0)]
    )
    assert len(clips) == 1
    assert clips[0].startswith(onset * 40 + detected * 40)


def test_nine_hundred_ms_internal_pause_keeps_one_phrase():
    quiet = bytes(FRAME_BYTES)
    first = b"\x04\x00" * (FRAME_BYTES // 2)
    second = b"\x05\x00" * (FRAME_BYTES // 2)
    clips = run_frames(
        [
            (80, quiet, 0.0),
            (40, first, 1.0),
            (90, quiet, 0.0),
            (40, second, 1.0),
            (150, quiet, 0.0),
        ]
    )
    assert len(clips) == 1
    assert first * 40 + quiet * 90 + second * 40 in clips[0]
