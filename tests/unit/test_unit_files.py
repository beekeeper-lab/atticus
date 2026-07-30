"""Static guards on the systemd units, for settings whose breakage is invisible.

ProtectKernelTunables is the motivating case. It sat in the processor unit from
the first commit, was never actually LOADED until install.sh rewrote the installed
copy on 2026-07-30 at 11:46, and from that moment every agent execution failed
with "bwrap: Can't mount proc on /newroot/proc: Operation not permitted". The
pipeline still transcribed, gated and routed perfectly, so nothing looked broken
until a real recording reached the execute stage.

No pytest can enable a systemd sandbox on a CI runner, so this asserts the unit
TEXT instead. Cheap, and it fails on the edit rather than on the next recording.
"""
import re
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2] / "ops"


def directives(unit: Path) -> dict[str, str]:
    out = {}
    for line in unit.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def test_processor_does_not_enable_ProtectKernelTunables():
    """It locks mount attributes on /proc, so bwrap cannot mount a fresh procfs
    and the agent sandbox fails to build at all.

    Bisected with systemd-run: baseline rc=0, +ProtectKernelTunables rc=1 with
    that exact error, +ProtectKernelModules rc=0. The trade is not close — the
    setting stops the pipeline writing kernel tunables it never writes, while
    enabling it disables the namespace containing an agent that executes text
    derived from ambient audio.
    """
    d = directives(OPS / "atticus-processor.service")
    val = d.get("ProtectKernelTunables", "no").lower()
    assert val in ("no", "false", "0"), (
        "ProtectKernelTunables is enabled on the processor unit — this breaks "
        "bwrap --proc and disables the agent sandbox entirely")


def test_ingest_may_keep_it_because_it_runs_no_sandbox():
    """Documents that the setting is fine where nothing nests a namespace, so a
    future reader does not 'consistently' strip it from both units."""
    body = (OPS / "atticus-ingest.service").read_text()
    assert "bwrap" not in body


@pytest.mark.parametrize("unit", ["atticus-ingest.service",
                                  "atticus-processor.service"])
def test_units_keep_the_ssh_config_escape(unit):
    """Sandbox options put a user unit in a user namespace where root-owned files
    read as nobody, so ssh rejects /etc/ssh/ssh_config.d/*.conf and every push
    fails. -F makes ssh skip the system config. Removing it broke every push for
    a day while the journal reported success."""
    body = (OPS / unit).read_text()
    assert "GIT_SSH_COMMAND" in body and "-F" in body


@pytest.mark.parametrize("timer", sorted(p.name for p in OPS.glob("*.timer")))
def test_timers_use_wall_clock_schedules(timer):
    """OnUnitActiveSec is monotonic: a daemon-reload loses the reference and
    systemd parks the timer at next_elapse=infinity, where it never fires again
    while still reporting enabled AND active. A timer that never fires cannot
    alarm about not firing. Persistent= is also a no-op on monotonic timers."""
    body = (OPS / timer).read_text()
    active = [ln for ln in body.splitlines()
              if ln.strip().startswith("OnUnitActiveSec")]
    assert not active, f"{timer} uses a monotonic schedule"
    assert re.search(r"^OnCalendar=", body, re.M), f"{timer} has no OnCalendar"


def test_inaccessible_paths_tolerates_absence():
    """Without the leading '-', a host lacking the directory fails namespace
    setup outright (226/NAMESPACE) and every activation dies. install.sh only
    creates it for the ingest role."""
    d = directives(OPS / "atticus-processor.service")
    val = d.get("InaccessiblePaths", "")
    assert val.startswith("-"), (
        "InaccessiblePaths must be prefixed '-' so a missing directory does not "
        "kill every activation on a processor-only host")
