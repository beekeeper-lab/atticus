"""Speech-to-text via the OpenAI REST endpoint.

Deliberately the same path the machine's dictation already uses
(hyprwhspr, rest-api backend, gpt-4o-mini-transcribe) rather than a second
transcription stack. Same endpoint, same model, same steering prompt.

Guards borrowed from ScribeVault's WhisperService: a pre-upload size check so
a too-large file fails immediately instead of after a slow upload, and
retry-on-transient with backoff.
"""
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests

API_MAX_BYTES = 25 * 1024 * 1024  # OpenAI hard limit

MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".webm": "audio/webm", ".mp4": "audio/mp4", ".mpga": "audio/mpeg",
}


class TranscriptionError(RuntimeError):
    def __init__(self, msg, *, retryable=False, kind="error"):
        super().__init__(msg)
        self.retryable = retryable
        self.kind = kind          # auth | quota | transient | too_large | error


class FileTooLarge(TranscriptionError):
    def __init__(self, size):
        super().__init__(
            f"{size} bytes exceeds the {API_MAX_BYTES}-byte API limit",
            retryable=False, kind="too_large")


API_MAX_SECONDS = 1400   # gpt-4o-transcribe rejects anything longer, with a 400


def _probe_seconds(audio: Path) -> float | None:
    """Duration via ffprobe, or None if it is unavailable or unhelpful.

    Only consulted when upstream metadata has no duration — the point is to
    avoid making ffprobe a hard dependency of the processor for the normal path.
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(audio)],
            capture_output=True, text=True, timeout=60)
        return float((p.stdout or "").strip()) if p.returncode == 0 else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


@contextmanager
def bounded_audio(audio: Path, cfg, duration_hint=None, log=print):
    """Yield (path_to_upload, metadata_dict), truncating an over-long recording.

    Keeps the original untouched: the cut goes to a temp file that is removed on
    exit. `metadata_dict` is empty for a normal recording, so callers can splat
    it into the record either way.
    """
    limit = getattr(cfg, "max_command_seconds", 0)
    seconds = duration_hint if isinstance(duration_hint, (int, float)) else None
    if limit and seconds is None:
        seconds = _probe_seconds(audio)

    if not limit or seconds is None or seconds <= limit:
        # Backstop: no usable duration and the file may still be over the API's
        # own ceiling. Let the request fail with the API's message rather than
        # guessing — but say so, because that 400 is confusing on its own.
        if seconds is None and limit:
            log("    duration unknown (no metadata, no ffprobe) — not truncating")
        yield audio, {}
        return

    exe = shutil.which("ffmpeg")
    if not exe:
        raise TranscriptionError(
            f"recording is {seconds:.0f}s, over the {limit}s command limit, and "
            f"ffmpeg is not installed to truncate it",
            retryable=False, kind="too_large")

    tmp = Path(tempfile.mkdtemp(prefix="atticus-trunc."))
    try:
        cut = tmp / f"head{audio.suffix or '.mp3'}"
        log(f"    {seconds:.0f}s exceeds the {limit}s command limit — "
            f"transcribing the first {limit}s only")
        # -c copy: no re-encode, so this is instant and needs no encoder. Frame
        # -boundary imprecision of a few ms does not matter for speech.
        p = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", str(audio), "-t", str(limit), "-c", "copy", str(cut)],
            capture_output=True, text=True, timeout=300)
        if p.returncode != 0 or not cut.is_file() or cut.stat().st_size == 0:
            raise TranscriptionError(
                f"could not truncate the recording: {(p.stderr or '').strip()[:200]}",
                retryable=False, kind="too_large")
        yield cut, {"truncated_from_seconds": round(seconds, 1),
                    "transcribed_seconds": limit}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe(audio: Path, cfg, *, attempts: int = 3) -> str:
    if not audio.is_file():
        raise TranscriptionError(f"audio missing: {audio}")
    size = audio.stat().st_size
    if size == 0:
        raise TranscriptionError("audio file is empty", kind="error")
    if size > API_MAX_BYTES:
        raise FileTooLarge(size)

    mime = MIME.get(audio.suffix.lower(), "application/octet-stream")
    last = None

    for attempt in range(1, attempts + 1):
        try:
            with audio.open("rb") as fh:
                resp = requests.post(
                    cfg.stt_url,
                    headers={"Authorization": f"Bearer {cfg.openai_key}"},
                    files={"file": (audio.name, fh, mime)},
                    data={"model": cfg.stt_model,
                          "prompt": cfg.stt_prompt,
                          "response_format": "json"},
                    timeout=cfg.stt_timeout,
                )
        except requests.Timeout as e:
            last = TranscriptionError(f"timeout after {cfg.stt_timeout}s",
                                      retryable=True, kind="transient")
        except requests.RequestException as e:
            last = TranscriptionError(f"network error: {type(e).__name__}",
                                      retryable=True, kind="transient")
        else:
            if resp.status_code == 200:
                text = (resp.json().get("text") or "").strip()
                if not text:
                    raise TranscriptionError("API returned empty text", kind="error")
                return text

            # Never surface the response body raw — it can echo the request.
            body = resp.text[:300]
            if resp.status_code in (401, 403):
                raise TranscriptionError(
                    f"auth rejected ({resp.status_code}) — check OPENAI_API_KEY "
                    f"in ~/.config/ai/env", kind="auth")
            if resp.status_code == 429 or "billing" in body.lower() or "quota" in body.lower():
                # Account-side block. Retrying a spend limit accomplishes nothing.
                hard = any(w in body.lower() for w in ("billing", "quota", "spend"))
                raise TranscriptionError(
                    f"rate limited or quota exhausted ({resp.status_code}) — "
                    f"check the OpenAI dashboard", retryable=not hard, kind="quota")
            if resp.status_code >= 500:
                last = TranscriptionError(f"upstream {resp.status_code}",
                                          retryable=True, kind="transient")
            else:
                raise TranscriptionError(
                    f"unexpected {resp.status_code}: {body[:120]}", kind="error")

        if attempt < attempts:
            time.sleep(min(2 ** attempt, 8))

    raise last or TranscriptionError("transcription failed", retryable=True,
                                     kind="transient")


def sanity_check(text: str, cfg) -> tuple[bool, str]:
    """Decide whether a transcript is safe to hand to an autonomous agent.

    The transcript becomes the prompt, so garbage in means an agent acting on
    garbage. gpt-4o-mini-transcribe returns plain text with no confidence
    signal (verbose_json with no_speech_prob is whisper-1 only), so this is
    heuristic by necessity — length and wake phrase, not model confidence.
    """
    words = text.split()
    if len(words) < cfg.min_words:
        return False, f"too short: {len(words)} word(s), need {cfg.min_words}"

    if cfg.wake_phrase:
        head = " ".join(words[:4]).lower().strip(" ,.:;!?")
        if not head.startswith(cfg.wake_phrase):
            return False, f"no wake phrase {cfg.wake_phrase!r} — filed as a note, not executed"

    return True, "ok"


def strip_wake_phrase(text: str, cfg) -> str:
    if not cfg.wake_phrase:
        return text
    low = text.lower()
    i = low.find(cfg.wake_phrase)
    if i == -1:
        return text
    return text[i + len(cfg.wake_phrase):].lstrip(" ,.:;-—").strip() or text
