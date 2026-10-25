"""The run lock: fcntl.flock on $REPO/.sync.lock (spec 6.5)."""
from __future__ import annotations

import fcntl
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from claude_sync.log import Log

LEGACY_STALE_SECONDS = 900
POLL_SECONDS = 0.2


def _legacy_busy(legacy: Path, log: Log) -> bool:
    """True while an old-code run may hold the lock directory; removes a stale one."""
    try:
        age = time.time() - os.lstat(legacy).st_mtime
    except FileNotFoundError:
        return False
    if age < LEGACY_STALE_SECONDS:
        return True
    log.warn("removing stale legacy lock dir")
    shutil.rmtree(legacy, ignore_errors=True)
    return legacy.exists()


def acquire(repo: Path, wait: float, log: Log) -> Optional[int]:
    repo = Path(repo)
    legacy = repo / ".sync.lock.d"
    fd = os.open(repo / ".sync.lock", os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait
    try:
        while True:
            if not _legacy_busy(legacy, log):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(POLL_SECONDS, remaining))
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    log.info("lock busy; skipping")
    return None
