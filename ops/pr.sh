#!/usr/bin/env bash
# Land a change on main through a PR. Never push to main directly.
#
#   ./ops/pr.sh "Short title" ["longer body, optional"]
#
# Because two machines edit this repo, every change goes: pull latest →
# branch → commit → push → PR → squash-merge → back to main. No approval
# needed; the point is the pull-and-merge discipline, not review.
#
# REFUSES TO RUN IN THE VAULT. atticus-vault is machine-written every few
# minutes from two hosts and is the pipeline's queue — a PR per commit would
# add a merge step to every message and stall the handoff. That repo uses
# pull --rebase + bounded retry instead (processor/vault.py).
set -euo pipefail

die(){ printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*" >&2; }

TITLE="${1:-}"; BODY="${2:-}"
[[ -n "$TITLE" ]] || die 'usage: ops/pr.sh "Short title" ["body"]'

cd "$(git rev-parse --show-toplevel)" || die "not in a git repo"

# --- guardrails -------------------------------------------------------------
if [[ -f README.md ]] && grep -qi 'machine-written' README.md 2>/dev/null; then
  die "this looks like atticus-vault. The vault does NOT use PRs — see the
       header of this script. Commit directly; the safe-push wrapper handles
       concurrency."
fi
[[ -d inbox && -d processed ]] && die "vault layout detected — refusing to run here"

command -v gh >/dev/null || die "gh not installed"
gh auth status >/dev/null 2>&1 || die "gh not authenticated"

BASE=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name 2>/dev/null || echo main)

# --- refuse to lose work ----------------------------------------------------
git add -A
if git diff --cached --quiet; then
  die "nothing staged — no changes to land"
fi

# Never let a credential or an ignored path through, whatever .gitignore says.
# The guard itself lives in ops/lib/check-staged.sh so it can be unit-tested —
# inline here it was only reachable by landing a real PR, which is why a security
# control shipped untested. See tests/unit/test_check_staged.py.
# shellcheck source=ops/lib/check-staged.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/check-staged.sh"
if ! check_staged "$(git rev-parse --show-toplevel)"; then
  git reset -q
  die "refusing to land. Unstaged; inspect with: git diff"
fi

echo "Landing on $BASE via PR"

# --- pull latest first ------------------------------------------------------
STASH=""
if ! git diff --cached --quiet; then
  git stash push -q --staged -m "pr.sh-$$" && STASH=1
fi
git checkout -q "$BASE"
git pull -q --rebase origin "$BASE" || die "pull failed — resolve by hand"
ok "pulled latest $BASE"

SLUG=$(printf '%s' "$TITLE" | tr '[:upper:]' '[:lower:]' \
       | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' | cut -c1-48)
BRANCH="change/${SLUG:-update}-$(git rev-parse --short HEAD)"
git checkout -q -b "$BRANCH"
[[ -n "$STASH" ]] && git stash pop -q
git add -A
ok "branch $BRANCH"

git commit -q -m "$TITLE" ${BODY:+-m "$BODY"} \
  -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -q -u origin "$BRANCH"
ok "pushed"

URL=$(gh pr create --base "$BASE" --head "$BRANCH" \
        --title "$TITLE" --body "${BODY:-$TITLE}" 2>&1 | tail -1)
ok "PR $URL"

# --auto, so the merge waits for required status checks instead of racing them.
# This used to squash-merge immediately in the statement after `gh pr create`,
# which meant every CI job in ci.yml — ruff, pytest, the security-tests-must-run
# gate, shellcheck, unit verification, the full-history secret scan — ran AFTER
# the change was already on main. CI gated nothing.
#
# --auto requires branch protection with required checks on $BASE; without it
# GitHub rejects the flag, so fall back to an immediate merge and say so rather
# than leaving the PR silently open.
if gh pr merge "$BRANCH" --squash --delete-branch --auto >/dev/null 2>&1; then
  ok "queued for auto-merge once checks pass: $URL"
  echo "   (it will land on $BASE by itself; this checkout stays on $BASE)"
else
  warn "auto-merge unavailable (no required checks configured on $BASE) —"
  warn "merging now, so CI results arrive after the fact. Enable branch"
  warn "protection with required status checks to make CI actually gate."
  gh pr merge "$BRANCH" --squash --delete-branch >/dev/null 2>&1 \
    || die "merge failed — the PR is open at $URL, resolve there"
  ok "squash-merged"
fi

git checkout -q "$BASE"
git pull -q --rebase origin "$BASE"
git branch -q -D "$BRANCH" 2>/dev/null || true
# --delete-branch removes it on the remote, but the local tracking ref lingers
# and accumulates one stale entry per PR. Prune it.
git fetch -q --prune
ok "back on $BASE at $(git rev-parse --short HEAD)"
