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

# Never let a credential through, whatever .gitignore says.
if git diff --cached | grep -qE '(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[osu]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'; then
  git reset -q
  die "credential-shaped string in the staged diff — refusing. Unstaged; inspect with: git diff"
fi
for f in $(git diff --cached --name-only); do
  case "$f" in
    ops/.env|*/ops/.env|.env|docs/recon/*|.scratch-vault/*)
      git reset -q; die "refusing to commit $f" ;;
  esac
done

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

gh pr merge "$BRANCH" --squash --delete-branch >/dev/null 2>&1 \
  || die "merge failed — the PR is open at $URL, resolve there"
ok "squash-merged"

git checkout -q "$BASE"
git pull -q --rebase origin "$BASE"
git branch -q -D "$BRANCH" 2>/dev/null || true
# --delete-branch removes it on the remote, but the local tracking ref lingers
# and accumulates one stale entry per PR. Prune it.
git fetch -q --prune
ok "back on $BASE at $(git rev-parse --short HEAD)"
