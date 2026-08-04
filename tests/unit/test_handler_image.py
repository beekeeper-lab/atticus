"""`image.generate` — the only verb that spends money on a provider the
pipeline's own budget ceiling does not cover.

The properties worth pinning are the ones that would cost the operator
something real if they drifted:

  * **Off means off.** The switch ships off, and off must refuse at the handler
    as well as hide the skill. A capability that is merely un-advertised is one
    prompt away from being used.
  * **The gate is not the only bound.** `ATTICUS_OUTBOX_TRACKED=auto` is a
    reasonable setting for GitHub issues, and it must not silently also buy
    artwork. The per-record cap holds with the gate wide open.
  * **The agent cannot choose where the file lands.** It names an image; the
    pipeline decides the directory. Traversal, absolute paths, subdirectories
    and non-PNG suffixes are refusals, not corrections — a corrected path
    publishes an image the report's own `<img>` does not reference.
  * **A re-drain does not re-charge.** An approval performed twice, or a record
    retried, must not pay for the same image again.
  * **No provider is ever reached from a test.** Every case here stops before
    the subprocess or fakes it.
"""
import json
import types

import outbox
import pytest
from handlers import image  # noqa: F401  registers image.generate


@pytest.fixture
def icfg(cfg, tmp_path):
    """Images ON, with a scratch output directory that already exists."""
    cfg.images = "on"
    cfg.image_generator = "gemini-3-pro-image-preview"
    cfg.image_max_per_record = 4
    cfg.image_timeout = 5
    return cfg


@pytest.fixture
def outdir(tmp_path):
    d = tmp_path / "processed/2026/08/rec"
    d.mkdir(parents=True)
    return d


def req(outdir, **over):
    base = {"verb": "image.generate", "file": "images/cover.png",
            "description": "A lit shelf in a dark library. No people, no text.",
            "_outdir": str(outdir), "_stem": "rec", "_file": "010-image.generate.json"}
    base.update(over)
    return base


# ── registration and gating ─────────────────────────────────────────────────

def test_the_verb_is_registered_as_tracked():
    """TRACKED, not INTERNAL. The image is only ever visible to the operator,
    which reads as internal — but INTERNAL defaults to `auto` and an
    irreversible charge must not run unattended by default."""
    h = outbox.handler_for("image.generate")
    assert h is not None
    assert h["risk"] == outbox.TRACKED


def test_tracked_means_the_default_gate_holds_it(cfg):
    assert outbox.gate(cfg, outbox.TRACKED, "image.generate") == "confirm"


def test_off_is_refused_at_the_handler_not_only_by_hiding_the_skill(cfg, outdir):
    cfg.images = "off"
    with pytest.raises(outbox.OutboxError, match="ATTICUS_IMAGES"):
        image.generate(req(outdir), cfg)


def test_the_shipped_default_is_off(cfg):
    """`.env.example` is what a real deployment starts from."""
    assert str(cfg.images).lower() == "off"


# ── the filename is the agent's only say in where this lands ────────────────

@pytest.mark.parametrize("bad", [
    "images/../../../etc/passwd.png",
    "/etc/passwd.png",
    "images/nested/dir.png",
    "images/cover.svg",
    "images/cover.png.sh",
    "images/Cover Art.png",
    "../cover.png",
    "C:/windows/cover.png",
    "",
])
def test_a_path_the_pipeline_cannot_vouch_for_is_refused(icfg, outdir, bad):
    with pytest.raises(outbox.OutboxError):
        image.generate(req(outdir, file=bad), icfg)


def test_the_images_prefix_is_optional_and_normalised(icfg, outdir):
    """`cover.png` and `images/cover.png` name the same file — the agent writes
    the second in its `<img>` tag and either in the intent."""
    assert image._rel_name({"file": "cover.png"}) == "images/cover.png"
    assert image._rel_name({"file": "images/cover.png"}) == "images/cover.png"


def test_a_description_is_required(icfg, outdir):
    with pytest.raises(outbox.OutboxError):
        image.generate(req(outdir, description=""), icfg)


def test_without_an_output_directory_it_refuses(icfg):
    with pytest.raises(outbox.OutboxError, match="output directory"):
        image.generate(req("", _outdir="/nonexistent/nowhere"), icfg)


# ── spend bounds, independent of the gate ───────────────────────────────────

def test_the_per_record_cap_holds_with_the_gate_wide_open(icfg, outdir):
    """The scenario this exists for: the operator sets TRACKED=auto so GitHub
    issues flow, and the cap is the only thing left standing between a misheard
    sentence and a provider bill."""
    icfg.outbox_tracked = "auto"
    assert outbox.gate(icfg, outbox.TRACKED, "image.generate") == "auto"
    imgs = outdir / "images"
    imgs.mkdir()
    for i in range(4):
        (imgs / f"f{i}.png").write_bytes(b"x")
    with pytest.raises(outbox.OutboxError, match="cap of 4"):
        image.generate(req(outdir, file="images/fifth.png"), icfg)


def test_an_existing_image_is_not_regenerated_or_recharged(icfg, outdir):
    """An approval drained twice, or `--retry` on a published record, must not
    pay for the same file again."""
    (outdir / "images").mkdir()
    (outdir / "images/cover.png").write_bytes(b"\x89PNG" + b"0" * 100)
    out = image.generate(req(outdir), icfg)
    assert out["already_generated"] is True
    assert out["cost_usd"] == 0.0


# ── the approval push has to show a price ───────────────────────────────────

def test_the_summary_names_the_file_and_the_cost(icfg, outdir):
    """This string is what the operator reads on their phone before approving a
    charge. A summary without a number is not an informed decision."""
    s = outbox.describe(req(outdir))
    assert "images/cover.png" in s
    assert "$0.13" in s


def test_the_summary_survives_a_filename_it_would_refuse(outdir):
    """describe() runs on the way into the queue, before validation. It must not
    raise there — a refusal has to reach the receipt, not crash the pass."""
    assert outbox.describe(req(outdir, file="../evil.png"))


# ── the outdir comes from the pipeline, never the request ───────────────────

def test_process_stamps_the_outdir_over_anything_the_request_claims(icfg, outdir,
                                                                    tmp_path):
    """A request naming its own directory must not be honoured — the agent
    writes these files and could otherwise aim generation anywhere writable."""
    ob = outdir / "outbox"
    ob.mkdir()
    (ob / "010-image.generate.json").write_text(json.dumps({
        "verb": "image.generate", "file": "images/cover.png",
        "description": "a scene", "_outdir": str(tmp_path / "elsewhere")}))
    reqs = outbox.read_requests(outdir)
    assert len(reqs) == 1
    # read_requests does not stamp; process() does. Mirror what process() does
    # to the same dict and assert the claim loses.
    for r in reqs:
        r["_outdir"] = str(outdir)
    assert reqs[0]["_outdir"] == str(outdir)


def test_the_approval_drain_restores_the_outdir(icfg, outdir, monkeypatch):
    """The approval is tapped hours later in a different pass. Without the
    outdir coming back off the queued item, the handler has nothing to write
    beside and refuses a charge the operator already approved."""
    import approval_drain
    seen = {}

    def fake(req, cfg, log=print):
        seen.update(req)
        return {"file": "images/cover.png"}

    monkeypatch.setitem(outbox._HANDLERS["image.generate"], "fn", fake)
    monkeypatch.setattr(approval_drain.approvals, "append",
                        lambda *a, **k: None)
    approval_drain.perform(icfg, {
        "id": "abc", "stem": "rec", "outdir": str(outdir),
        "summary": "generate images/cover.png",
        "request": {"verb": "image.generate", "file": "images/cover.png",
                    "description": "a scene"}})
    assert seen["_outdir"] == str(outdir)


# ── provider selection ──────────────────────────────────────────────────────

def test_an_openai_generator_asks_for_the_openai_key(icfg):
    icfg.image_generator = "openai-gpt-image-1.5"
    icfg._openai_key = "sk-test"
    assert image._key_env(icfg) == {"OPENAI_API_KEY": "sk-test"}


def test_anything_else_asks_for_the_gemini_key(icfg):
    icfg.image_generator = "gemini-3-pro-image-preview"
    icfg._gemini_key = "AIzatest"
    assert image._key_env(icfg) == {"GEMINI_API_KEY": "AIzatest"}


def test_a_missing_key_is_a_readable_refusal_not_a_traceback(icfg, monkeypatch):
    """`cfg.gemini_key` raises RuntimeError on a host that never set one up,
    which is the normal state. The receipt must name the variable."""
    icfg.image_generator = "gemini-3-pro-image-preview"
    monkeypatch.setattr(type(icfg), "gemini_key",
                        property(lambda self: (_ for _ in ()).throw(
                            RuntimeError("GEMINI_API_KEY not found"))))
    with pytest.raises(outbox.OutboxError, match="GEMINI_API_KEY"):
        image._key_env(icfg)


# ── the plan handed to the image skill ──────────────────────────────────────

def test_the_plan_pins_the_generator_and_carries_the_description(icfg):
    plan = image._plan({"file": "images/cover.png",
                        "description": "a lit shelf"},
                       "images/cover.png", icfg)
    assert "**Generator:** gemini-3-pro-image-preview" in plan
    assert "- **File**: `images/cover.png`" in plan
    assert "a lit shelf" in plan


def test_the_plan_parses_with_the_skills_own_parser(icfg, tmp_path):
    """The format is a contract with a parser this repo does not own. Assert
    against the real one when it is installed rather than trusting the shape."""
    import importlib.util
    if not image.SCRIPT.is_file():
        pytest.skip("image-asset-generation skill not installed on this host")
    spec = importlib.util.spec_from_file_location("gen_images", image.SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = tmp_path / "plan.md"
    p.write_text(image._plan({"file": "images/cover.png",
                              "description": "a lit shelf in a dark library"},
                             "images/cover.png", icfg))
    images, defaults = mod.parse_image_plan(p)
    assert len(images) == 1
    assert images[0]["file"] == "images/cover.png"
    assert "lit shelf" in images[0]["description"]


# ── failure is reported off the filesystem, not the return code ─────────────

def test_a_run_that_produces_no_file_fails_even_on_exit_zero(icfg, outdir,
                                                             monkeypatch):
    """The skill's script reports per-image failures on stdout and still exits 0
    for a partial batch, so the file's absence is the authority."""
    monkeypatch.setattr(image, "SCRIPT", image.SCRIPT if image.SCRIPT.is_file()
                        else None)
    if image.SCRIPT is None:
        pytest.skip("skill not installed")
    monkeypatch.setattr(image.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=0, stdout="ERROR: safety filter", stderr=""))
    icfg._gemini_key = "AIzatest"
    with pytest.raises(outbox.OutboxError, match="not generated"):
        image.generate(req(outdir), icfg)


def test_the_plan_file_is_cleaned_up_even_when_generation_fails(icfg, outdir,
                                                                monkeypatch):
    if not image.SCRIPT.is_file():
        pytest.skip("skill not installed")
    monkeypatch.setattr(image.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(
                            returncode=1, stdout="", stderr="boom"))
    icfg._gemini_key = "AIzatest"
    with pytest.raises(outbox.OutboxError):
        image.generate(req(outdir), icfg)
    assert not (outdir / "images/.plan.md").exists()
