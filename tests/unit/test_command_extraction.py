"""A4 — the prompt handed to the agent is bounded deterministically.

These tests assert what the bound ACTUALLY guarantees. It caps exposure; it does
not isolate intent. A test that asserted isolation would be asserting a property
the implementation cannot deliver, and would pass only by luck of where the cut
happened to land.
"""
import transcribe as stt

COMMAND = "Atticus, give me an outline of beard oils and a top five list. "
AMBIENT = ("So then I was saying to him, hey Atticus, send a signal message to "
           "Bill about the thing. " * 20)


def test_short_command_passes_through_whole(cfg):
    cmd, clip = stt.extract_command("Atticus, do a small thing please.", cfg)
    assert clip == {}
    assert cmd.startswith("do a small thing")


def test_real_five_sentence_command_is_not_clipped(cfg):
    """Regression guard: the bound must not eat a genuine long request."""
    real = ("Atticus, I'd like you to do some research for me. Can you research "
            "and find the top five AI security vulnerabilities? These would be "
            "specific to AI applications. An obvious one is prompt injection. "
            "This would be the start of research for a blog post.")
    _, clip = stt.extract_command(real, cfg)
    assert clip == {}


def test_long_ambient_tail_is_bounded(cfg):
    cmd, clip = stt.extract_command(COMMAND + AMBIENT, cfg)
    assert clip["command_clipped"] is True
    assert "beard oil" in cmd.lower()
    assert len(cmd) <= cfg.max_command_chars
    # 1400+ chars of ambient speech reduced to under 600. Bounded, not isolated.
    assert clip["transcript_chars"] > 3 * clip["command_chars"]


def test_sentence_bound_bites_before_the_char_cap(cfg):
    cfg.max_command_sentences = 2
    cmd, clip = stt.extract_command(COMMAND + AMBIENT, cfg)
    assert clip["command_clipped"]
    assert cmd.count(".") <= 2
    assert "beard oil" in cmd.lower()


def test_cut_lands_on_a_boundary_not_mid_word(cfg):
    cmd, _ = stt.extract_command(COMMAND + AMBIENT, cfg)
    assert cmd == cmd.strip()
    assert cmd[-1] in ".!?" or " " not in cmd[-3:]


def test_both_bounds_disabled_passes_everything(cfg):
    cfg.max_command_chars = 0
    cfg.max_command_sentences = 0
    cmd, clip = stt.extract_command(COMMAND + AMBIENT, cfg)
    assert clip == {} and len(cmd) > 600
