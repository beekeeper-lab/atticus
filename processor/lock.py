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
def single_instance(name: str, log=print, vault=None):
    # THE VAULT FIRST, when we know it. Everything below is a fallback, and the
    # fallbacks proved insufficient in production on 2026-07-30:
    #
    #   the unit has PrivateTmp=yes and NO XDG_RUNTIME_DIR in its Environment,
    #   so a timed pass locked /tmp/atticus-<uid>/processor.lock inside its OWN
    #   private /tmp, while a manual pass locked
    #   $XDG_RUNTIME_DIR/atticus/processor.lock. Two different files, no mutual
    #   exclusion, and the 15:05 and 15:10 timer passes both walked into a record
    #   a manual pass was actively executing.
    #
    # The vault is the contended resource and is visible identically to every
    # participant regardless of PrivateTmp, RuntimeDirectory or which shell
    # launched the process. .git/ keeps it out of the working tree, so `add -A`
    # can never stage it. The docstring above already required the location be
    # "STABLE and shared"; only this actually is.
    candidates = []
    if vault is not None:
        v = Path(vault)
        if (v / ".git").is_dir():
            candidates.append(v / ".git")
        else:
            candidates.append(v)        # local-only vault (tests)
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
    path = d / f"atticus-{name}.lock" if d.name == ".git" else d / f"{name}.lock"
    # "a" not "w": opening for write TRUNCATES the holder's recorded pid before
    # we know whether we can take the lock, so a failed acquisition used to wipe
    # the very information the error message wants.
    fh = path.open("a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = (fh.read() or "").strip().splitlines()
            who = f" (held by {holder[0]})" if holder else ""
            raise AlreadyRunning(
                f"another {name} pass holds {path}{who} — exiting rather than "
                f"racing it")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} on {os.uname().nodename}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
