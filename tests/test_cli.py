from __future__ import annotations

import fcntl
import os
import re
import time
import unittest
from typing import List, Tuple

from tests.harness import SyncTestCase, wait_for

LOCAL = "projects/-Users-alice-p/memory/"      # under ~/.claude
REPO = "memory/-Users-__USER__-p/"             # under data/
PENDING = "refs/claude-sync/pending"
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ pid=\d+ (pull|push|status|dry-run) "
                      r"(DEBUG|INFO|WARN|ERROR) ")
KEYS = ["base", "head", "origin", "pending", "fresh-machine marker", "local changes",
        "conflict folders", "last"]


def fields(stdout: str) -> List[Tuple[str, str]]:
    return [tuple(line.split(": ", 1)) for line in stdout.splitlines()]      # type: ignore[misc]


class CliTest(SyncTestCase):
    def kill_at(self, m, point: str, *args: str) -> None:
        hold = m.repo / f".hold-{point}"
        p = m.popen(*args, env=m.env(CLAUDE_SYNC_TEST_HOLD=point))
        try:
            wait_for(hold)
        finally:
            p.kill()
            p.communicate()

    def age_dir(self, path, days: float) -> None:
        t = time.time() - days * 86400
        os.utime(path, (t, t))

    def aged_folders(self, m, old: str = "2020-01-01T00-00-00Z", young: str = "2999-01-01T00-00-00Z"):
        for top in ("conflicts", "trash"):
            for stamp, days in ((old, 31), (young, 29)):
                d = m.repo / top / stamp
                d.mkdir(parents=True)
                (d / "x.md").write_text("x\n")
                self.age_dir(d, days)

    def break_remote(self, m) -> None:
        m.git("remote", "set-url", "origin", str(self.root / "missing.git"))

    # status

    def test_status_fields(self):
        a, = self.synced("a")
        rel = LOCAL + "n.md"
        log_before = a.log_lines()
        head, base = a.git("rev-parse", "HEAD"), a.base()
        r = a.run("status")
        got = fields(r.stdout)
        self.assertEqual([k for k, _ in got], KEYS)
        values = dict(got)
        self.assertEqual(values["base"], base)
        self.assertEqual(values["head"], head)
        self.assertEqual(values["origin"], self.remote_tip())
        self.assertEqual(values["pending"], "no")
        self.assertEqual(values["fresh-machine marker"], "no")
        self.assertEqual(values["local changes"], "0 changed, 0 deleted")
        self.assertEqual(values["conflict folders"], "0")
        self.assertIn(" pull INFO pull done in ", values["last"])
        self.assertEqual(a.log_lines(), log_before)                      # status writes nothing
        self.assertEqual((a.base(), a.git("rev-parse", "HEAD")), (base, head))
        a.write(rel, "new\n")
        a.write("CLAUDE.md", "# shared\nedit\n")
        self.remote_commit({"skills/s/gone.md": "g\n"})
        a.run("pull")
        os.unlink(a.claude / "skills/s/gone.md")
        (a.repo / ".fresh-machine").touch()
        (a.repo / "conflicts" / "2026-01-01T00-00-00Z").mkdir(parents=True)
        a.git("update-ref", PENDING, a.base())
        values = dict(fields(a.run("status").stdout))
        self.assertEqual(values["local changes"], "2 changed, 1 deleted")
        self.assertEqual(values["fresh-machine marker"], "yes")
        self.assertEqual(values["conflict folders"], "1")
        self.assertEqual(values["pending"], "yes")

    def test_status_with_no_base_says_none(self):
        a = self.machine("a")
        values = dict(fields(a.run("status").stdout))
        self.assertEqual(values["base"], "none")
        self.assertEqual(values["last"], "none")
        self.assertEqual(values["origin"], a.git("rev-parse", "origin/main"))

    def test_status_takes_no_lock(self):
        a, = self.synced("a")
        fd = os.open(a.repo / ".sync.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        t0 = time.monotonic()
        a.run("status", env=a.env(CLAUDE_SYNC_LOCK_WAIT="30"))
        self.assertLess(time.monotonic() - t0, 10)

    def test_status_locked_and_unknown(self):                # spec test 38
        key = self.make_gitcrypt_remote()
        a = self.machine("a")                                  # locked clone, no key
        self.assertEqual(dict(fields(a.run("status").stdout))["local changes"], "locked")
        a.key.write_bytes(key.read_bytes())
        a.run("pull")
        values = dict(fields(a.run("status").stdout))
        self.assertEqual(values["local changes"], "0 changed, 0 deleted")
        a.write(LOCAL + "n.md", "secret\n")
        self.kill_at(a, "push:before-push", "push")
        values = dict(fields(a.run("status").stdout))
        self.assertEqual(values["local changes"], "unknown (a run is in progress or was interrupted)")
        self.assertNotEqual(a.git("rev-parse", "HEAD"), a.base())

    # dry-run

    def test_dry_run_lists_and_writes_nothing(self):
        a, = self.synced("a")
        self.remote_commit({REPO + "x.md": "x\n", "skills/s/gone.md": "g\n"})
        a.run("pull")
        a.write(LOCAL + "new.md", "new\n")
        a.write(LOCAL + "x.md", "x edited\n")
        os.unlink(a.claude / "skills/s/gone.md")
        head, base, tip = a.git("rev-parse", "HEAD"), a.base(), self.remote_tip()
        for command in ("dry-run", "dry"):
            r = a.run(command)
            self.assertEqual(r.stdout.splitlines(), [
                f"A {REPO}new.md", f"M {REPO}x.md", "D skills/s/gone.md"])
            self.assertEqual((a.git("rev-parse", "HEAD"), a.base(), self.remote_tip()), (head, base, tip))
            self.assertEqual(a.git("status", "--porcelain", "--ignored", "--", "data"), "")
            self.assertEqual(self.remote_paths().count("skills/s/gone.md"), 1)

    def test_dry_run_after_killed_push(self):
        a, = self.synced("a")
        rel = LOCAL + "n.md"
        a.write(rel, "mine\n")
        self.kill_at(a, "push:before-commit", "push")
        self.assertNotEqual(a.git("status", "--porcelain", "--", "data"), "")      # data/ left dirty
        r = a.run("dry-run")
        self.assertEqual(r.stdout.splitlines(), [f"A {REPO}n.md"])
        self.assertEqual(a.git("status", "--porcelain", "--ignored", "--", "data"), "")
        self.assertEqual(a.git("rev-parse", "HEAD"), a.base())
        a.run("push")
        self.assertEqual(a.git("diff", "--name-only", "HEAD~1", "HEAD"), f"data/{REPO}n.md")
        self.assertEqual(self.remote_tip(), a.git("rev-parse", "HEAD"))

    # retention

    def test_retention(self):                                 # spec test 23
        for command in ("pull", "push", "dry-run"):
            with self.subTest(command=command):
                a = self.machine(f"m-{command}")
                a.run("pull")
                self.aged_folders(a)
                a.run(command)
                self.assertEqual(sorted(p.name for p in (a.repo / "conflicts").iterdir()), ["2999-01-01T00-00-00Z"])
                self.assertEqual(sorted(p.name for p in (a.repo / "trash").iterdir()), ["2999-01-01T00-00-00Z"])
                self.assertEqual(sum(" INFO " in ln and "2020-01-01T00-00-00Z" in ln for ln in a.log_lines()), 2)

    def test_retention_runs_after_error(self):
        a, = self.synced("a")
        self.aged_folders(a)
        self.break_remote(a)
        a.run("pull", expect=1)
        self.assertFalse((a.repo / "conflicts" / "2020-01-01T00-00-00Z").exists())
        self.assertFalse((a.repo / "trash" / "2020-01-01T00-00-00Z").exists())
        self.assertTrue((a.repo / "conflicts" / "2999-01-01T00-00-00Z").exists())

    def test_retention_skipped_when_lock_busy(self):
        a, = self.synced("a")
        self.aged_folders(a)
        fd = os.open(a.repo / ".sync.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        a.run("pull", env=a.env(CLAUDE_SYNC_LOCK_WAIT="1"))
        self.assertTrue((a.repo / "conflicts" / "2020-01-01T00-00-00Z").exists())

    # errors and logging

    def test_broken_remote_is_error_not_offline(self):        # spec test 16
        a, = self.synced("a")
        self.break_remote(a)
        r = a.run("pull", expect=1)
        self.assertTrue(any(" ERROR " in ln and "missing.git" in ln for ln in a.log_lines()))
        self.assertFalse(any("offline" in ln for ln in a.log_lines()))
        self.assertRegex(r.stdout, r"^claude-sync: ERROR ")

    def test_every_log_line_timestamped(self):
        a, = self.synced("a")
        a.run("pull", env=a.env(CLAUDE_SYNC_DEBUG="1"))
        a.write(LOCAL + "n.md", "mine\n")
        a.run("push", env=a.env(CLAUDE_SYNC_DEBUG="1"))
        a.run("status")
        a.write(LOCAL + "m.md", "more\n")
        self.break_remote(a)
        a.run("push", expect=1)
        lines = a.log_lines()
        self.assertGreater(len(lines), 5)
        for ln in lines:
            self.assertRegex(ln, LOG_LINE)
        self.assertTrue(any(" ERROR " in ln for ln in lines))

    def test_uncaught_exception_logged_with_traceback(self):
        a, = self.synced("a")
        (a.repo / ".sync.lock").unlink()
        (a.repo / ".sync.lock").mkdir()                        # os.open on a directory: not an expected error
        r = a.run("push", expect=1)
        lines = a.log_lines()
        self.assertTrue(any(" ERROR " in ln and "Traceback" in ln for ln in lines))
        self.assertTrue(any(" ERROR IsADirectoryError" in ln for ln in lines))
        self.assertEqual(len(r.stdout.splitlines()), 1)
        self.assertRegex(r.stdout, r"^claude-sync: ERROR IsADirectoryError")
        for ln in lines:
            self.assertRegex(ln, LOG_LINE)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_file_is_a_plain_error(self):
        a, = self.synced("a")
        os.chmod(a.claude / "CLAUDE.md", 0)
        self.addCleanup(os.chmod, a.claude / "CLAUDE.md", 0o644)
        r = a.run("push", expect=1)
        self.assertRegex(r.stdout, r"^claude-sync: ERROR cannot scan CLAUDE.md: ")
        self.assertFalse(any("Traceback" in ln for ln in a.log_lines()))

    def test_git_error_logged_without_traceback(self):
        a, = self.synced("a")
        self.break_remote(a)
        a.run("pull", expect=1)
        errors = [ln for ln in a.log_lines() if " ERROR " in ln]
        self.assertTrue(any("git fetch" in ln and "missing.git" in ln for ln in errors))
        self.assertFalse(any("Traceback" in ln for ln in a.log_lines()))

    def test_legacy_lock_dir(self):                           # spec test 19, integration
        a, = self.synced("a")
        legacy = a.repo / ".sync.lock.d"
        legacy.mkdir()
        self.remote_commit({REPO + "x.md": "x\n"})
        r = a.run("pull", env=a.env(CLAUDE_SYNC_LOCK_WAIT="1"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(any(" INFO lock busy" in ln for ln in a.log_lines()))
        self.assertIsNone(a.read(LOCAL + "x.md"))
        old = time.time() - 20 * 60
        os.utime(legacy, (old, old))
        a.run("pull", env=a.env(CLAUDE_SYNC_LOCK_WAIT="1"))
        self.assertFalse(legacy.exists())
        self.assertEqual(a.read(LOCAL + "x.md"), "x\n")


if __name__ == "__main__":
    unittest.main()
