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
    d = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "atticus"
    d.mkdir(parents=True, exist_ok=True)
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
