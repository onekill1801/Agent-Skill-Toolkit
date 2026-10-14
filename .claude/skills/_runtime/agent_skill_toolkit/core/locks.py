"""Portable exclusive file lock (checklist R.M3) — the O_EXCL lock-file pattern used by the review queue,
as one primitive: works on Windows / macOS / Linux, survives a crashed holder (stale locks are broken).

    from agent_skill_toolkit.core import locks
    with locks.file_lock(path, timeout=120, stale_after=900):
        ...
"""

import contextlib
import os
import time


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 120, stale_after: float = 900, poll: float = 0.2):
    """Hold `path` exclusively. A lock file older than `stale_after` seconds is treated as left behind by
    a crashed holder and broken. Raises TimeoutError after `timeout` seconds."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after:
                    os.remove(path)
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError(f"lock busy: {os.path.basename(path)}")
            time.sleep(poll)
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
