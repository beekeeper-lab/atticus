"""Audio slicing shared by the pipeline and the standalone CLI.

These two had already drifted once — a duration guard was added to one and had
to be added separately to the other — so the parts that are genuinely the same
live here. What stays separate is the API transport, because that difference is
real and deliberate: the pipeline posts with `requests` and the larger
`gpt-4o-transcribe`, the CLI uses the OpenAI SDK and the mini model to match the
desktop dictation stack.

Nothing here talks to a network. Stdlib plus ffmpeg only, so the CLI's own venv
can import it without inheriting the pipeline's dependencies.
"""
import shutil
import subprocess
from pathlib import Path

# gpt-4o-transcribe rejects anything longer with a 400. Independent of the
# 25 MB size limit: a long, low-bitrate speech recording passes the byte check
# and is refused after the upload.
API_MAX_SECONDS = 1400

# A 12-hour recording at the default chunk length is 36 parts. Hundreds means
# a misconfiguration, and every part is a paid request.
MAX_CHUNKS = 200


class AudioToolMissing(RuntimeError):
    """ffmpeg or ffprobe is not installed."""


def have(tool: str) -> bool:
    return bool(shutil.which(tool))


def probe_seconds(audio: Path) -> float | None:
    """Duration via ffprobe, or None when unavailable.

    Returns None rather than raising so callers can fall back to upstream
    metadata; ffprobe is deliberately not a hard dependency of the normal path.
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


def slice_audio(src: Path, dest: Path, start: float, duration: float) -> None:
    """Cut [start, start+duration) into dest.

    `-c copy` so there is no re-encode: instant, and it needs no encoder, which
    matters because Fedora's ffmpeg-free omits several. Frame-boundary
    imprecision of a few milliseconds is irrelevant for speech.
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioToolMissing("ffmpeg is not installed")
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src), "-t", str(duration), "-c", "copy", str(dest)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise AudioToolMissing(
            f"ffmpeg could not slice the audio: {(p.stderr or '').strip()[:200]}")


def plan_chunks(total: float, chunk: float, overlap: float) -> list[tuple[float, float]]:
    """(start, duration) pairs covering `total`, each overlapping the last.

    The overlap exists because a hard cut lands mid-word roughly as often as
    not, and a word split across two requests is transcribed wrong in both. A
    few seconds of deliberate duplication is cheap; a lost sentence is not.

    Never emits a chunk longer than `chunk`, so every part stays inside the
    API's per-request ceiling.
    """
    if total <= chunk:
        return [(0.0, total)]
    # Each chunk is a paid API call, so a pathological overlap is a cost bomb,
    # not merely inefficient: overlap=1199 against chunk=1200 gives a one-second
    # step and 8,801 requests for a three-hour recording. Refuse loudly — this
    # can only come from a misconfigured .env, and the operator should hear it.
    if overlap >= chunk / 2:
        raise ValueError(
            f"overlap ({overlap}s) must be under half the chunk length "
            f"({chunk}s); anything more explodes the request count")

    out: list[tuple[float, float]] = []
    start = 0.0
    step = chunk - overlap
    while start < total:
        remaining = total - start
        out.append((round(start, 3), round(min(chunk, remaining), 3)))
        if remaining <= chunk:
            break
        start += step
        # Backstop against arithmetic nobody anticipated. A real recording needs
        # a handful of chunks; hundreds means something is wrong upstream.
        if len(out) > MAX_CHUNKS:
            raise ValueError(
                f"chunk plan exceeded {MAX_CHUNKS} parts for a {total:.0f}s "
                f"recording — check chunk_seconds and chunk_overlap_seconds")
    return out


def join_transcripts(parts: list[str], overlap_words: int = 40) -> str:
    """Concatenate chunk transcripts, dropping text duplicated by the overlap.

    Each chunk re-transcribes the last few seconds of its predecessor, so
    consecutive parts share a run of words. Find the longest suffix of the
    previous part that opens the next one, and drop it.

    Deliberately conservative: on no match the parts are simply concatenated.
    A duplicated sentence is a cosmetic flaw; dropping real speech because a
    fuzzy match went wrong is not.
    """
    joined = ""
    for part in (p.strip() for p in parts if p and p.strip()):
        if not joined:
            joined = part
            continue

        tail = joined.split()[-overlap_words:]
        head = part.split()

        best = 0
        for n in range(min(len(tail), len(head)), 0, -1):
            # Compare case- and punctuation-insensitively: the same words come
            # back capitalised differently either side of a chunk boundary.
            if _norm(tail[-n:]) == _norm(head[:n]):
                best = n
                break
        joined = f"{joined} {' '.join(head[best:])}".strip()
    return joined


def _norm(words: list[str]) -> list[str]:
    return [w.lower().strip(".,;:!?\"'—-") for w in words]
