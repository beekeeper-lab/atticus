"""B3/B4 — malformed records are surfaced, never silently skipped, and paths
cannot escape the record's own directory."""
import json
import pytest
from conftest import write_record
from vault import MalformedRecord, load_records


def test_good_record_loads(cfg):
    write_record(cfg.vault)
    assert len(load_records(cfg.vault)) == 1


def test_corrupt_json_is_reported_not_skipped(cfg):
    write_record(cfg.vault)
    d = cfg.vault / "inbox/2026/07"
    (d / "broken.json").write_text("{not json")
    bad = []
    recs = load_records(cfg.vault, on_bad=lambda p, e: bad.append(p))
    assert len(recs) == 1 and len(bad) == 1


def test_corrupt_json_raises_when_no_handler(cfg):
    write_record(cfg.vault)
    (cfg.vault / "inbox/2026/07/broken.json").write_text("{nope")
    with pytest.raises(json.JSONDecodeError):
        load_records(cfg.vault)


@pytest.mark.parametrize("name", [
    "../../../etc/passwd", "sub/dir/a.mp3", "..", ".", "",
])
def test_path_escape_refused(cfg, name):
    write_record(cfg.vault, stem="bad", audio_filename=name)
    bad = []
    load_records(cfg.vault, on_bad=lambda p, e: bad.append(e))
    assert bad and isinstance(bad[0], MalformedRecord)


def test_missing_required_field_refused(cfg):
    write_record(cfg.vault, stem="x", plaud_id="")
    bad = []
    load_records(cfg.vault, on_bad=lambda p, e: bad.append(e))
    assert bad
