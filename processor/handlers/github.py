"""GitHub through `gh`: file an issue, comment on one. Nothing else (issue #50).

## Why this is deliberately the narrowest thing that works

`gh` on this host is authenticated as a **write-capable token on the operator's own
account**, and `ops/pr.sh` depends on that same auth, so it cannot be quietly
downgraded to something weaker without breaking how this repo ships. That token can
push branches, merge pull requests, dispatch workflows and edit repository
settings.

The thing on the other end of it is a pipeline that executes text derived from
ambient audio.

So this module does **not** shell out to `gh` generically, and there is no verb
that takes a `gh` command line. Two verbs exist, each building a fixed argv:

    github.issue     gh issue create  --repo <allowlisted> --title … --body …
    github.comment   gh issue comment <n> --repo <allowlisted> --body …

Adding `github.pr`, `github.merge` or `github.run` here is not an incremental
change; it is a different risk decision and belongs in its own issue with its own
credential. Reads are absent for a different reason: an outbox cannot answer a
question, because the answer would have to arrive *during* the agent's run. See the
module docstring of `processor/outbox.py`.

## The repository allowlist is the control

`ATTICUS_GITHUB_REPOS` is where the target comes from. A request may *select* from
it; it can never *name* a repo. That distinction is the whole security design of
this handler, so it is worth stating why the obvious alternative is wrong:

If the handler trusted `req["repo"]`, then the sentence that decides where an issue
is filed is a sentence a microphone picked up. "File an issue on atticus" mishears
into any number of things, and prompt text reaching the agent from a fetched web
page could name a repository outright. Either way the token can reach every
repository on the account — including private ones and other people's, through org
membership — so a bad string is not a bad issue, it is an issue filed somewhere the
operator will never look, on a repo they may not own.

With the allowlist, the worst case of a completely wrong repo string is a *refused*
request with a receipt naming what was permitted. The blast radius is the list the
operator wrote down, and nothing spoken can extend it.

Two supporting details matter and are easy to lose:

  * `--repo` is passed **explicitly on every call**, so the process's working
    directory can never decide the target. The pipeline's cwd is a git checkout,
    and `gh` would happily infer a repo from it.
  * Allowlist entries are themselves validated against `owner/name`. They come
    from the operator, not the agent, but an entry like `--json` would otherwise be
    handed to `gh` in argv position where it reads as a flag.
"""
import re
import subprocess

import outbox
from outbox import OutboxError

# owner/name, GitHub's own character set. Anchored, so no path traversal, no
# leading dash that `gh` would read as a flag, no whitespace.
_NWO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_ISSUE_NO = re.compile(r"^#?(\d{1,7})$")
_URL_IN = re.compile(r"https://\S+/(\d+)\s*$")

# Bounds on what one utterance can commit to somebody else's issue tracker. A
# transcript is at most ATTICUS_MAX_COMMAND_CHARS, but an agent can elaborate
# freely, and an issue nobody can read is nearly as useless as no issue.
MAX_TITLE = 250
MAX_BODY = 60_000

# Provenance, appended to everything filed. Anyone reading the issue is entitled to
# know a machine wrote it from a voice command rather than a person typing — that is
# most of what makes TRACKED an acceptable risk class for this at all.
FOOTER = ("\n\n---\n*Filed by [Atticus](https://github.com/beekeeper-lab/atticus) "
          "from a voice command. The recording, its transcript and the intent file "
          "that produced this are in the operator's vault.*\n")


def _allowlist(cfg) -> list[str]:
    repos = [str(r).strip() for r in (getattr(cfg, "github_repos", None) or [])]
    if not repos:
        raise OutboxError(
            "GitHub is not configured: ATTICUS_GITHUB_REPOS is empty, so no "
            "repository may be written to. Set it in ops/.env to a comma-separated "
            "owner/name allowlist.")
    bad = [r for r in repos if not _NWO.match(r)]
    if bad:
        raise OutboxError(
            f"ATTICUS_GITHUB_REPOS has entries that are not owner/name: "
            f"{', '.join(bad)} — fix ops/.env; nothing was sent to gh")
    return repos


def _repo(req: dict, cfg) -> str:
    """Resolve the target repository from CONFIG, using the request only to pick.

    A bare name ("atticus") is accepted because that is what a person says out
    loud, but it is matched against the allowlist rather than combined with an
    owner — so `evil-org/atticus` cannot arrive by being spoken, and an owner the
    operator never listed cannot be reached even if the name half matches.
    """
    allowed = _allowlist(cfg)
    asked = str(req.get("repo") or "").strip().strip("/")
    if not asked:
        return allowed[0]
    low = asked.lower()
    hits = [r for r in allowed
            if r.lower() == low or r.split("/", 1)[1].lower() == low]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise OutboxError(
            f"{asked!r} is not a permitted repository. ATTICUS_GITHUB_REPOS allows: "
            f"{', '.join(allowed)}")
    raise OutboxError(
        f"{asked!r} is ambiguous — it matches {' and '.join(hits)}. Say the full "
        f"owner/name.")


def _why(p: subprocess.CompletedProcess) -> str:
    """`gh`'s own diagnosis, trimmed to one line, with a hint where we know one.

    The operator reads this in a receipt, not a log, so "exit status 1" is not an
    answer and a stack trace is not either.
    """
    lines = [ln.strip() for ln in ((p.stderr or "") + "\n" + (p.stdout or "")).splitlines()
             if ln.strip()]
    msg = lines[0][:200] if lines else f"exit status {p.returncode}"
    low = " ".join(lines).lower()
    if "gh auth login" in low or "authentication" in low or "not logged" in low:
        msg += " — gh is not authenticated for the user running the processor"
    elif "not found" in low and "label" in low:
        msg += " — every label in ATTICUS_GITHUB_LABELS must already exist in the repo"
    elif "not found" in low:
        msg += " — the token cannot see that repository, or it does not exist"
    return msg


def _gh(cfg, args: list[str], *, log=print) -> str:
    """Run one fixed `gh` invocation. Never a shell, never a caller-built string."""
    exe = str(getattr(cfg, "gh_bin", "gh") or "gh")
    timeout = int(getattr(cfg, "github_timeout", 60) or 60)
    # The argv is logged WITHOUT the body: the operator wants to see which repo was
    # touched, and the body is already in the intent file and the receipt.
    log(f"      gh {' '.join(args[:2])} on {args[args.index('--repo') + 1]}"
        if "--repo" in args else f"      gh {' '.join(args[:2])}")
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True,
                           timeout=timeout, check=False)
    except FileNotFoundError:
        raise OutboxError(f"{exe!r} is not installed on this host (ATTICUS_GH_BIN)")
    except subprocess.TimeoutExpired:
        raise OutboxError(f"gh {' '.join(args[:2])} timed out after {timeout}s")
    if p.returncode != 0:
        raise OutboxError(f"gh {' '.join(args[:2])} failed: {_why(p)}")
    return (p.stdout or "").strip()


def _url_and_number(out: str) -> dict:
    """`gh` prints the issue URL on stdout. Keep both so the receipt is clickable."""
    for ln in reversed(out.splitlines()):
        m = _URL_IN.search(ln.strip())
        if m:
            return {"url": ln.strip(), "number": int(m.group(1))}
    return {"url": out.splitlines()[-1].strip() if out.strip() else ""}


def _body(text: str) -> str:
    body = str(text or "").strip()
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n*(truncated)*"
    return body + FOOTER


@outbox.handler(
    "github.issue", risk=outbox.TRACKED, schema=("title",),
    describe=lambda r: (f"file a GitHub issue on "
                        f"{r.get('repo') or 'the default repo'}: {r.get('title')}"))
def create_issue(req: dict, cfg, log=print) -> dict:
    """File one issue. `title` required; `body` and `repo` optional.

    TRACKED, not OUTWARD: an issue is visible to other people but it is exactly
    what an issue tracker is for, it is editable, and it can be closed. It is still
    held for confirmation by default (ATTICUS_OUTBOX_TRACKED).
    """
    repo = _repo(req, cfg)
    title = str(req.get("title") or "").strip().replace("\n", " ")[:MAX_TITLE]
    args = ["issue", "create", "--repo", repo, "--title", title,
            "--body", _body(req.get("body"))]
    for label in (getattr(cfg, "github_labels", None) or []):
        args += ["--label", str(label)]
    return {"repo": repo, **_url_and_number(_gh(cfg, args, log=log))}


@outbox.handler(
    "github.comment", risk=outbox.TRACKED, schema=("issue", "body"),
    describe=lambda r: (f"comment on {r.get('repo') or 'the default repo'} "
                        f"issue #{str(r.get('issue')).lstrip('#')}"))
def add_comment(req: dict, cfg, log=print) -> dict:
    """Comment on one existing issue. `issue` and `body` required.

    The number must be a number. There is no read path (see `outbox`), so the agent
    cannot look an issue up by title, and accepting anything looser here would mean
    guessing which thread a misheard sentence meant.
    """
    repo = _repo(req, cfg)
    m = _ISSUE_NO.match(str(req.get("issue") or "").strip())
    if not m:
        raise OutboxError(
            f"github.comment needs an issue NUMBER, got {req.get('issue')!r}. "
            f"There is no way to look one up from here.")
    args = ["issue", "comment", str(int(m.group(1))), "--repo", repo,
            "--body", _body(req.get("body"))]
    # The comment URL carries an #issuecomment anchor, so keep the ISSUE number we
    # were given rather than whatever trailing digits the URL happens to end in.
    return {"repo": repo, **_url_and_number(_gh(cfg, args, log=log)),
            "number": int(m.group(1))}
