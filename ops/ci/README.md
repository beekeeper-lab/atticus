# CI

`github-workflow.yml` is the pull-request workflow. It is **not** at
`.github/workflows/ci.yml` in this repo yet, because the token used to land
changes lacks GitHub's `workflow` OAuth scope — pushes that create or modify
workflow files are rejected outright.

To activate it:

```bash
gh auth refresh -s workflow          # one-time, interactive
mkdir -p .github/workflows
git mv ops/ci/github-workflow.yml .github/workflows/ci.yml
git commit -m "Activate CI" && git push
```

## What it checks

| Job | Purpose |
|-----|---------|
| `test` | ruff, then the full pytest suite with `bubblewrap` and `ffmpeg` installed |
| `shell` | shellcheck over `ops/*.sh` |
| `units` | `systemd-analyze verify` on every unit file |
| `secrets` | credential-shaped strings across the **entire history**, not just the diff |

The `test` job additionally asserts that at least eight **security** tests
actually ran. Those tests skip when `bwrap` is unavailable, and a green build
with every isolation test skipped reads as proof while proving nothing — which
is a worse outcome than a red build.
