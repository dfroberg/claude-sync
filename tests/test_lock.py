from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from claude_sync.lock import acquire
from claude_sync.log import Log
from tests.harness import WORKTREE, SyncTestCase, base_env

HOLDER_CHECK = (
    "import sys; from pathlib import Path; from claude_sync.lock import acquire; from claude_sync.log import Log; "
    "r = Path(sys.argv[1]); fd = acquire(r, float(sys.argv[2]), Log(r / 'child.log', 'test', True)); "
    "sys.exit(3 if fd is None else 0)"
)
HOLDER_SLEEP = (
    "import sys, time; from pathlib import Path; from claude_sync.lock import acquire; from claude_sync.log import Log; "
    "r = Path(sys.argv[1]); fd = acquire(r, 0, Log(r / 'child.log', 'test', True)); "
    "print('locked' if fd is not None else 'busy', flush=True); time.sleep(60)"
)


class LockTest(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "lockrepo"
        self.repo.mkdir()
        self.log = Log(self.root / "test.log", "test", True)

    def child(self, code, *args, **kw):
        return subprocess.Popen([sys.executable, "-c", code, str(self.repo), *args], cwd=str(WORKTREE),
                                env=base_env(self.root), **kw)

    def log_text(self):
        return (self.root / "test.log").read_text() if (self.root / "test.log").exists() else ""

    def test_second_acquire_waits_then_skips(self):
        fd = acquire(self.repo, 0, self.log)
        self.assertIsNotNone(fd)
        self.addCleanup(os.close, fd)
        t = time.monotonic()
        r = subprocess.run([sys.executable, "-c", HOLDER_CHECK, str(self.repo), "0.5"], cwd=str(WORKTREE),
                           env=base_env(self.root))
        self.assertEqual(r.returncode, 3)
        self.assertGreaterEqual(time.monotonic() - t, 0.5)
        self.assertIn("lock busy; skipping", (self.repo / "child.log").read_text())

    def test_wait_zero_still_tries_once(self):
        fd = acquire(self.repo, 0, self.log)
        self.addCleanup(os.close, fd)
        self.assertIsNone(acquire(self.repo, 0, self.log))
        self.assertIn("INFO lock busy; skipping", self.log_text())

    def test_released_when_fd_closed(self):
        fd = acquire(self.repo, 0, self.log)
        os.close(fd)
        fd2 = acquire(self.repo, 0, self.log)
        self.assertIsNotNone(fd2)
        os.close(fd2)

    def test_released_on_sigkill(self):
        p = self.child(HOLDER_SLEEP, stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        self.assertIsNone(acquire(self.repo, 0, self.log))
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        fd = acquire(self.repo, 2, self.log)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_legacy_dir_young_busy_old_removed(self):     # spec test 19, unit half
        legacy = self.repo / ".sync.lock.d"
        legacy.mkdir()
        self.assertIsNone(acquire(self.repo, 0, self.log))
        self.assertTrue(legacy.is_dir())
        old = time.time() - 1000
        os.utime(legacy, (old, old))
        fd = acquire(self.repo, 0, self.log)
        self.assertIsNotNone(fd)
        os.close(fd)
        self.assertFalse(legacy.exists())
        self.assertIn("WARN removing stale legacy lock dir", self.log_text())
