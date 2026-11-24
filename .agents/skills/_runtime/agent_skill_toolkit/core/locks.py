"""Portable exclusive file lock (checklist R.M3), backed by the OPERATING SYSTEM: `msvcrt.locking` on
Windows, `fcntl.flock` on POSIX. The kernel releases it when the holder exits or crashes, so there is no
"stale lock" to guess about and the lock file is never deleted while anyone may use it (the O_EXCL
create-then-unlink pattern let two waiters break the same stale lock and enter together — Codex R.M3 #1).

    from agent_skill_toolkit.core import locks
    with locks.file_lock(path, timeout=120):
        ...
"""

import contextlib
import os
import time

if os.name == "nt":
    import msvcrt

    def _try_lock(fd) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 120, stale_after: float = None, poll: float = 0.05):
    """Hold `path` exclusively (other processes AND threads with their own handle wait). Raises
    TimeoutError after `timeout` seconds. `stale_after` is accepted for compatibility and ignored: a dead
    holder's lock is released by the OS."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")                       # msvcrt locks a byte range: make sure byte 0 exists
        deadline = time.time() + timeout
        while not _try_lock(fd):
            if time.time() > deadline:
                raise TimeoutError(f"lock busy: {os.path.basename(path)}")
            time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
