# shellcheck shell=bash
#
# The pre-commit guard for this repo, extracted from ops/pr.sh so it can be
# tested. It was previously inline in a linear script that pushes and merges, so
# the only way to exercise it was to land a real PR — which meant this security
# control shipped with zero coverage. See tests/unit/test_check_staged.py.
#
# Usage:  check_staged <repo-dir>   → 0 = clean, 1 = refuse (reason on stderr)
#
# Deliberately does NOT unstage anything. The caller decides how to react; a
# library that mutates the caller's index cannot be tested without side effects.

# Credential shapes. The original set covered OpenAI and GitHub and missed the
# credentials this project actually handles: the Plaud workspace token is a
# Bearer JWT (eyJ…) harvested off a live request and the primary secret in the
# whole design, and ATTICUS_NOTIFY_URL is an ntfy topic capability — the one real
# secret in ops/.env — which anyone holding it can use to read alarms carrying
# transcript fragments, or to send spoofed ones.
CHECK_STAGED_CRED_RE='(sk-[A-Za-z0-9_-]{20,}'
CHECK_STAGED_CRED_RE+='|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[osu]_[A-Za-z0-9]{20,}'
CHECK_STAGED_CRED_RE+='|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'
CHECK_STAGED_CRED_RE+='|A(KIA|SIA)[0-9A-Z]{16}'
CHECK_STAGED_CRED_RE+='|xox[abprs]-[A-Za-z0-9-]{10,}'
CHECK_STAGED_CRED_RE+='|https?://ntfy\.sh/[A-Za-z0-9_-]{8,}'
CHECK_STAGED_CRED_RE+='|-----BEGIN [A-Z ]*PRIVATE KEY-----)'

check_staged() {
  local repo="${1:-.}"

  if git -C "$repo" diff --cached | grep -qE "$CHECK_STAGED_CRED_RE"; then
    echo "credential-shaped string in the staged diff" >&2
    return 1
  fi

  # NUL-delimited. The original `for f in $(git diff --cached --name-only)` split
  # on whitespace, so a path containing a space was tested as two nonexistent
  # paths and slipped the guard entirely; git also C-quotes non-ASCII names in
  # normal output, which defeated the case match outright.
  local f
  while IFS= read -r -d '' f; do
    case "$f" in
      ops/.env|*/ops/.env|.env|docs/recon/*|.scratch-vault/*)
        echo "refusing to commit $f" >&2
        return 1 ;;
    esac
  done < <(git -C "$repo" diff --cached --name-only -z)

  return 0
}
