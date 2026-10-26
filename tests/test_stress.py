from __future__ import annotations

import os
import signal
import subprocess
import time

from tests.harness import SyncTestCase, wait_for

LOCAL = "projects/-Users-alice-p/memory/"      # under ~/.claude
REPO = "memory/-Users-__USER__-p/"             # under data/
PENDING = "refs/claude-sync/pending"
POINTS = ["pull:before-replace", "pull:before-reset", "pull:before-base",
          "push:before-commit", "push:before-push", "push:after-push"]


class StressTest(SyncTestCase):
    def reap(self, p: subprocess.Popen) -> None:
        """Kill a child that is still running and collect it; used with addCleanup."""
        if p.poll() is None:
            p.kill()
        p.communicate()

    def spawn(self, m, *args: str, env=None) -> subprocess.Popen:
        p = m.popen(*args, env=env)
        self.addCleanup(self.reap, p)
        return p

    def test_concurrent_sessions(self):                      # spec test 13
        a, b = self.synced("a", "b")
        rels = [LOCAL + "e0.md", LOCAL + "e1.md", "skills/s/e2.md"]
        editor = a.popen_editor(rels, rounds=40)
        self.addCleanup(self.reap, editor)
        env = a.env(CLAUDE_SYNC_LOCK_WAIT="60")
        procs = [self.spawn(a, cmd, env=env) for cmd in ["pull", "push"] * 4]
        b_procs = []                      # the other machine keeps the remote moving
        for i in range(2):
            b.write(LOCAL + f"b{i}.md", f"from b {i}\n")
            b_procs.append(self.spawn(b, "push"))
        for p in procs + b_procs:
            out, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, f"stdout: {out}\nstderr: {err}\nlog a:\n" + "\n".join(a.log_lines())
                             + "\nlog b:\n" + "\n".join(b.log_lines()))
        editor.communicate(timeout=120)
        self.assertEqual(editor.returncode, 0)
        a.run("push")
        b.run("pull")
        for rel in rels:
            self.assertEqual(b.read(rel), a.read(rel), rel)
            self.assertIn("round 39", a.read(rel))
        for i in range(2):
            self.assertEqual(a.read(LOCAL + f"b{i}.md"), f"from b {i}\n")
        self.assertEqual(a.git("rev-parse", "HEAD"), a.base())
        self.assertEqual(a.git("status", "--porcelain", "--", "data"), "")
        self.assertIsNone(a.ref(PENDING))
        self.assertFalse((a.repo / ".sync.lock.d").exists())

    def test_sigkill_at_each_point(self):                    # spec test 14 and 27 (kill variant)
        times = {}
        for i, point in enumerate(POINTS):
            with self.subTest(point=point):
                t0 = time.monotonic()
                a, b = self.synced(f"a{i}", f"b{i}")
                rel_local, local_text = LOCAL + f"local{i}.md", f"local edit {i}\n"
                rel_remote, remote_text = LOCAL + f"remote{i}.md", f"remote change {i}\n"
                self.remote_commit({REPO + f"remote{i}.md": remote_text})
                a.write(rel_local, local_text)
                hold = a.repo / f".hold-{point}"
                p = self.spawn(a, point.split(":")[0], env=a.env(CLAUDE_SYNC_TEST_HOLD=point))
                wait_for(hold)
                os.kill(p.pid, signal.SIGKILL)
                p.communicate(timeout=30)
                hold.unlink()
                self.assertEqual(p.returncode, -signal.SIGKILL)
                landed = REPO + f"local{i}.md" in self.remote_paths()
                self.assertEqual(landed, point == "push:after-push")       # the kill hit the named point
                if point == "pull:before-replace":
                    self.assertIsNone(a.read(rel_remote))
                self.assertEqual(a.run("pull").returncode, 0)
                self.assertEqual(a.run("push").returncode, 0)
                self.assertEqual([x for x in (a.home / ".claude").rglob("*")
                                  if x.is_file() and x.stat().st_size == 0], [])
                b.run("pull")
                self.assertEqual(b.read(rel_local), local_text)          # no edit lost
                self.assertEqual(a.read(rel_local), local_text)
                self.assertEqual(a.read(rel_remote), remote_text)        # the remote change arrived
                self.assertEqual(a.git("rev-parse", "HEAD"), a.base())
                self.assertEqual(a.git("status", "--porcelain", "--", "data"), "")
                self.assertIsNone(a.ref(PENDING))
                if point == "push:after-push":
                    self.assertEqual(a.conflicts(), [])           # a landed push is not seen as a remote change
                times[point] = round(time.monotonic() - t0, 1)
        print(f"kill-point times: {times}")

    def test_pull_skips_when_lock_held(self):
        a, = self.synced("a")
        a.write(LOCAL + "mine.md", "mine\n")
        hold = a.repo / ".hold-push:before-commit"
        holder = self.spawn(a, "push", env=a.env(CLAUDE_SYNC_TEST_HOLD="push:before-commit"))
        wait_for(hold)                    # the push now holds the lock
        self.remote_commit({REPO + "theirs.md": "theirs\n"})
        t0 = time.monotonic()
        r = a.run("pull", env=a.env(CLAUDE_SYNC_LOCK_WAIT="2"))
        elapsed = time.monotonic() - t0
        self.assertEqual(r.returncode, 0)
        self.assertLess(elapsed, 5)
        self.assertGreaterEqual(elapsed, 1.5)                    # it did wait the 2 s
        self.assertEqual(r.stdout, "")
        self.assertTrue(any(" pull INFO lock busy" in ln for ln in a.log_lines()), a.log_lines())
        self.assertIsNone(a.read(LOCAL + "theirs.md"))           # the skipped pull applied nothing
        hold.unlink()
        out, err = holder.communicate(timeout=90)
        self.assertEqual(holder.returncode, 0, f"stdout: {out}\nstderr: {err}")
        a.run("pull")
        self.assertEqual(a.read(LOCAL + "theirs.md"), "theirs\n")
        self.assertEqual(a.read(LOCAL + "mine.md"), "mine\n")
