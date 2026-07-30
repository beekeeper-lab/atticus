#!/usr/bin/env python3
"""Transcribe an existing audio file with OpenAI, and write it to a file.

The server-side counterpart to WarDog's hyprwhspr dictation. Same API, same
model family, same credential file — but no microphone, no keyboard shortcut,
and no desktop insertion. It takes a file that already exists and writes a
transcript next to it.

    transcribe-audio recording.m4a
    transcribe-audio recording.m4a --output ~/notes/thoughts.md
    transcribe-audio recording.m4a --format text
    transcribe-audio recording.m4a --language en --verbose

Design rules, in priority order:

1. **Never touch the source.** Nothing here writes, moves, or deletes the
   input. Normalization happens on a copy in a temp dir.
2. **Never leak the credential.** The key arrives through the environment and
   is never printed, logged, or written to output. See `_redact`.
3. **Fail in a named way.** A caller — human or agent — should be able to tell
   "your file is broken" from "your card was declined" from "the network
   blipped" without reading a stack trace. See `Err`.
4. **Do not retry what will not succeed.** Auth and quota failures are final.
   Only transport-shaped failures get the backoff.

Exit codes are the `Err` values below, so shell callers can branch on them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from enum import IntEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "processor"))
from audio import (  # noqa: E402
    API_MAX_SECONDS,
    AudioToolMissing,
    join_transcripts,
    plan_chunks,
    slice_audio,
)

MODEL_DEFAULT = os.environ.get("TRANSCRIBE_AUDIO_MODEL") or "gpt-4o-mini-transcribe"

# OpenAI's documented per-request ceiling. Override with --max-bytes if the
# limit moves; the API's own rejection is the backstop either way.
API_MAX_BYTES = 25 * 1024 * 1024

# Extensions the transcription endpoint accepts directly. Anything outside
# this set is a normalization candidate rather than an immediate failure.
API_EXTS = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga",
            ".oga", ".ogg", ".wav", ".webm"}

# Sanity bounds. A "recording" of 30ms is a truncated download; one of 12h is
# almost certainly not what the caller meant to send to a paid API.
MIN_DURATION_S = 0.20
MAX_DURATION_S = 12 * 3600

RETRY_ATTEMPTS = 3


class Err(IntEnum):
    """Exit codes. Names match the error taxonomy in the design brief."""
    OK = 0
    InputFileNotFound = 10
    InvalidAudioFile = 11
    UnsupportedAudioFormat = 12
    OutputAlreadyExists = 13
    MissingCredential = 20
    AuthenticationFailure = 21
    QuotaOrBillingFailure = 22
    NetworkFailure = 30
    ApiTimeout = 31
    TranscriptionFailure = 32
    OutputWriteFailure = 40
    DependencyMissing = 50
    Usage = 2


class Failure(Exception):
    def __init__(self, code: Err, message: str, hint: str = ""):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


# ---------------------------------------------------------------------------
#  logging — the only place that decides what the user sees
# ---------------------------------------------------------------------------

class Log:
    def __init__(self, verbose: bool):
        self.verbose = verbose

    def say(self, msg: str):
        print(msg, flush=True)

    def detail(self, msg: str):
        if self.verbose:
            print(f"  {msg}", flush=True)


def _redact(text: str) -> str:
    """Last line of defence for anything we echo from a library or subprocess.

    The key should never reach here — it is passed via the environment and
    never interpolated into a message. This exists because "should never" is
    not "cannot", and a leaked key in a log file is unrecoverable.
    """
    key = os.environ.get("OPENAI_API_KEY", "")
    if key and len(key) > 8:
        text = text.replace(key, "sk-***REDACTED***")
    # Catch any bearer-shaped token, ours or not.
    import re
    text = re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "sk-***REDACTED***", text)
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1***REDACTED***", text)
    return text


# ---------------------------------------------------------------------------
#  probing
# ---------------------------------------------------------------------------

def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise Failure(
            Err.DependencyMissing,
            f"{tool} is not installed",
            "Fedora: sudo dnf install ffmpeg-free   (provides ffmpeg and ffprobe)",
        )
    return path


def probe(path: Path, log: Log) -> dict:
    """ffprobe → the facts we validate and record.

    Deliberately not trusting the extension: a `.mp3` that is really a
    QuickTime container will be caught here rather than by a paid API call.
    """
    ffprobe = _require("ffprobe")
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams",
           "-print_format", "json", str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise Failure(Err.InvalidAudioFile,
                      "ffprobe timed out inspecting the file",
                      "The file may be corrupt or on unresponsive storage.")
    if p.returncode != 0:
        raise Failure(Err.InvalidAudioFile,
                      "ffprobe could not read the file as media",
                      _redact((p.stderr or "").strip()[:300]))
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        raise Failure(Err.InvalidAudioFile, "ffprobe returned unparseable output")

    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise Failure(
            Err.InvalidAudioFile,
            "the file exists, but ffprobe found no readable audio stream",
            "If this is a video file, extract the audio first; if it is a "
            "download, it may be truncated.",
        )

    fmt = data.get("format", {})
    a = audio[0]
    duration = None
    for src in (fmt.get("duration"), a.get("duration")):
        try:
            duration = float(src)
            break
        except (TypeError, ValueError):
            continue

    info = {
        "container": fmt.get("format_name", "?"),
        "codec": a.get("codec_name", "?"),
        "sample_rate": int(a.get("sample_rate") or 0) or None,
        "channels": a.get("channels"),
        "audio_streams": len(audio),
        "duration_seconds": round(duration, 2) if duration else None,
    }
    log.detail(f"container {info['container']}  codec {info['codec']}  "
               f"{info['sample_rate'] or '?'} Hz  {info['channels'] or '?'} ch  "
               f"{info['audio_streams']} audio stream(s)")
    return info


def validate(src: Path, info: dict, size: int, max_bytes: int,
             allow_chunking: bool = False):
    d = info["duration_seconds"]
    if d is not None and d < MIN_DURATION_S:
        raise Failure(Err.InvalidAudioFile,
                      f"audio is only {d}s long — implausibly short",
                      "A truncated download looks exactly like this.")
    if d is not None and d > MAX_DURATION_S:
        raise Failure(Err.InvalidAudioFile,
                      f"audio is {d / 3600:.1f}h long, beyond the {MAX_DURATION_S // 3600}h "
                      f"sanity limit",
                      "Raise MAX_DURATION_S if this is genuinely intended.")
    if size > max_bytes:
        raise Failure(
            Err.UnsupportedAudioFormat,
            f"{size:,} bytes exceeds the {max_bytes:,}-byte single-request limit",
            "Chunking is not implemented in this version. Split the recording, "
            "or re-encode it smaller (mono 16 kHz) and try again.",
        )
    # Duration, not just size. The API caps DURATION independently, and a small
    # speech-bitrate file sails past the size check and is rejected after the
    # upload. Observed on a 39.6-minute 9.5 MB recording: well under 25 MB, far
    # over the limit, and we paid for the upload to be told so.
    if d is not None and d > API_MAX_SECONDS and not allow_chunking:
        raise Failure(
            Err.UnsupportedAudioFormat,
            f"audio is {d / 60:.1f} minutes; the API limit is "
            f"{API_MAX_SECONDS / 60:.0f} minutes per request",
            "Pass --chunk to split it and transcribe the parts in order.",
        )


# Which ffprobe `format_name` tokens are plausible for a given extension.
# The API dispatches on the FILENAME, so a WAV named .m4a is rejected upstream
# even though it is perfectly valid audio — mislabeled extensions are the norm
# for phone and voice-recorder files, so this is a routine case, not an edge one.
CONTAINER_ALIASES = {
    ".mp3": {"mp3"}, ".mpga": {"mp3"}, ".mpeg": {"mp3", "mpeg"},
    ".wav": {"wav"}, ".flac": {"flac"},
    ".m4a": {"m4a", "mp4", "mov"}, ".mp4": {"mp4", "m4a", "mov"},
    ".ogg": {"ogg"}, ".oga": {"ogg"},
    ".webm": {"webm", "matroska"},
}


def needs_normalizing(src: Path, info: dict) -> str | None:
    """Return a reason to normalize, or None to submit the file as-is.

    Policy is 'normalize only when necessary'. Transcoding costs time, loses a
    little fidelity, and is one more thing to go wrong — so a file the API
    already accepts goes up untouched.
    """
    ext = src.suffix.lower()
    if ext not in API_EXTS:
        return f"extension {ext or '(none)'} is not an accepted upload format"
    if info["audio_streams"] > 1:
        return f"{info['audio_streams']} audio streams — one must be selected"

    expected = CONTAINER_ALIASES.get(ext)
    tokens = {t.strip() for t in (info["container"] or "").split(",") if t.strip()}
    if expected and tokens and tokens.isdisjoint(expected):
        return (f"extension {ext} disagrees with the detected container "
                f"({info['container']})")
    return None


def normalize(src: Path, tmpdir: Path, log: Log) -> Path:
    """Re-encode to a single mono speech-rate stream in a temp directory.

    Encoder choice is discovered, not assumed: Fedora's ffmpeg-free omits some
    encoders, and hardcoding libmp3lame would fail on exactly the package we
    tell people to install.
    """
    ffmpeg = _require("ffmpeg")
    enc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                         capture_output=True, text=True, timeout=60).stdout
    if "libmp3lame" in enc:
        args, out = ["-codec:a", "libmp3lame", "-b:a", "64k"], tmpdir / "normalized.mp3"
    elif "libopus" in enc:
        args, out = ["-codec:a", "libopus", "-b:a", "32k"], tmpdir / "normalized.ogg"
    else:
        args, out = ["-codec:a", "pcm_s16le"], tmpdir / "normalized.wav"
    log.detail(f"normalizing with {args[1]} → {out.name}")

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
           *args, str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise Failure(Err.UnsupportedAudioFormat,
                      "ffmpeg could not normalize the audio",
                      _redact((p.stderr or "").strip()[:300]))
    log.detail(f"normalized to {out.stat().st_size:,} bytes")
    return out


# ---------------------------------------------------------------------------
#  the API call
# ---------------------------------------------------------------------------

def _classify(exc) -> Failure:
    """Map SDK exceptions onto the taxonomy, and decide retryability.

    The distinction that matters most: a 429 can mean 'slow down' (retry) or
    'you are out of money' (do not). They arrive on the same status code and
    are told apart by the error body, so this looks at both.
    """
    import openai
    name = type(exc).__name__
    body = ""
    try:
        body = json.dumps(getattr(exc, "body", None) or {})
    except Exception:
        body = str(getattr(exc, "body", ""))
    blob = f"{exc} {body}".lower()

    if isinstance(exc, openai.AuthenticationError):
        return Failure(Err.AuthenticationFailure,
                       "OpenAI rejected the credential",
                       "The key is present but not accepted. Check that the key "
                       "in ~/.config/ai/env is current and not revoked. "
                       "No key value was displayed.")
    if isinstance(exc, openai.PermissionDeniedError):
        return Failure(Err.AuthenticationFailure,
                       "the key is not permitted to use this model or endpoint",
                       "Check project/org model permissions for "
                       f"{MODEL_DEFAULT}.")
    if isinstance(exc, openai.RateLimitError):
        if any(s in blob for s in ("insufficient_quota", "billing", "exceeded your current quota")):
            return Failure(Err.QuotaOrBillingFailure,
                           "the request was rejected for quota, billing, or "
                           "organization limits",
                           "The local audio file was not modified. This is not "
                           "retried — resolve it in the OpenAI dashboard.")
        f = Failure(Err.NetworkFailure, "rate limited by OpenAI")
        f.retryable = True                                      # type: ignore[attr-defined]
        return f
    if isinstance(exc, openai.APITimeoutError):
        f = Failure(Err.ApiTimeout, "the transcription request timed out")
        f.retryable = True                                      # type: ignore[attr-defined]
        return f
    if isinstance(exc, openai.APIConnectionError):
        f = Failure(Err.NetworkFailure, f"could not reach the API: {_redact(str(exc))}")
        f.retryable = True                                      # type: ignore[attr-defined]
        return f
    if isinstance(exc, openai.InternalServerError):
        f = Failure(Err.TranscriptionFailure, f"OpenAI server error: {_redact(str(exc))}")
        f.retryable = True                                      # type: ignore[attr-defined]
        return f
    if isinstance(exc, openai.BadRequestError):
        # Usually a media problem the probe did not catch.
        return Failure(Err.UnsupportedAudioFormat,
                       f"the API rejected the audio: {_redact(str(exc))}",
                       "Re-run with --verbose to see the detected codec, or try "
                       "--force-normalize.")
    return Failure(Err.TranscriptionFailure,
                   f"{name}: {_redact(str(exc))}")


def transcribe_file(audio: Path, model: str, language: str | None,
                    prompt: str | None, timeout: int, log: Log) -> tuple[str, float]:
    if not os.environ.get("OPENAI_API_KEY"):
        raise Failure(
            Err.MissingCredential,
            "OPENAI_API_KEY is not available to the transcription process",
            "Run this through ~/bin/transcribe-audio, which loads "
            "~/.config/ai/env. No key value was displayed.",
        )
    try:
        import openai
    except ImportError:
        raise Failure(Err.DependencyMissing,
                      "the openai package is not installed in this interpreter",
                      "Use ~/bin/transcribe-audio, which selects the right venv.")

    # max_retries=0: our backoff is the only one, so the retry policy is
    # visible here rather than split between two layers.
    client = openai.OpenAI(timeout=timeout, max_retries=0)
    kwargs = {"model": model, "response_format": "text"}
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["prompt"] = prompt

    last: Failure | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            with audio.open("rb") as fh:
                result = client.audio.transcriptions.create(file=fh, **kwargs)
        except Exception as exc:                       # noqa: BLE001 — classified below
            f = _classify(exc)
            if not getattr(f, "retryable", False) or attempt == RETRY_ATTEMPTS:
                raise f
            backoff = min(2 ** attempt, 8)
            log.detail(f"attempt {attempt} failed ({f.message}); retrying in {backoff}s")
            last = f
            time.sleep(backoff)
            continue
        elapsed = time.monotonic() - started
        text = result if isinstance(result, str) else getattr(result, "text", "")
        if not (text or "").strip():
            raise Failure(Err.TranscriptionFailure,
                          "the API returned an empty transcript",
                          "The audio may be silent. Check it with a player.")
        log.detail(f"api call took {elapsed:.1f}s")
        return text.strip(), elapsed
    raise last or Failure(Err.TranscriptionFailure, "transcription failed")


# ---------------------------------------------------------------------------
#  output
# ---------------------------------------------------------------------------

def transcribe_chunked(audio: Path, info: dict, args, log: Log) -> tuple[str, dict, float]:
    """Split, transcribe each part in order, and join.

    Chunking belongs here more than in the pipeline: a file handed to this
    command was chosen deliberately, so the whole recording is the point —
    whereas a spoken command is short and its wake word comes first.
    """
    total = info["duration_seconds"] or 0
    chunk = min(args.chunk_seconds, API_MAX_SECONDS)
    try:
        plan = plan_chunks(total, chunk, args.chunk_overlap)
    except ValueError as e:
        raise Failure(Err.UnsupportedAudioFormat, str(e))
    log.say(f"Chunking {total / 60:.1f} minutes into {len(plan)} part(s)...")

    tmp = Path(tempfile.mkdtemp(prefix="transcribe-chunk."))
    parts, elapsed = [], 0.0
    try:
        for i, (start, dur) in enumerate(plan, 1):
            piece = tmp / f"part{i:03d}{audio.suffix or '.mp3'}"
            try:
                slice_audio(audio, piece, start, dur)
            except AudioToolMissing as e:
                raise Failure(Err.DependencyMissing, str(e))
            log.say(f"  part {i}/{len(plan)}  "
                    f"{human_duration(start)}-{human_duration(start + dur)}")
            # One failed part fails the whole file rather than returning a
            # transcript with a silent hole in the middle.
            text, secs = transcribe_file(piece, args.model, args.language,
                                         args.prompt, args.timeout, log)
            parts.append(text)
            elapsed += secs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return join_transcripts(parts), {
        "chunks": len(plan),
        "chunk_seconds": chunk,
        "chunk_overlap_seconds": args.chunk_overlap,
    }, elapsed


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    # Before the int branch: bool is a subclass of int, and Python's repr
    # ("False") is not a boolean to a YAML 1.2 parser. The front matter is
    # meant to be machine-read, so it has to be lowercase.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return f'"{s}"' if any(c in s for c in ':#"\'{}[]') else s


def render(text: str, meta: dict, fmt: str) -> str:
    if fmt == "text":
        return text + "\n"
    if fmt == "json":
        return json.dumps({**meta, "text": text}, indent=2) + "\n"
    front = "\n".join(f"{k}: {_yaml_scalar(v)}" for k, v in meta.items())
    return f"---\n{front}\n---\n\n# Transcript\n\n{text}\n"


def write_output(dest: Path, body: str):
    """Temp file plus rename, so an interrupted write cannot leave a partial
    transcript that looks complete."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.partial")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(dest)
    except OSError as e:
        raise Failure(Err.OutputWriteFailure,
                      f"could not write {dest}: {e}")


# ---------------------------------------------------------------------------

def human_duration(seconds: float | None) -> str:
    # `is None`, not falsy: zero is a legitimate offset — the start of the first
    # chunk — and rendering it as "unknown" made the progress line nonsense.
    if seconds is None:
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


EXT = {"markdown": ".md", "text": ".txt", "json": ".json"}


def resolve_source(raw: str, allow_symlinks: bool) -> Path:
    p = Path(raw).expanduser()
    if not p.exists():
        raise Failure(Err.InputFileNotFound, f"no such file: {p}")
    if p.is_symlink() and not allow_symlinks:
        raise Failure(Err.InvalidAudioFile,
                      f"{p} is a symbolic link",
                      "Symlinks are not followed by default. Pass "
                      "--allow-symlinks if that is what you intend.")
    p = p.resolve()
    if not p.is_file():
        raise Failure(Err.InvalidAudioFile, f"not a regular file: {p}")
    if p.stat().st_size == 0:
        raise Failure(Err.InvalidAudioFile, f"file is empty: {p}")
    return p


def run(args) -> int:
    log = Log(args.verbose)
    src = resolve_source(args.input, args.allow_symlinks)
    size = src.stat().st_size

    dest = (Path(args.output).expanduser().resolve() if args.output
            else src.with_suffix(EXT[args.format]))
    if dest == src:
        raise Failure(Err.OutputWriteFailure,
                      "the output path is the source audio file",
                      "Refusing to overwrite the recording with its transcript.")
    if dest.exists() and not args.force:
        raise Failure(Err.OutputAlreadyExists,
                      f"output already exists: {dest}",
                      "Use --force to replace it.")

    log.say(f"Validating {src.name}...")
    info = probe(src, log)
    validate(src, info, size, args.max_bytes, allow_chunking=args.chunk)
    log.detail(f"{size:,} bytes")
    log.say(f"Duration: {human_duration(info['duration_seconds'])}")

    tmpdir = None
    upload = src
    reason = "forced" if args.force_normalize else needs_normalizing(src, info)
    try:
        if reason:
            log.say(f"Normalizing ({reason})...")
            tmpdir = Path(tempfile.mkdtemp(prefix="transcribe-audio."))
            upload = normalize(src, tmpdir, log)
            if upload.stat().st_size > args.max_bytes:
                raise Failure(
                    Err.UnsupportedAudioFormat,
                    f"even after normalizing, {upload.stat().st_size:,} bytes "
                    f"exceeds the {args.max_bytes:,}-byte limit",
                    "Chunking is not implemented in this version.")

        log.say(f"Transcribing with {args.model}...")
        chunk_meta = {}
        if args.chunk and (info["duration_seconds"] or 0) > API_MAX_SECONDS:
            # Too long for one request and the caller asked for chunking. The
            # normalize-and-retry fallback below does not apply: each part is
            # sliced from a file ffprobe has already validated.
            text, chunk_meta, elapsed = transcribe_chunked(upload, info, args, log)
        else:
            try:
                text, elapsed = transcribe_file(upload, args.model, args.language,
                                                args.prompt, args.timeout, log)
            except Failure as f:
                # The probe can pass and the API still refuse on media grounds —
                # it knows container quirks ffprobe does not care about. One
                # re-encode-and-retry, only if we have not already normalized, and
                # only for media rejections (never auth, quota, or network).
                if f.code != Err.UnsupportedAudioFormat or reason:
                    raise
                log.say("Direct upload rejected on media grounds; "
                        "normalizing and retrying once...")
                log.detail(f.message)
                tmpdir = tmpdir or Path(tempfile.mkdtemp(prefix="transcribe-audio."))
                upload = normalize(src, tmpdir, log)
                reason = "retry after API media rejection"
                text, elapsed = transcribe_file(upload, args.model, args.language,
                                                args.prompt, args.timeout, log)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    meta = {
        "source_file": str(src),
        "source_filename": src.name,
        "transcribed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_seconds": info["duration_seconds"],
        "model": args.model,
        "sha256": sha256(src),
    }
    meta.update(chunk_meta)
    if args.verbose:
        meta.update({"codec": info["codec"], "container": info["container"],
                     "sample_rate": info["sample_rate"],
                     "source_bytes": size,
                     "normalized": bool(reason),
                     "api_seconds": round(elapsed, 1)})

    write_output(dest, render(text, meta, args.format))
    words = len(text.split())
    log.say(f"Transcript written to {dest}  ({words:,} words)")
    return Err.OK


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="transcribe-audio",
        description="Transcribe an existing audio file with OpenAI.",
        epilog="The source file is never modified, moved, or deleted.")
    ap.add_argument("input", help="path to an audio file")
    ap.add_argument("-o", "--output", help="transcript path (default: alongside the source)")
    ap.add_argument("-f", "--format", choices=("markdown", "text", "json"),
                    default="markdown")
    ap.add_argument("-l", "--language", help="ISO-639-1 hint, e.g. en. Omit to auto-detect.")
    ap.add_argument("--prompt", default=os.environ.get("TRANSCRIBE_AUDIO_PROMPT") or None,
                    help="steering prompt for punctuation/vocabulary")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--force", action="store_true", help="overwrite an existing transcript")
    ap.add_argument("--chunk", action="store_true",
                    help="split a recording longer than the API limit and "
                         "transcribe the parts in order")
    ap.add_argument("--chunk-seconds", type=int, default=1200,
                    help="chunk length in seconds; clamped to the API maximum")
    ap.add_argument("--chunk-overlap", type=int, default=10,
                    help="overlap so words at a chunk boundary are not lost")
    ap.add_argument("--force-normalize", action="store_true",
                    help="re-encode before upload even if not required")
    ap.add_argument("--allow-symlinks", action="store_true")
    ap.add_argument("--max-bytes", type=int, default=API_MAX_BYTES)
    ap.add_argument("--timeout", type=int, default=300, help="per-request seconds")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        return int(run(args))
    except Failure as f:
        print(f"error [{f.code.name}]: {_redact(f.message)}", file=sys.stderr)
        if f.hint:
            print(f"  {_redact(f.hint)}", file=sys.stderr)
        return int(f.code)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
