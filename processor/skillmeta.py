"""What a skill declares about itself, and whether this host can offer it.

Issue #89. A skill's frontmatter used to carry a name and a description; risk
class, required credentials and refusal behaviour all lived in the handler.
That is the right place for *enforcement* and the wrong place for *routing*,
and the gap had two visible costs:

  * **Every unconfigured skill still advertised itself.** Slack ships with an
    empty token, so the agent would route "post to Slack", compose a request,
    write a report saying it was pending — and the handler would refuse after
    the fact. On a fresh install that is the normal state of every skill. The
    operator reads a confident report about an action that never had a chance.

  * **The description could contradict the handler and nothing noticed.**
    `github.close` shipped on 2026-08-02 with the verb implemented, the skill
    body documenting it, and the description still saying "Do NOT use it to …
    close anything". The agent obeyed the description and refused, correctly.
    Routing reads the description; a capability the description denies does not
    exist, however well the body documents it.

So skills now declare `verbs`, `requires`, `risk`, `outputs` and `cost`, and
two things consume that: `execute.py` hides a skill whose requirements are not
met, and the test suite asserts the declarations and the handlers agree.

## Why a hand-rolled parser

The frontmatter is a fenced block of `key: value` and `key: [a, b]` lines.
Adding PyYAML for that would put a dependency in the processor's import path
for the sake of ten lines, on a host where the pipeline's whole appeal is that
it is stdlib plus `requests`. The parser below understands exactly the shapes
this project uses and ignores everything else — including the multi-line
`description: |` block, which is prose and is never read as data.
"""
import re
from pathlib import Path

# Only these keys are interpreted. Anything else in the frontmatter is prose or
# forward-compatibility and is deliberately ignored rather than rejected.
LIST_KEYS = ("verbs", "requires", "outputs")
STR_KEYS = ("name", "risk", "cost")

_FENCE = re.compile(r"^---\s*$")
_KV = re.compile(r"^([a-z_]+)\s*:\s*(.*)$")


def parse(text: str) -> dict:
    """The declared metadata of one SKILL.md. Never raises.

    A malformed block yields whatever was parseable. That is deliberate: this
    feeds a filter that can HIDE a skill, and a parser that threw on a stray
    character would disable a working capability over a typo. Fail open, and
    let the tests catch the typo.
    """
    lines = text.splitlines()
    if not lines or not _FENCE.match(lines[0]):
        return {}
    body = []
    for ln in lines[1:]:
        if _FENCE.match(ln):
            break
        body.append(ln)

    out: dict = {}
    for ln in body:
        # Continuation lines of a block scalar (description: |) are indented;
        # skipping them keeps prose out of the metadata.
        if ln.startswith((" ", "\t")):
            continue
        m = _KV.match(ln)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        # Strip a trailing YAML comment. " #" rather than "#" so a value that
        # legitimately contains a hash — a channel name, a fragment — survives.
        raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        if key in LIST_KEYS:
            out[key] = [v.strip().strip("'\"") for v in
                        raw.strip("[]").split(",") if v.strip()]
        elif key in STR_KEYS:
            out[key] = raw.strip("'\"")
    return out


def read(skill_dir: Path) -> dict:
    try:
        return parse((skill_dir / "SKILL.md").read_text(errors="replace"))
    except OSError:
        return {}


def missing_requirements(meta: dict, cfg) -> list[str]:
    """Which of a skill's `requires:` config keys are empty on this host.

    Named by their ENVIRONMENT VARIABLE, because that is what the operator sets
    and what every error message in this project already says. The lookup maps
    `ATTICUS_SLACK_BOT_TOKEN` to `cfg.slack_bot_token`, which is the convention
    config.py already follows without exception.
    """
    missing = []
    for env in meta.get("requires") or []:
        attr = env.lower().removeprefix("atticus_")
        val = getattr(cfg, attr, None)
        if val is None or val == "" or val == [] or val == {}:
            missing.append(env)
        # A SWITCH set to off counts as unset. Meeting mode ships "off"
        # (ADR-008), and a non-empty string that means "no" would otherwise
        # read as configured and offer the agent a capability the operator
        # deliberately declined.
        elif isinstance(val, str) and val.strip().lower() in (
                "off", "false", "no", "0"):
            missing.append(env)
    return missing


def offerable(skills_dir: Path, cfg) -> tuple[list[Path], list[tuple[str, list[str]]]]:
    """(skill directories to copy, [(skipped name, what it needed)]).

    A skill with no `requires:` is always offered — that covers every skill
    written before this existed, and every one that genuinely needs nothing.
    """
    keep, skipped = [], []
    if not skills_dir.is_dir():
        return keep, skipped
    for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        meta = read(d)
        gaps = missing_requirements(meta, cfg)
        if gaps:
            skipped.append((meta.get("name") or d.name, gaps))
        else:
            keep.append(d)
    return keep, skipped
