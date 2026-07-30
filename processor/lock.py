"""Single-instance lock.

systemd will not start a unit that is already running, which covers the timers.
It does not cover a manual run racing a timed one, a second host with the vault
mounted, or an operator debugging while the timer fires. Two passes that both
observe a record as `raw` before either commits will both execute it — running
the same spoken command twice, with whatever external side effects that carries.

Advisory, non-blocking, and released when the process dies (including on kill -9,
since the kernel drops flock on close). A stale lock file is therefore harmless.
"""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(name: str, log=print):
    # Under ProtectSystem=strict the runtime dir is read-only unless the unit
    # declares RuntimeDirectory=, so fall back rather than crash. The fallback
    # must be STABLE and shared, or a manual run and a timed run would take
    # different locks and the race this exists to prevent would still happen.
    candidates = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(os.environ["XDG_RUNTIME_DIR"]) / "atticus")
    candidates.append(Path(f"/tmp/atticus-{os.getuid()}"))  # noqa: S108

    d = None
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".w"
            probe.touch()
            probe.unlink()
            d = c
            break
        except OSError:
            continue
    if d is None:
        raise RuntimeError(f"no writable location for the {name} lock")
    path = d / f"{name}.lock"
    fh = path.open("w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning(
                f"another {name} pass holds {path} — exiting rather than "
                f"racing it")
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
