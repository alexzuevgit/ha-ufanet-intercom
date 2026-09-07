"""Pure evidence-metric checks; no model, audio download or network request."""

from types import SimpleNamespace

from controlled_vad_probe import (
    crop_coverage,
    pcm_bytes,
    pcm_values,
    segment,
    word_errors,
)


def test_word_metric_preserves_order_and_normalizes_russian():
    assert word_errors("Принёс посылку.", "принес ПОСЫЛКУ!")["errors"] == 0
    assert word_errors("синий велосипед", "велосипед синий")["errors"] == 2
    assert word_errors("раз два", "раз")["wer"] == 0.5
    assert word_errors("раз", "раз два три")["wer"] == 2


def test_pcm_little_endian_round_trip():
    values = [-32768, -1, 0, 1, 32767]
    assert pcm_bytes(values) == b"\x00\x80\xff\xff\x00\x00\x01\x00\xff\x7f"
    assert list(pcm_values(pcm_bytes(values))) == values


def test_boundary_metric_detects_dry_speech_loss_not_padding():
    dry = pcm_bytes([0] * 160 + [100] * 320 + [0] * 160)
    chunks = [{"start_sample": 0, "end_sample": 640, "text": "artificial"}]
    intact = crop_coverage(dry, [{"start_sample": 160, "end_sample": 480}], chunks)[0]
    assert intact["lost_energy_fraction"] == 0
    assert intact["missing_active_frames"] == 0
    clipped = crop_coverage(dry, [{"start_sample": 320, "end_sample": 640}], chunks)[0]
    assert clipped["lost_energy_fraction"] == 0.5
    assert clipped["missing_active_frames"] == 1
    assert clipped["missing_active_frame_starts"] == [160]


def test_probe_uses_current_frame_skips_warmup_and_exact_crop():
    class Detector:
        def __init__(self):
            self.scores = iter([None, 0.8, 0.9, 0.0])

        def process(self, frame):
            assert len(frame) == 320
            return next(self.scores)

    class Segmenter:
        active_frame_count = 0

        def __init__(self):
            self.frames = []

        def process(self, frame, probability):
            self.frames.append(frame)
            if probability == 0.0:
                return b"".join(self.frames)
            return None

    audio = SimpleNamespace(
        MicroVadAdapter=Detector,
        PcmSegmenter=Segmenter,
        FRAME_BYTES=320,
        MAX_SEGMENT_FRAMES=800,
    )
    pcm = b"".join(bytes([i]) * 320 for i in range(4))
    clips, scores, pending = segment(pcm, audio)
    assert scores == [None, 0.8, 0.9, 0.0]
    assert pending == 0
    assert len(clips) == 1
    assert clips[0]["start_sample"] == 160
    assert clips[0]["end_sample"] == 640
    assert clips[0]["pcm"] == pcm[320:]
