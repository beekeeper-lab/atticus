# CI

The workflow lives at [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)
and runs on every pull request.

It was parked here briefly because GitHub rejects workflow-file pushes from an
OAuth token lacking the `workflow` scope. The fix was not to refresh the token:
that restriction applies to **OAuth over HTTPS**, and an SSH push is not subject
to it. `origin` now fetches over HTTPS and pushes over SSH.

## What it checks

| Job | Purpose |
|-----|---------|
| `test` | ruff, the full pytest suite, and the generated config docs, with `bubblewrap` and `ffmpeg` installed |
| `shell` | shellcheck over `ops/*.sh` |
| `units` | `systemd-analyze verify` on every unit file |
| `secrets` | credential-shaped strings across the **entire history**, not just the diff |

The `test` job additionally asserts that at least eight **security** tests
actually ran. Those tests skip when `bwrap` is unavailable, and a green build
with every isolation test skipped reads as proof while proving nothing — a worse
outcome than a red build.
