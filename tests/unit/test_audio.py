"""The audio-slicing primitives shared by the pipeline and the CLI.

These exist as a shared module because the two implementations had already
drifted: a duration guard was added to one and had to be added separately to
the other. Chunking would have been the second such divergence.
"""
import pytest
from audio import API_MAX_SECONDS, join_transcripts, plan_chunks


class TestPlanChunks:
    def test_short_audio_is_a_single_chunk(self):
        assert plan_chunks(120, 1200, 10) == [(0.0, 120)]

    def test_boundary_exactly_at_chunk_length(self):
        assert plan_chunks(1200, 1200, 10) == [(0.0, 1200)]

    def test_covers_the_whole_recording(self):
        plan = plan_chunks(2376, 1200, 10)
        start, dur = plan[-1]
        assert start + dur == pytest.approx(2376)

    def test_no_chunk_exceeds_the_api_limit(self):
        for total in (1500, 2376, 9999, 43200):
            assert all(d <= API_MAX_SECONDS
                       for _, d in plan_chunks(total, 1200, 10))

    def test_consecutive_chunks_overlap(self):
        plan = plan_chunks(3000, 1200, 10)
        for (s1, d1), (s2, _) in zip(plan, plan[1:], strict=False):
            assert s2 < s1 + d1, "a gap here would silently lose speech"
            assert s1 + d1 - s2 == pytest.approx(10)

    def test_overlap_must_be_smaller_than_the_chunk(self):
        with pytest.raises(ValueError):
            plan_chunks(5000, 100, 100)

    def test_pathological_overlap_is_refused_not_billed(self):
        """overlap=1199 against chunk=1200 is a one-second step: 8,801 paid
        requests for a three-hour recording. It must fail, not bill."""
        with pytest.raises(ValueError, match="half the chunk"):
            plan_chunks(10_000, 1200, 1199)

    def test_a_long_recording_stays_a_sane_number_of_parts(self):
        assert len(plan_chunks(12 * 3600, 1200, 10)) < 50


class TestJoinTranscripts:
    def test_removes_the_duplicated_overlap(self):
        a = "the quick brown fox jumps over the lazy dog"
        b = "over the lazy dog and then runs away"
        assert join_transcripts([a, b]) == (
            "the quick brown fox jumps over the lazy dog and then runs away")

    def test_concatenates_when_nothing_matches(self):
        """Conservative on purpose: a duplicated sentence is cosmetic, dropping
        real speech because a fuzzy match went wrong is not."""
        assert join_transcripts(["alpha beta", "gamma delta"]) == \
            "alpha beta gamma delta"

    def test_matches_across_case_and_punctuation(self):
        out = join_transcripts(["we discussed the budget.", "The budget, and then costs"])
        assert out.lower().count("budget") == 1

    def test_single_and_empty_inputs(self):
        assert join_transcripts(["only one"]) == "only one"
        assert join_transcripts([]) == ""
        assert join_transcripts(["", "  ", "real"]) == "real"

    def test_three_chunks_join_cleanly(self):
        parts = ["one two three four", "three four five six", "five six seven eight"]
        assert join_transcripts(parts) == "one two three four five six seven eight"


class TestChunkedOrchestration:
    """transcribe_long's control flow, with the API stubbed out."""

    def test_chunks_are_transcribed_in_order_and_joined(self, cfg, tmp_path, monkeypatch):
        import transcribe as stt
        cfg.chunk_seconds, cfg.chunk_overlap_seconds = 1200, 10

        audio = tmp_path / "long.mp3"
        audio.write_bytes(b"\xff\xfb\x90\x00" + b"\0" * 4096)
        monkeypatch.setattr(stt, "slice_audio", lambda *a, **k: a[1].write_bytes(b"x"))

        calls = []

        def fake(part, _cfg, attempts=3):
            calls.append(part.name)
            return f"chunk{len(calls)}"
        monkeypatch.setattr(stt, "transcribe", fake)

        text, meta = stt.transcribe_long(audio, cfg, 2376, log=lambda m: None)
        assert calls == sorted(calls), "parts must be transcribed in order"
        assert meta["chunks"] == 2
        assert meta["chunk_overlap_seconds"] == 10
        assert text == "chunk1 chunk2"

    def test_a_failed_part_fails_the_recording(self, cfg, tmp_path, monkeypatch):
        """Better to fail than to return a transcript with a silent hole."""
        import transcribe as stt
        cfg.chunk_seconds, cfg.chunk_overlap_seconds = 1200, 10
        audio = tmp_path / "long.mp3"
        audio.write_bytes(b"\xff\xfb\x90\x00")
        monkeypatch.setattr(stt, "slice_audio", lambda *a, **k: a[1].write_bytes(b"x"))

        def boom(part, _cfg, attempts=3):
            if "002" in part.name:
                raise stt.TranscriptionError("upstream 500", retryable=True)
            return "ok"
        monkeypatch.setattr(stt, "transcribe", boom)

        with pytest.raises(stt.TranscriptionError):
            stt.transcribe_long(audio, cfg, 2376, log=lambda m: None)

    def test_chunk_length_is_clamped_to_the_api_limit(self, cfg, tmp_path, monkeypatch):
        import transcribe as stt
        cfg.chunk_seconds, cfg.chunk_overlap_seconds = 9999, 10
        audio = tmp_path / "long.mp3"
        audio.write_bytes(b"\xff\xfb\x90\x00")
        monkeypatch.setattr(stt, "slice_audio", lambda *a, **k: a[1].write_bytes(b"x"))
        monkeypatch.setattr(stt, "transcribe", lambda *a, **k: "t")
        _, meta = stt.transcribe_long(audio, cfg, 5000, log=lambda m: None)
        assert meta["chunk_seconds"] == API_MAX_SECONDS
