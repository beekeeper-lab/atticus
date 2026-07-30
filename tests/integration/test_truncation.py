"""ADR-004 — over-long recordings are truncated, never rejected."""
import shutil
import subprocess
import pytest
import transcribe as stt

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg absent")


@pytest.fixture
def long_wav(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg absent")
    p = tmp_path / "long.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=400", "-ar", "16000",
                    "-ac", "1", str(p)], check=True)
    return p


@needs_ffmpeg
def test_long_recording_is_truncated_not_refused(long_wav, cfg):
    with stt.bounded_audio(long_wav, cfg, 400, log=lambda m: None) as (path, meta):
        assert meta["truncated_from_seconds"] == 400
        assert meta["transcribed_seconds"] == cfg.max_command_seconds
        assert path != long_wav
        assert path.stat().st_size < long_wav.stat().st_size
    assert not path.exists(), "temp file leaked"
    assert long_wav.exists(), "SOURCE WAS MODIFIED"


@needs_ffmpeg
def test_short_recording_passes_through_untouched(long_wav, cfg):
    with stt.bounded_audio(long_wav, cfg, 12, log=lambda m: None) as (path, meta):
        assert meta == {} and path == long_wav
