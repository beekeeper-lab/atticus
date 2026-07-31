"""The audio-overview stage.

Nothing here touches OpenAI. `_speak` is monkeypatched to write a fake MP3, and
the ffmpeg join is exercised for real only where ffmpeg exists — the point of
these tests is the parsing, the refusals, and the promise that no audio failure
can cost a finished report.
"""
import json
import shutil

import podcast as pod
import pytest

SCRIPT = """# Why the pin will not talk

**A:** The short version is that the device is reachable but silent.
**B:** Reachable how? We can connect to it?
**A:** Connect, read the battery, read the serial. It just ignores commands.
**B:** So what would change your mind about this being closed?
"""


def _write(outdir, script=SCRIPT, html="<html><body><h1>Report</h1></body></html>"):
    outdir.mkdir(parents=True, exist_ok=True)
    if script is not None:
        (outdir / pod.SCRIPT_NAME).write_text(script)
    if html is not None:
        (outdir / "report.html").write_text(html)
    return outdir


# ── parsing ────────────────────────────────────────────────────────────────
def test_parse_extracts_title_and_turns():
    title, turns = pod.parse_script(SCRIPT)
    assert title == "Why the pin will not talk"
    assert len(turns) == 4
    assert turns[0][0] == "A"
    assert turns[1] == ("B", "Reachable how? We can connect to it?")


def test_parse_ignores_prose_around_the_turns():
    _, turns = pod.parse_script(
        "# T\n\nSome note to self.\n\n**A:** One.\n\nMore prose.\n**B:** Two.\n")
    assert [t for _, t in turns] == ["One.", "Two."]


def test_a_bolded_phrase_mid_line_is_not_a_speaker():
    """`**A:**` must be anchored to line start, or a report that quotes a label
    inside a sentence would inject a phantom turn."""
    _, turns = pod.parse_script("# T\n**A:** He said **B:** loudly and left.\n")
    assert len(turns) == 1
    assert turns[0][1] == "He said **B:** loudly and left."


def test_estimate_scales_with_characters():
    small = pod.estimate([("A", "x" * 140)])
    big = pod.estimate([("A", "x" * 1400)])
    assert big["seconds"] == pytest.approx(small["seconds"] * 10)
    assert big["usd"] > small["usd"] > 0


# ── refusals: each must be explained, never silent ─────────────────────────
def test_no_script_means_audio_was_not_requested(tmp_path, cfg):
    res = pod.generate(_write(tmp_path / "o", script=None), cfg)
    assert res["made"] is False
    assert "not requested" in res["reason"]


def test_an_unparsable_script_is_refused_by_name(tmp_path, cfg):
    res = pod.generate(_write(tmp_path / "o", script="# T\n\nJust prose.\n"), cfg)
    assert res["made"] is False
    assert "parsable turn" in res["reason"]
    assert "podcast-companion" in res["reason"], "must point at the contract"


def test_an_oversized_script_is_refused_before_any_spend(tmp_path, cfg, monkeypatch):
    called = []
    monkeypatch.setattr(pod, "_speak", lambda *a, **k: called.append(1))
    res = pod.generate(_write(tmp_path / "o", script="# T\n" + "**A:** x\n" * 30_000), cfg)
    assert res["made"] is False and not called
    assert "re-narration" in res["reason"]


def test_the_per_episode_ceiling_is_checked_before_the_first_request(tmp_path, cfg,
                                                                    monkeypatch):
    """The ceiling exists to prevent spend, so it must gate the FIRST call, not
    be noticed after N turns have already been paid for."""
    called = []
    monkeypatch.setattr(pod, "_speak", lambda *a, **k: called.append(1))
    cfg.podcast_max_usd = 0.000001
    res = pod.generate(_write(tmp_path / "o"), cfg)
    assert res["made"] is False and not called
    assert "ATTICUS_PODCAST_MAX_USD" in res["reason"], "must name the remedy"


def test_no_html_report_means_no_episode(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(pod, "_speak", lambda *a, **k: pytest.fail("must not spend"))
    res = pod.generate(_write(tmp_path / "o", html=None), cfg)
    assert res["made"] is False
    assert "no HTML report" in res["reason"]


def test_a_failed_turn_produces_no_partial_audio(tmp_path, cfg, monkeypatch):
    """Half an episode stops mid-argument and reads as a bug, not a summary."""
    def boom(text, voice, c, dest):
        if "ignores commands" in text:
            raise pod.PodcastError("upstream 503")
        dest.write_bytes(b"\xff\xfbfake")
    monkeypatch.setattr(pod, "_speak", boom)
    outdir = _write(tmp_path / "o")
    res = pod.generate(outdir, cfg)
    assert res["made"] is False
    assert "turn 3/4 failed" in res["reason"] and "503" in res["reason"]
    assert not (outdir / pod.AUDIO_NAME).exists()


# ── the player ─────────────────────────────────────────────────────────────
def test_player_block_is_a_self_contained_fragment():
    block = pod.player_html("podcast.mp3", "T", 372)
    assert "6:12" in block, "length must be human-readable, not raw seconds"
    assert 'src="podcast.mp3"' in block
    assert "download" in block, "a player with no download is a dead end offline"
    assert "<head>" not in block and "<body" not in block, "must splice into a page"


def test_the_player_carries_a_print_only_absolute_url(tmp_path, cfg, monkeypatch):
    """A shared PDF that mentions a recording without saying where it lives is a
    dead reference, and the <audio> control prints as a grey rectangle. So the
    block carries the absolute URL, hidden on screen and shown by print.css."""
    cfg.site_base_url = "http://forge/atticus"
    monkeypatch.setattr(pod, "_speak",
                        lambda t, v, c, d: d.write_bytes(b"\xff\xfbfake"))
    monkeypatch.setattr(pod, "_concat", lambda parts, dest: dest.write_bytes(b"x"))
    monkeypatch.setattr(pod.au, "probe_seconds", lambda p: 372.0)
    outdir = _write(tmp_path / "2026-07-31T135221Z_deadbeef")
    pod.generate(outdir, cfg)
    page = (outdir / "report.html").read_text()
    assert ('http://forge/atticus/docs/2026-07-31T135221Z_deadbeef/podcast.mp3'
            in page)
    assert 'class="atticus-audio-url"' in page


def test_with_no_site_base_url_the_printed_reference_stays_relative(tmp_path, cfg,
                                                                   monkeypatch):
    """Better a relative filename than a fabricated host."""
    cfg.site_base_url = ""
    monkeypatch.setattr(pod, "_speak",
                        lambda t, v, c, d: d.write_bytes(b"\xff\xfbfake"))
    monkeypatch.setattr(pod, "_concat", lambda parts, dest: dest.write_bytes(b"x"))
    outdir = _write(tmp_path / "o")
    pod.generate(outdir, cfg)
    page = (outdir / "report.html").read_text()
    assert "http://" not in page and "https://" not in page
    assert "Audio overview" in page


def test_inject_places_the_player_immediately_after_body(tmp_path):
    p = tmp_path / "r.html"
    p.write_text('<html><head></head><body class="x"><h1>T</h1></body></html>')
    assert pod.inject_player(p, pod.player_html("podcast.mp3", "T", 60)) is True
    text = p.read_text()
    assert text.index("atticus-audio") < text.index("<h1>")
    assert text.index('<body class="x">') < text.index("atticus-audio")


def test_inject_is_idempotent(tmp_path):
    p = tmp_path / "r.html"
    p.write_text("<html><body><h1>T</h1></body></html>")
    block = pod.player_html("podcast.mp3", "T", 60)
    assert pod.inject_player(p, block) is True
    assert pod.inject_player(p, block) is False, "an identical re-run writes nothing"
    assert p.read_text().count('<div class="atticus-audio">') == 1
    assert p.read_text().count("<audio") == 1


def test_an_unfenced_legacy_block_is_replaced_not_duplicated(tmp_path):
    """The first shipped block had no comment fences, and reports published in
    that window are already in the vault. Matching only the fenced form appended
    a SECOND player instead of replacing the first — observed on a real report."""
    legacy = (
        '<style>.atticus-audio{margin:0}</style>\n'
        '<div class="atticus-audio"><span class="lab">Listen &middot; 1:00</span>'
        '<audio controls src="podcast.mp3"></audio></div>\n')
    p = tmp_path / "r.html"
    p.write_text(f"<html><body>\n{legacy}<h1>T</h1></body></html>")
    new = pod.player_html("podcast.mp3", "T", 372, "http://forge/atticus/x.mp3")
    assert pod.inject_player(p, new) is True
    text = p.read_text()
    assert text.count('<div class="atticus-audio">') == 1, "must not stack players"
    assert text.count("<audio") == 1
    assert "1:00" not in text and "6:12" in text
    assert "http://forge/atticus/x.mp3" in text


def test_a_stale_player_block_is_replaced_not_skipped(tmp_path):
    """The block's markup evolves. When the print-only URL was added, every
    already-published report held a version without it — and a skip-if-present
    rule left them that way silently. Re-running must upgrade."""
    p = tmp_path / "r.html"
    p.write_text("<html><body><h1>T</h1><p>keep me</p></body></html>")
    old = pod.player_html("podcast.mp3", "T", 60)
    assert pod.inject_player(p, old) is True
    new = pod.player_html("podcast.mp3", "T", 372, "http://forge/atticus/x.mp3")
    assert pod.inject_player(p, new) is True, "a changed block must be rewritten"
    text = p.read_text()
    assert text.count('<div class="atticus-audio">') == 1, "must replace, not stack"
    assert "http://forge/atticus/x.mp3" in text
    assert "6:12" in text and "1:00" not in text, "the stale length must be gone"
    assert "keep me" in text, "the report itself must survive an upgrade"


def test_inject_survives_html_with_no_body_tag(tmp_path):
    """Agent HTML is not guaranteed well-formed; losing the player is acceptable,
    losing the report is not."""
    p = tmp_path / "r.html"
    p.write_text("<h1>T</h1><p>body-less</p>")
    assert pod.inject_player(p, pod.player_html("podcast.mp3", "T", 60)) is True
    assert "body-less" in p.read_text()


def test_primary_html_prefers_index_then_largest(tmp_path):
    d = tmp_path / "o"
    d.mkdir()
    (d / "small.html").write_text("x")
    (d / "big.html").write_text("x" * 500)
    assert pod.primary_html(d).name == "big.html"
    (d / "index.html").write_text("x")
    assert pod.primary_html(d).name == "index.html"


# ── the happy path ─────────────────────────────────────────────────────────
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_a_real_join_produces_audio_and_a_player(tmp_path, cfg, monkeypatch):
    """End to end with a genuine ffmpeg concat over real (tiny) MP3 frames."""
    silent = _one_frame_mp3()

    def fake(text, voice, c, dest):
        assert voice in (cfg.tts_voice_a, cfg.tts_voice_b)
        dest.write_bytes(silent)
    monkeypatch.setattr(pod, "_speak", fake)

    outdir = _write(tmp_path / "o")
    res = pod.generate(outdir, cfg)
    assert res["made"] is True, res.get("reason")
    assert (outdir / pod.AUDIO_NAME).stat().st_size > 0
    assert "atticus-audio" in (outdir / "report.html").read_text()
    meta = json.loads((outdir / "podcast.json").read_text())
    assert meta["turns"] == 4 and meta["title"] == "Why the pin will not talk"


def test_the_two_hosts_get_different_voices(tmp_path, cfg, monkeypatch):
    seen = {}

    def fake(text, voice, c, dest):
        seen[text[:6]] = voice
        dest.write_bytes(b"\xff\xfbfake")
    monkeypatch.setattr(pod, "_speak", fake)
    monkeypatch.setattr(pod, "_concat", lambda parts, dest: dest.write_bytes(b"x"))
    pod.generate(_write(tmp_path / "o"), cfg)
    assert set(seen.values()) == {cfg.tts_voice_a, cfg.tts_voice_b}


def _one_frame_mp3() -> bytes:
    """A single valid, silent MPEG-1 Layer III frame.

    Built rather than committed as a fixture: 32 kbps mono 44.1 kHz gives a
    104-byte frame, and ffmpeg's concat demuxer needs real frame headers to copy.
    """
    # 0xFF 0xFB = sync + MPEG1 Layer3 no-CRC; 0x90 = 128kbps 44.1k; 0x00 = mono-ish
    header = b"\xff\xfb\x90\x00"
    return (header + b"\x00" * 413) * 4
