"""Voice a two-host script into an audio overview, and link it from the report.

The agent writes `output/podcast-script.md` (see `skills/podcast-companion/`);
this module turns it into an MP3 sitting beside the HTML and injects a player
near the top of that HTML.

**Why this is pipeline code and not a skill.** Synthesis needs an OpenAI key, and
the agent is deliberately denied every credential this host holds — it executes
text derived from ambient audio, so a TTS key inside the sandbox would be a
credential reachable from anything spoken near the pin. Splitting the work at the
script boundary keeps the model's judgement (what to say) with the agent and the
spend (saying it) with the pipeline. It also makes the script a reviewable,
committed artifact rather than an invisible intermediate.

**Nothing here may fail a record.** The HTML report is the deliverable; audio is
a companion. A TTS outage, an exhausted budget or a malformed script must leave a
good report published, so `generate()` returns a result dict and raises only on
programmer error. The caller logs and moves on.

NotebookLM was the original ask. Google's Discovery Engine Podcast API — the only
programmatic route to a real NotebookLM audio overview — was deprecated in 2026
with no new allowlisting, so there is no API to call. This reproduces the format
(two hosts, conversational, summarising one source) with a TTS model instead.
"""
import base64
import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

import audio as au

SCRIPT_NAME = "podcast-script.md"
AUDIO_NAME = "podcast.mp3"

# One turn: `**A:** text`. Anchored to line start so a bolded phrase mid-sentence
# cannot be mistaken for a speaker label.
_TURN = re.compile(r"^\*\*([AB])\:\*\*\s*(.+?)\s*$", re.MULTILINE)
_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# Where the player goes. Prefer just after the opening <body ...>; fall back to
# the top of the document. Deliberately NOT "after the first </h1>": an agent
# report often wraps its title in a header block, and landing inside that block
# breaks the layout in ways that are invisible until someone opens the page.
_BODY_OPEN = re.compile(r"(?i)<body[^>]*>")

# The injected block is fenced by comments so it can be replaced as a unit — the
# markup inside it changes, and matching on a class name cannot tell "there is a
# player" from "there is a CURRENT player". Comments survive the vault's
# sanitiser, which only rewrites active constructs.
BLOCK_OPEN = "<!--atticus-audio-->"
BLOCK_CLOSE = "<!--/atticus-audio-->"
_BLOCK = re.compile(re.escape(BLOCK_OPEN) + r".*?" + re.escape(BLOCK_CLOSE),
                    re.DOTALL)

# The first shipped block had no fences. Reports published in that window are
# already in the vault, so an upgrade has to be able to find one — matching only
# the fenced form appended a second player instead of replacing the first, which
# is what happened on 2026-07-31T135221Z before this existed.
_LEGACY_BLOCK = re.compile(
    r"<style>\.atticus-audio\{.*?</style>\s*"
    r'<div class="atticus-audio">.*?</div>', re.DOTALL)

# gpt-4o-mini-tts bills per token, but the useful unit for an estimate made
# BEFORE the call is characters of script. OpenAI documents roughly $0.015 per
# minute of generated audio; conversational speech runs about 14 characters per
# second.
#
# Treat this as an order-of-magnitude guard, NOT a tight bound. Measured
# 2026-07-31: the same 124-character script came back as 8.5s on one run and
# 12.1s on the next, so the model's own pacing varies by a third between
# identical requests. That is why ATTICUS_PODCAST_MAX_USD defaults to roughly
# three times a normal episode, and why the ledger records the measured cost
# rather than this one.
_USD_PER_AUDIO_MINUTE = 0.015
_CHARS_PER_SECOND = 14.0

# Gemini's published rates (ai.google.dev/gemini-api/docs/pricing, 2026-07-31).
# Audio bills at a measured 25 tokens/second, so $10/1M works out to the same
# $0.015/minute as OpenAI — the saving comes from faster delivery, not a cheaper
# rate. Kept as named constants because they are the only figures in this module
# that a provider can change under us.
_GEMINI_AUDIO_USD_PER_MTOK = 10.00
_GEMINI_TEXT_USD_PER_MTOK = 0.50

# A hard structural bound, separate from the money bound. A script this long is
# not a summary and almost certainly means the agent misunderstood the task.
MAX_CHARS = 40_000


class PodcastError(Exception):
    """Audio could not be produced. Never fatal to the record."""


def find_script(outdir: Path) -> Path | None:
    p = outdir / SCRIPT_NAME
    return p if p.is_file() else None


def parse_script(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(title, [(speaker, line), …]).

    Tolerant on purpose: anything that is not a heading or a turn is dropped
    rather than raising, because the agent writing a stray note above the script
    should not cost the whole episode.
    """
    m = _TITLE.search(text)
    title = m.group(1).strip() if m else ""
    turns = [(sp, body.strip()) for sp, body in _TURN.findall(text) if body.strip()]
    return title, turns


def estimate(turns: list[tuple[str, str]]) -> dict:
    chars = sum(len(t) for _, t in turns)
    seconds = chars / _CHARS_PER_SECOND
    return {"turns": len(turns), "chars": chars,
            "seconds": round(seconds, 1),
            "usd": round((seconds / 60.0) * _USD_PER_AUDIO_MINUTE, 6)}


def _speak(text: str, voice: str, cfg, dest: Path) -> None:
    """One turn → one MP3. Raises PodcastError on anything non-transient."""
    body = {
        "model": cfg.tts_model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if getattr(cfg, "tts_instructions", ""):
        body["instructions"] = cfg.tts_instructions
    try:
        resp = requests.post(
            cfg.tts_url,
            headers={"Authorization": f"Bearer {cfg.openai_key}"},
            json=body, timeout=cfg.tts_timeout,
        )
    except requests.Timeout:
        raise PodcastError(f"TTS timeout after {cfg.tts_timeout}s")
    except requests.RequestException as e:
        raise PodcastError(f"TTS network error: {type(e).__name__}")

    if resp.status_code != 200:
        # Same reasoning as transcribe.py: this string can reach the vault, and
        # git is forever, so the body is truncated and never echoed wholesale.
        if resp.status_code in (401, 403):
            raise PodcastError(f"TTS auth rejected ({resp.status_code}) — check "
                               f"OPENAI_API_KEY in ~/.config/ai/env")
        raise PodcastError(f"TTS returned {resp.status_code}: {resp.text[:120]}")
    if not resp.content:
        raise PodcastError("TTS returned an empty body")
    dest.write_bytes(resp.content)


def _speak_gemini(turns: list[tuple[str, str]], cfg, dest: Path) -> dict:
    """Render the WHOLE dialogue in one Gemini call. Returns measured usage.

    This is the path that fixes the thing per-turn synthesis cannot: the model
    sees the entire conversation, so a reply is paced as a reply. It also removes
    the per-turn loop and the ffmpeg concat entirely — one request, one decode.

    Gemini returns raw 16-bit little-endian PCM at 24 kHz mono, not a container,
    so ffmpeg wraps it. And it returns usageMetadata, which means cost here is
    MEASURED rather than derived from duration — no estimate to drift.
    """
    a, b = "Alex", "Blake"
    script = "\n".join(f"{a if spk == 'A' else b}: {text}" for spk, text in turns)
    style = (cfg.gemini_tts_style or "").format(a=a, b=b)
    body = {
        "contents": [{"parts": [{"text": f"{style}\n\n{script}" if style else script}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                {"speaker": a, "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": cfg.gemini_voice_a}}},
                {"speaker": b, "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": cfg.gemini_voice_b}}},
            ]}},
        },
    }
    url = cfg.gemini_tts_url.format(model=cfg.gemini_tts_model)
    try:
        resp = requests.post(url, json=body, timeout=cfg.gemini_tts_timeout,
                             headers={"x-goog-api-key": cfg.gemini_key})
    except requests.Timeout:
        raise PodcastError(f"Gemini TTS timeout after {cfg.gemini_tts_timeout}s")
    except requests.RequestException as e:
        raise PodcastError(f"Gemini TTS network error: {type(e).__name__}")
    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            raise PodcastError(f"Gemini TTS auth rejected ({resp.status_code}) — "
                               f"check GEMINI_API_KEY in ~/.config/ai/env")
        raise PodcastError(f"Gemini TTS returned {resp.status_code}: "
                           f"{resp.text[:160]}")
    try:
        payload = resp.json()
        cand = payload["candidates"][0]
        inline = cand["content"]["parts"][0]["inlineData"]
        pcm = base64.b64decode(inline["data"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise PodcastError(f"Gemini TTS response not understood: {type(e).__name__}")

    # A truncated render sounds like a complete episode that stops mid-sentence,
    # which is worse than a failure because nothing flags it. Refuse it.
    reason = cand.get("finishReason")
    if reason and reason != "STOP":
        raise PodcastError(f"Gemini stopped early (finishReason={reason}) — the "
                           f"script is probably too long for one call")
    if not pcm:
        raise PodcastError("Gemini TTS returned no audio")

    exe = shutil.which("ffmpeg")
    if not exe:
        raise PodcastError("ffmpeg is not installed, so the PCM cannot be encoded")
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "audio.pcm"
        raw.write_bytes(pcm)
        p = subprocess.run(
            [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", str(raw),
             "-c:a", "libmp3lame", "-b:a", str(cfg.tts_bitrate_kbps) + "k",
             str(dest)],
            capture_output=True, text=True, timeout=300)
    if p.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise PodcastError(f"ffmpeg could not encode the audio: "
                           f"{(p.stderr or '').strip()[:200]}")

    meta = payload.get("usageMetadata") or {}
    audio_tok = next((d.get("tokenCount", 0)
                      for d in meta.get("candidatesTokensDetails") or []
                      if d.get("modality") == "AUDIO"), 0)
    in_tok = meta.get("promptTokenCount", 0)
    return {
        "audio_tokens": audio_tok,
        "input_tokens": in_tok,
        "usd": round(audio_tok / 1e6 * _GEMINI_AUDIO_USD_PER_MTOK
                     + in_tok / 1e6 * _GEMINI_TEXT_USD_PER_MTOK, 6),
        "calls": 1,
    }


def _concat(parts: list[Path], dest: Path) -> None:
    """Join per-turn MP3s. Stream copy — same model and format throughout, so
    re-encoding would only lose quality and time."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise PodcastError("ffmpeg is not installed, so turns cannot be joined")
    with tempfile.TemporaryDirectory() as td:
        listing = Path(td) / "parts.txt"
        # ffmpeg's concat demuxer parses this file; single-quote the paths and
        # escape any quote in them.
        listing.write_text("".join(
            "file '{}'\n".format(str(p).replace("'", r"'\''")) for p in parts))
        p = subprocess.run(
            [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(dest)],
            capture_output=True, text=True, timeout=120,
        )
    if p.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise PodcastError(f"ffmpeg could not join the audio: "
                           f"{(p.stderr or '').strip()[:200]}")


def player_html(audio_name: str, title: str, seconds: float,
                audio_url: str = "") -> str:
    """The block injected into the report.

    Self-contained and style-scoped. Agent-authored pages are served with
    'unsafe-inline' for styles (they inline their own CSS), so a <style> block
    here is consistent with that page's policy rather than an exception to it.

    The <audio> element needs `media-src 'self'` in the vault's page CSP. Without
    it the player renders and silently refuses to play — see site/build.py.
    """
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    length = f"{mins}:{secs:02d}" if mins else f"0:{secs:02d}"
    return (
        BLOCK_OPEN + "\n"
        '<style>.atticus-audio{margin:0 0 1.5rem;padding:.9rem 1rem;border-radius:8px;'
        'border:1px solid rgba(127,127,127,.35);background:rgba(127,127,127,.08);'
        'font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}'
        '.atticus-audio .lab{display:block;margin-bottom:.5rem;opacity:.75;'
        'text-transform:uppercase;letter-spacing:.05em;font-size:11px;font-weight:600}'
        '.atticus-audio audio{width:100%;max-width:34rem;display:block}'
        '.atticus-audio .dl{display:inline-block;margin-top:.45rem;font-size:12px;'
        'opacity:.7}'
        '.atticus-audio .atticus-audio-url{font-size:12px;word-break:break-all}'
        '</style>\n'
        '<div class="atticus-audio">'
        f'<span class="lab">Listen &middot; {length}</span>'
        f'<audio controls preload="none" src="{audio_name}"></audio>'
        f'<a class="dl" href="{audio_name}" download>Download the audio</a>'
        # Screen-hidden, print-visible (site/assets/print.css). A PDF of this
        # report is meant to be shared, and a shared PDF that mentions a
        # recording without saying where it lives is a dead reference. The URL is
        # absolute for the same reason — relative to nothing, once it is a file
        # on someone's phone.
        f'<span class="atticus-audio-url">Audio overview ({length}): '
        f'{html.escape(audio_url or audio_name)}</span>'
        '</div>\n' + BLOCK_CLOSE + "\n"
    )


def inject_player(html_path: Path, block: str) -> bool:
    """Put the player near the top of the report, replacing any earlier one.

    Delimited by HTML comments rather than matched on class names, so the whole
    block — its <style> included — can be swapped wholesale. It REPLACES rather
    than skips because the block's markup evolves: when the print-only absolute
    URL was added, every already-published report was carrying a version without
    it, and a skip-if-present rule silently left them that way. Re-running is
    now how you upgrade them.
    """
    text = html_path.read_text(errors="replace")
    existing = _BLOCK.search(text) or _LEGACY_BLOCK.search(text)
    if existing:
        if existing.group(0).strip() == block.strip():
            return False                  # already current; nothing to write
        text = text[:existing.start()] + block + text[existing.end():]
        html_path.write_text(text)
        return True
    m = _BODY_OPEN.search(text)
    text = (text[:m.end()] + "\n" + block + text[m.end():]) if m else block + text
    html_path.write_text(text)
    return True


def primary_html(outdir: Path) -> Path | None:
    """The report the player belongs in — the same choice the site build makes:
    index.html if present, otherwise the largest HTML file."""
    htmls = sorted(outdir.glob("*.html"))
    if not htmls:
        return None
    for h in htmls:
        if h.name == "index.html":
            return h
    return max(htmls, key=lambda p: p.stat().st_size)


def generate(outdir: Path, cfg, *, log=print) -> dict:
    """Voice the script in `outdir`, if there is one. Never raises PodcastError.

    Returns {"made": bool, "reason": str, ...}. `reason` is always populated so
    a skipped episode is explainable from the record alone — "no audio" and "we
    chose not to make audio" must not look the same.
    """
    script = find_script(outdir)
    if not script:
        return {"made": False, "reason": "no script — audio was not requested"}

    raw = script.read_text(errors="replace")
    if len(raw) > MAX_CHARS:
        return {"made": False,
                "reason": f"script is {len(raw):,} chars (limit {MAX_CHARS:,}) — "
                          f"that is a re-narration, not a summary"}

    title, turns = parse_script(raw)
    if len(turns) < 2:
        return {"made": False,
                "reason": f"script has {len(turns)} parsable turn(s); expected "
                          f"lines like '**A:** …' (see skills/podcast-companion)"}

    est = estimate(turns)
    cap = getattr(cfg, "podcast_max_usd", 0.0)
    if cap and est["usd"] > cap:
        return {"made": False, **est,
                "reason": f"estimated ${est['usd']:.4f} exceeds "
                          f"ATTICUS_PODCAST_MAX_USD ${cap:.2f}"}

    report = primary_html(outdir)
    if report is None:
        return {"made": False, **est,
                "reason": "no HTML report to attach the player to"}

    provider = getattr(cfg, "tts_provider", "gemini")
    log(f"    podcast: {est['turns']} turns, ~{est['seconds']:.0f}s, "
        f"~${est['usd']:.4f} estimated, via {provider}")
    dest = outdir / AUDIO_NAME
    measured = {}

    if provider == "gemini":
        try:
            measured = _speak_gemini(turns, cfg, dest)
        except PodcastError as e:
            dest.unlink(missing_ok=True)
            return {"made": False, **est, "reason": str(e)}
    else:
        voices = {"A": cfg.tts_voice_a, "B": cfg.tts_voice_b}
        with tempfile.TemporaryDirectory() as td:
            parts = []
            for i, (speaker, text) in enumerate(turns):
                part = Path(td) / f"{i:04d}.mp3"
                try:
                    _speak(text, voices.get(speaker, cfg.tts_voice_a), cfg, part)
                except PodcastError as e:
                    # Partial audio is worse than none: it stops mid-argument and
                    # sounds like a bug rather than a summary.
                    return {"made": False, **est,
                            "reason": f"turn {i + 1}/{len(turns)} failed: {e}"}
                parts.append(part)
            try:
                _concat(parts, dest)
            except PodcastError as e:
                dest.unlink(missing_ok=True)
                return {"made": False, **est, "reason": str(e)}
        measured = {"calls": len(turns)}

    # The player's running time must be the file's, not the pre-flight guess. The
    # estimate is good (measured within 5% on a real run) but it is still an
    # estimate, and a listener who sees 6:12 and gets 5:40 has been told a small
    # lie by a page whose whole value is being trustworthy. ffprobe is already a
    # hard dependency of the transcribe path, so this costs nothing new.
    actual = au.probe_seconds(dest) or est["seconds"]
    base = (getattr(cfg, "site_base_url", "") or "").rstrip("/")
    stem = outdir.name
    audio_url = f"{base}/docs/{stem}/{AUDIO_NAME}" if base else AUDIO_NAME
    injected = inject_player(report,
                             player_html(AUDIO_NAME, title, actual, audio_url))
    # Cost: Gemini REPORTS what it billed, so use that. OpenAI does not, so it is
    # derived from measured duration. Either way the estimate is kept beside it —
    # the ceiling has to be decided before spending, and keeping both is how a
    # drifting estimate stays visible instead of confirming itself.
    if measured.get("usd") is not None:
        billed = measured["usd"]
    else:
        billed = round((actual / 60.0) * _USD_PER_AUDIO_MINUTE, 6)
    meta = {"made": True, "reason": "ok", **est,
            "audio": AUDIO_NAME, "bytes": dest.stat().st_size,
            "report": report.name, "injected": injected, "title": title,
            "provider": provider,
            "estimated_usd": est["usd"],
            "seconds": round(actual, 1),
            "usd": billed,
            **{k: v for k, v in measured.items() if k != "usd"}}
    (outdir / "podcast.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
