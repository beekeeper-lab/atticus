"""Speech-to-text via the OpenAI REST endpoint.

Deliberately the same path the machine's dictation already uses (hyprwhspr,
rest-api backend) rather than a second transcription stack. Same endpoint and
the same steering prompt — but a LARGER model: dictation runs
`gpt-4o-mini-transcribe` because the user is watching a cursor and fixes typos
as they appear, whereas here a misheard word silently becomes an autonomous
agent's instruction. See SPEC §2.3 for the cost/error-asymmetry argument.

Guards borrowed from ScribeVault's WhisperService: a pre-upload size check so
a too-large file fails immediately instead of after a slow upload, and
retry-on-transient with backoff.
"""
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from audio import (
    API_MAX_BYTES,
    API_MAX_SECONDS,
    AudioToolMissing,
    join_transcripts,
    plan_chunks,
    probe_seconds,
    slice_audio,
)

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


_probe_seconds = probe_seconds   # kept for callers that import it by name


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

    if not limit or (seconds is not None and seconds <= limit):
        # No limit configured, or a KNOWN duration within it. Pass the original
        # through untouched.
        yield audio, {}
        return

    # We are here because either the recording is over the limit, OR a limit is
    # set but we could not establish a duration at all (no metadata, no
    # ffprobe). Both must be CUT. Passing an unknown-duration file through
    # "because we can't prove it's too long" fails OPEN — a 40-minute ambient
    # recording would then be transcribed in full, which is exactly the exposure
    # the limit exists to cap. Cut blindly to the limit; if the source is
    # actually shorter, ffmpeg simply produces a file of the original length.
    exe = shutil.which("ffmpeg")
    if not exe:
        known = f"{seconds:.0f}s" if seconds is not None else "of unknown length"
        raise TranscriptionError(
            f"recording is {known}, at or over the {limit}s command limit, and "
            f"ffmpeg is not installed to truncate it",
            retryable=False, kind="too_large")

    tmp = Path(tempfile.mkdtemp(prefix="atticus-trunc."))
    try:
        cut = tmp / f"head{audio.suffix or '.mp3'}"
        if seconds is None:
            log(f"    duration unknown — cutting blindly to the first {limit}s "
                f"rather than transcribing an unbounded recording")
        else:
            log(f"    {seconds:.0f}s exceeds the {limit}s command limit — "
                f"transcribing the first {limit}s only")
        try:
            slice_audio(audio, cut, 0, limit)
        except AudioToolMissing as e:
            raise TranscriptionError(str(e), retryable=False, kind="too_large")
        meta = {"transcribed_seconds": limit}
        # Only claim a truncation-from length when we actually know it; the
        # pipeline's "was the device left running?" alarm keys off this.
        if seconds is not None:
            meta["truncated_from_seconds"] = round(seconds, 1)
        yield cut, meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def transcribe_long(audio: Path, cfg, duration: float, log=print,
                    *, attempts: int = 3) -> tuple[str, dict]:
    """Transcribe a recording too long for one request, by chunking it.

    This is the DOCUMENT path, and it is opt-in for a reason. Truncation is
    right for a command — the wake phrase comes first, so everything after the
    opening seconds is silence or ambient speech, and transcribing 40 minutes of
    someone's day would be both wasteful and a privacy problem. But a meeting
    handed over deliberately is the opposite case: the whole thing is the point.

    Chunks overlap so a word split across a boundary is not lost in both halves;
    the duplicated run is removed when the parts are joined.
    """
    chunk = getattr(cfg, "chunk_seconds", 1200)
    overlap = getattr(cfg, "chunk_overlap_seconds", 10)
    if chunk > API_MAX_SECONDS:
        chunk = API_MAX_SECONDS

    plan = plan_chunks(duration, chunk, overlap)
    log(f"    chunking {duration:.0f}s into {len(plan)} part(s) "
        f"of up to {chunk}s with {overlap}s overlap")

    tmp = Path(tempfile.mkdtemp(prefix="atticus-chunk."))
    parts: list[str] = []
    try:
        for i, (start, dur) in enumerate(plan, 1):
            piece = tmp / f"part{i:03d}{audio.suffix or '.mp3'}"
            try:
                slice_audio(audio, piece, start, dur)
            except AudioToolMissing as e:
                raise TranscriptionError(str(e), retryable=False, kind="too_large")
            log(f"      part {i}/{len(plan)}  {start:.0f}s–{start + dur:.0f}s")
            # A failure in one part fails the whole recording rather than
            # yielding a transcript with a silent hole in the middle.
            parts.append(transcribe(piece, cfg, attempts=attempts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    text = join_transcripts(parts)
    return text, {
        "chunks": len(plan),
        "chunk_seconds": chunk,
        "chunk_overlap_seconds": overlap,
        "transcribed_seconds": round(duration, 1),
    }


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
        except requests.Timeout:
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
    garbage. gpt-4o-transcribe returns plain text with no confidence
    signal (verbose_json with no_speech_prob is whisper-1 only), so this is
    heuristic by necessity — length and wake phrase, not model confidence.
    """
    words = text.split()
    if len(words) < cfg.min_words:
        return False, f"too short: {len(words)} word(s), need {cfg.min_words}"

    if cfg.wake_phrase:
        head = " ".join(words[:5]).lower().strip(" ,.:;!?")
        head = _drop_fillers(head)
        triggers = [cfg.wake_phrase, *getattr(cfg, "wake_aliases", [])]
        if not any(_triggers_at_start(head, t) for t in triggers if t):
            return False, f"no wake phrase {cfg.wake_phrase!r} — filed as a note, not executed"


    return True, "ok"


# Fillers that precede a direct address. Deliberately excludes "so" and "well":
# those introduce DESCRIPTION as often as address ("So Atticus is a thing I
# built…"), and a false positive runs an agent on speech never aimed at it.
# A false negative only files a note, which is recoverable and visible.
_FILLERS = ("um", "uh", "er", "erm", "okay", "ok", "hey", "alright", "right")


def _triggers_at_start(head: str, trigger: str) -> bool:
    """True when `head` begins with `trigger` on a word boundary.

    Plain startswith() admitted "Atticusville" and "Atticus's" as the wake
    phrase. Harmless for a long distinctive phrase; a real false accept for a
    short alias, where the gate is the only thing standing between ambient
    speech and an autonomous agent.
    """
    if not head.startswith(trigger):
        return False
    rest = head[len(trigger):]
    return rest == "" or not (rest[0].isalnum() or rest[0] == "'")


def leading_words(text: str, n: int) -> list[str]:
    """The opening words of a transcript, lowercased, with filler removed.

    sanity_check strips filler before matching but the adjudicator path did not,
    so "Okay, Artemis, research…" asked whether "Okay" was a mishearing of the
    wake phrase while handing over "artemis research…" as the context — a
    nonsense question with a near-certain hold, on precisely the mishearing the
    adjudicator exists to recover. Both paths now start from the same words.
    """
    head = _drop_fillers(" ".join(text.split()).lower().strip(" ,.:;!?"))
    out = []
    for raw in head.split():
        tok = raw.strip(" ,.:;!?\"'()[]—-")
        if tok:
            out.append(tok)
        if len(out) >= n:
            break
    return out


def _drop_fillers(head: str) -> str:
    """Strip leading conversational filler so "Okay, Atticus, …" still triggers."""
    changed = True
    while changed:
        changed = False
        for f in _FILLERS:
            for prefix in (f + " ", f + ", "):
                if head.startswith(prefix):
                    head = head[len(prefix):].lstrip(" ,")
                    changed = True
    return head


def extract_command(text: str, cfg) -> tuple[str, dict]:
    """The bounded instruction actually handed to the agent.

    The preamble ASKS the model to ignore trailing speech. That is guidance, not
    a control: the transcript that motivated this held 389 words of which ~25
    were the command, and included "hey Atticus, send a message to <name>"
    spoken as an example of a future capability.

    Two bounds, whichever bites first: a sentence count and a character cap.
    The full transcript always stays in the vault; only the prompt is cut.

    WHAT THIS DOES NOT DO. No positional heuristic can separate a command from
    speech that immediately follows it. If you keep talking for two sentences
    after the request, those two sentences reach the agent. This bounds
    exposure — it does not isolate intent. Real isolation needs a terminator
    phrase, silence segmentation (which needs word timestamps, i.e. whisper-1),
    or an extraction model. See docs/HARDENING.md A4.
    """
    instruction = strip_wake_phrase(text, cfg)
    max_chars = getattr(cfg, "max_command_chars", 0)
    max_sents = getattr(cfg, "max_command_sentences", 0)

    cut = len(instruction)
    if max_sents:
        # Sentence ends, not decimal points or abbreviations we cannot detect.
        ends = [m.end() for m in re.finditer(r"[.!?](?:\s|$)", instruction)]
        if len(ends) > max_sents:
            cut = min(cut, ends[max_sents - 1])
    if max_chars and cut > max_chars:
        window = instruction[:max_chars]
        b = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
        if b > max_chars // 3:
            cut = b + 1
        else:
            # rfind returns -1 when there is no space. `-1 or max_chars` kept the
            # -1 (truthy) and cut to instruction[:-1] — dropping one char instead
            # of enforcing the cap. Handle the sentinel explicitly.
            sp = window.rfind(" ")
            cut = sp if sp > 0 else max_chars

    trimmed = instruction[:cut].strip()
    if trimmed == instruction.strip():
        return instruction, {}
    return trimmed, {
        "command_chars": len(trimmed),
        "transcript_chars": len(instruction),
        "command_clipped": True,
    }


def strip_wake_phrase(text: str, cfg) -> str:
    if not cfg.wake_phrase:
        return text
    low = text.lower()
    # Also try configured aliases, not just the wake phrase. sanity_check admits
    # a command that opens with an alias ("Advocates, research …"), but stripping
    # only the wake phrase then left the misheard name at the front of the prompt
    # handed to the agent. Take the EARLIEST trigger that appears, so the
    # instruction starts right after whichever word actually gated it.
    triggers = [cfg.wake_phrase, *getattr(cfg, "wake_aliases", [])]
    best_i, best_len = -1, 0
    for t in triggers:
        if not t:
            continue
        i = low.find(t)
        if i != -1 and (best_i == -1 or i < best_i):
            best_i, best_len = i, len(t)
    if best_i == -1:
        return text
    return text[best_i + best_len:].lstrip(" ,.:;-—").strip() or text
