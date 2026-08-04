"""`image.generate` — an illustration for the report the agent just wrote.

## Why this is a verb and not a skill the agent runs

The operator's `~/.claude/skills/image-asset-generation` is a real, working
capability, and the obvious move was to add it to `ATTICUS_GLOBAL_SKILLS` and
let the agent call it. That fails twice, and both failures are the point of
this file:

  * **The sandbox has no key and must not get one.** `agent_env()` is an
    allowlist — LANG, LC_ALL, TZ, TERM, HOME, PATH, ATTICUS_OUTPUT_DIR and the
    agent's own OAuth token. Its docstring records that `OPENAI_API_KEY` used to
    reach the agent and that this was treated as the bug it was. A provider key
    inside the namespace is a spending credential inside the blast radius of
    anything spoken near the pin.

  * **The skill's own gate cannot close here.** It enforces plan → approval →
    execute and says so in capitals: never call a provider without explicit
    approval *in the current conversation*. A pipeline run has no conversation.
    The agent would either honour the gate and generate nothing, or ignore it
    and spend unattended.

So the work splits at the intent boundary the outbox already draws. The agent
writes `<img src="images/foo.png">` into its HTML and an intent file beside it;
this handler — outside the sandbox, holding the key — generates the file. The
approval queue supplies the human the skill's gate was asking for, and it
arrives out of band by push (ADR-009) rather than through any surface the agent
can reach.

## Spend is bounded twice, deliberately

`TRACKED` risk means the gate defaults to `confirm`, so nothing generates until
the operator taps approve. But `ATTICUS_OUTBOX_TRACKED=auto` is a setting a
reasonable person sets to let GitHub issues flow unattended, and that would
otherwise also open image spend — precisely the over-granting that per-verb
overrides exist to prevent. A gate is therefore not enough on its own, and
`MAX_PER_RECORD` bounds the images one recording can produce **regardless of the
gate**, counted off the filesystem so it holds across an approval drain that
re-enters this handler one request at a time.

## Generation is not reimplemented here

The provider calls, the retry-on-429 backoff, the rate limiter, the cost table
and the skip-if-exists idempotency all live in the skill's `generate_images.py`.
This handler writes a one-image plan and shells out to it. Reimplementing any of
that would give the pipeline a second copy to drift against the operator's own,
which is the thing the global-skills split in `skills/README.md` exists to stop.
"""
import os
import re
import subprocess
from pathlib import Path

from outbox import TRACKED, OutboxError, handler

# The skill's own headline figure ("Even one image is $0.13"). An estimate shown
# in the approval push, not a billing record — the operator is approving a spend
# and must see a number before tapping, even an approximate one.
IMAGE_COST_USD = 0.13

SKILL_DIR = Path.home() / ".claude/skills/image-asset-generation"
SCRIPT = SKILL_DIR / "scripts/generate_images.py"

# Ceiling on images per recording, enforced here and not only by the gate. See
# the module docstring: opening the TRACKED class for GitHub must not also open
# unbounded spend.
MAX_PER_RECORD = 4

IMAGES_SUBDIR = "images"
MAX_DESCRIPTION = 2000

# A deliberately narrow name. The agent names this file and the pipeline writes
# it OUTSIDE the sandbox, so the usual traversal defences are not enough on
# their own — an absolute path, a `..`, a symlink-shaped name or a suffix that
# is not an image all have to be impossible rather than merely unlikely.
_STEM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _s(req: dict, field: str) -> str:
    return str(req.get(field) or "").strip()


def _rel_name(req: dict) -> str:
    """The image's path relative to the report, validated to `images/<stem>.png`.

    Returned rather than joined so the caller decides the root; every rejection
    below is a refusal to write, never a silent rewrite into somewhere safe. A
    corrected path would publish an image under a name the agent's own `<img>`
    tag does not reference, which reads as a generation failure and is harder to
    diagnose than the refusal.
    """
    raw = _s(req, "file")
    if not raw:
        raise OutboxError("image.generate needs a 'file'")
    raw = raw.replace("\\", "/")
    if raw.startswith(f"{IMAGES_SUBDIR}/"):
        raw = raw[len(IMAGES_SUBDIR) + 1:]
    if raw.startswith("/") or ":" in raw:
        raise OutboxError(f"image file must be relative, got {raw!r}")
    if "/" in raw:
        raise OutboxError(
            f"image file must sit directly in {IMAGES_SUBDIR}/, got {raw!r}")
    if not raw.lower().endswith(".png"):
        raise OutboxError(f"image file must end in .png, got {raw!r}")
    if not _STEM.match(raw.lower()):
        raise OutboxError(
            f"image file must be lowercase letters, digits, dot, dash or "
            f"underscore, got {raw!r}")
    return f"{IMAGES_SUBDIR}/{raw.lower()}"


def _describe(req: dict) -> str:
    try:
        rel = _rel_name(req)
    except OutboxError:
        rel = _s(req, "file") or "(unnamed)"
    desc = _s(req, "description")
    short = (desc[:80] + "…") if len(desc) > 80 else desc
    return f"generate {rel} (~${IMAGE_COST_USD:.2f}) — {short}"


def _plan(req: dict, rel: str, cfg) -> str:
    """A one-image ad-hoc plan in the format `generate_images.py` already parses.

    Style lives in the plan's frontmatter because that is where the script looks
    for it, and pinning the generator here rather than letting the agent pick one
    keeps a project's images on one provider — the skill's own consistency rule.
    """
    style = _s(req, "style") or (
        "clean technical illustration, flat colours, generous whitespace, "
        "restrained palette, no photorealism")
    desc = _s(req, "description")[:MAX_DESCRIPTION]
    generator = str(getattr(cfg, "image_generator", "") or
                    "gemini-3-pro-image-preview").strip()
    return "\n".join((
        "# Image Plan — atticus",
        "",
        f"**Style:** {style}",
        f"**Generator:** {generator}",
        "**Aspect ratio:** 16:9",
        "**Background:** white",
        "**Text in image:** minimal",
        "**Avoid:** photorealistic, dark, cluttered, watermarks, lorem ipsum",
        "",
        "---",
        "",
        "## Section 1: report illustration",
        "",
        "### Image 1: atticus",
        f"- **File**: `{rel}`",
        f"- **Description**: {desc}",
        "",
    ))


def _key_env(cfg) -> dict:
    """The one credential this generation needs, and nothing else.

    `cfg.openai_key` / `cfg.gemini_key` raise when the shared credential file has
    no key, which is the normal state on a host that never set one up. Translated
    to OutboxError so the receipt says which variable is missing instead of
    carrying a traceback into the operator's report.
    """
    generator = str(getattr(cfg, "image_generator", "") or "").strip().lower()
    provider = "openai" if generator.startswith("openai") else "gemini"
    var = "OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"
    try:
        key = cfg.openai_key if provider == "openai" else cfg.gemini_key
    except RuntimeError as e:
        raise OutboxError(str(e))
    if not key:
        raise OutboxError(f"{var} is not configured")
    return {var: key}


def _already_generated(images_dir: Path) -> int:
    if not images_dir.is_dir():
        return 0
    return len([p for p in images_dir.glob("*.png") if p.is_file()])


@handler("image.generate", risk=TRACKED, schema=("file", "description"),
         describe=_describe)
def generate(req: dict, cfg, log=print) -> dict:
    """Generate one image into the record's own output directory.

    TRACKED rather than INTERNAL: the image is only ever visible to the operator,
    which reads as internal, but INTERNAL defaults to `auto` and an irreversible
    charge must not run unattended by default. The class is chosen for the gate it
    implies, and the module docstring records that the gate alone is not relied on.
    """
    if str(getattr(cfg, "images", "off") or "off").strip().lower() in (
            "off", "false", "no", "0", ""):
        raise OutboxError("image generation is off — set ATTICUS_IMAGES=on")

    outdir = Path(str(req.get("_outdir") or "")).expanduser()
    if not str(outdir) or not outdir.is_dir():
        raise OutboxError(
            "no output directory for this record — image.generate can only "
            "illustrate a report the same recording produced")

    rel = _rel_name(req)
    if not _s(req, "description"):
        raise OutboxError("image.generate needs a 'description'")
    if not SCRIPT.is_file():
        raise OutboxError(f"the image-asset-generation skill is not installed "
                          f"at {SKILL_DIR}")

    images_dir = outdir / IMAGES_SUBDIR
    target = outdir / rel

    if target.exists():
        # The script skips existing files, but saying so here makes a re-drain
        # of an already-performed approval report the truth rather than claiming
        # a fresh generation and a fresh charge.
        log(f"    image {rel} already generated")
        return {"file": rel, "bytes": target.stat().st_size,
                "already_generated": True, "cost_usd": 0.0}

    cap = int(getattr(cfg, "image_max_per_record", MAX_PER_RECORD) or 0)
    if cap and _already_generated(images_dir) >= cap:
        raise OutboxError(
            f"per-record image cap of {cap} reached — raise "
            f"ATTICUS_IMAGE_MAX_PER_RECORD to allow more")

    env = {**{k: v for k, v in os.environ.items()
              if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ")},
           **_key_env(cfg)}
    images_dir.mkdir(parents=True, exist_ok=True)
    plan = images_dir / ".plan.md"
    plan.write_text(_plan(req, rel, cfg))

    cmd = [str(getattr(cfg, "uv_bin", "") or "uv"), "run",
           "--with", "google-genai", "--with", "openai",
           "python", str(SCRIPT),
           "--plan", str(plan), "--images-dir", str(images_dir)]
    timeout = int(getattr(cfg, "image_timeout", 300) or 300)
    log(f"    generating {rel} …")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=str(images_dir))
    except FileNotFoundError:
        raise OutboxError("uv is not on PATH — needed to run the image skill")
    except subprocess.TimeoutExpired:
        raise OutboxError(f"image generation timed out after {timeout}s")
    finally:
        plan.unlink(missing_ok=True)

    if not target.exists():
        # The script reports per-image failures on stdout and still exits 0 for
        # a partial batch, so the file's absence is the authority on whether this
        # worked — not the return code.
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        raise OutboxError("image was not generated"
                          + (f": {' / '.join(tail)}" if tail else ""))

    size = target.stat().st_size
    log(f"    ✓ {rel} ({size} bytes)")
    return {"file": rel, "bytes": size, "cost_usd": IMAGE_COST_USD}
