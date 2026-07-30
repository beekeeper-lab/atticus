"""Coverage for ops/retention.py — the one job here that DELETES user audio, so
its gates matter more than most. All hermetic: a plain temp vault with no .git,
so commit_push() is a no-op and nothing touches git or the network."""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


retention = _load("atticus_retention", "ops/retention.py")

INBOX = ("inbox", "2026", "07")


def _vault(tmp_path) -> Path:
    v = tmp_path / "vault"
    (v.joinpath(*INBOX)).mkdir(parents=True)
    return v


def _write(vault: Path, stem: str, *, status="published",
           recorded_at="2020-01-01T00:00:00Z", audio=True, **extra) -> Path:
    d = vault.joinpath(*INBOX)
    meta = {"plaud_id": "p_" + stem, "recorded_at": recorded_at,
            "audio_filename": f"{stem}.mp3", "status": status}
    meta.update(extra)
    (d / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n")
    if audio:
        (d / f"{stem}.mp3").write_bytes(b"\xff\xfb\x90\x00" + b"\0" * 512)
    return d / f"{stem}.json"


def _run(monkeypatch, vault: Path, *args) -> int:
    monkeypatch.setenv("ATTICUS_VAULT_PATH", str(vault))
    monkeypatch.setattr(sys, "argv", ["retention.py", *args])
    return retention.main()


def _mp3(vault: Path, stem: str) -> Path:
    return vault.joinpath(*INBOX, f"{stem}.mp3")


def _meta(vault: Path, stem: str) -> dict:
    return json.loads(vault.joinpath(*INBOX, f"{stem}.json").read_text())


def test_only_published_audio_is_expired(monkeypatch, tmp_path):
    vault = _vault(tmp_path)
    _write(vault, "pub", status="published")
    _write(vault, "inflight", status="transcribed")

    assert _run(monkeypatch, vault, "--days", "1") == 0

    assert not _mp3(vault, "pub").exists()          # finished → gone
    assert _mp3(vault, "inflight").exists()          # still in flight → kept
    assert _meta(vault, "pub").get("audio_expired_at")


def test_dry_run_deletes_nothing(monkeypatch, tmp_path):
    vault = _vault(tmp_path)
    _write(vault, "pub", status="published")

    assert _run(monkeypatch, vault, "--dry-run", "--days", "1") == 0

    assert _mp3(vault, "pub").exists()
    assert "audio_expired_at" not in _meta(vault, "pub")


def test_already_expired_is_idempotent(monkeypatch, tmp_path):
    vault = _vault(tmp_path)
    _write(vault, "pub", status="published",
           audio_expired_at="2021-01-01T00:00:00Z")

    assert _run(monkeypatch, vault, "--days", "1") == 0

    # A record already carrying audio_expired_at is skipped untouched.
    assert _mp3(vault, "pub").exists()
    assert _meta(vault, "pub")["audio_expired_at"] == "2021-01-01T00:00:00Z"


def test_naive_recorded_at_does_not_crash_or_delete(monkeypatch, tmp_path, capsys):
    vault = _vault(tmp_path)
    _write(vault, "pub", status="published",
           recorded_at="2020-01-01T00:00:00")     # no timezone

    assert _run(monkeypatch, vault, "--days", "1") == 0

    assert _mp3(vault, "pub").exists()             # unparseable → not expired
    assert "unparseable recorded_at" in capsys.readouterr().err


def test_negative_days_is_rejected(monkeypatch, tmp_path):
    vault = _vault(tmp_path)
    _write(vault, "pub", status="published")

    # A negative window puts the cutoff in the future and would delete ALL
    # published audio at once — must be refused before touching anything.
    assert _run(monkeypatch, vault, "--days", "-5") == 2
    assert _mp3(vault, "pub").exists()


def test_missing_audio_is_marked_not_skipped(monkeypatch, tmp_path):
    vault = _vault(tmp_path)
    _write(vault, "pub", status="published", audio=False)

    assert _run(monkeypatch, vault, "--days", "1") == 0

    meta = _meta(vault, "pub")
    assert meta.get("audio_missing") is True
    assert meta.get("audio_expired_at")


def test_refuses_when_vault_path_unset(monkeypatch, tmp_path):
    # Config would fall back to REPO/.scratch-vault and report "nothing to
    # expire" against the wrong tree — refuse instead.
    monkeypatch.delenv("ATTICUS_VAULT_PATH", raising=False)
    monkeypatch.setattr(retention, "_parse_env", lambda p: {})
    monkeypatch.setattr(sys, "argv", ["retention.py", "--days", "1"])
    assert retention.main() == 2


def test_recent_published_audio_is_KEPT(monkeypatch, tmp_path):
    """The other direction, which had no test at all.

    Every existing case used recorded_at=2020 against --days 1, so a units bug
    (days read as hours, an inverted comparison, bad cutoff arithmetic) that
    over-deleted would have passed the whole suite. This is the one job here that
    destroys user data; the boundary deserves both sides.
    """
    from datetime import UTC, datetime, timedelta
    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    vault = _vault(tmp_path)
    _write(vault, "keep-me", recorded_at=recent)

    assert _run(monkeypatch, vault, "--days", "30") == 0
    assert (vault.joinpath(*INBOX) / "keep-me.mp3").exists(), \
        "audio inside the retention window must survive"
    meta = json.loads((vault.joinpath(*INBOX) / "keep-me.json").read_text())
    assert "audio_expired_at" not in meta


def test_the_cutoff_is_days_not_hours(monkeypatch, tmp_path):
    """A 5-day-old recording must survive --days 30 and die at --days 1."""
    from datetime import UTC, datetime, timedelta
    five = (datetime.now(UTC) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    vault = _vault(tmp_path)
    _write(vault, "five-days", recorded_at=five)

    assert _run(monkeypatch, vault, "--days", "30") == 0
    assert (vault.joinpath(*INBOX) / "five-days.mp3").exists()

    assert _run(monkeypatch, vault, "--days", "1") == 0
    assert not (vault.joinpath(*INBOX) / "five-days.mp3").exists()
