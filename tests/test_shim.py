from __future__ import annotations

import os
import stat
import subprocess
import sys
import unittest

from tests.harness import SyncTestCase, TOOL


class ShimTest(SyncTestCase):
    def test_shim_is_executable_bash(self):
        self.assertTrue(TOOL.stat().st_mode & stat.S_IXUSR)
        self.assertTrue(TOOL.read_text().startswith("#!/usr/bin/env bash\n"))

    def test_runs_package_with_given_python(self):
        m = self.machine("a")
        r = m.run("status")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(m.env()["CLAUDE_SYNC_PYTHON"], sys.executable)

    def test_no_python_logs_and_exits_zero(self):
        m = self.machine("a")
        r = m.run("pull", env=m.env(CLAUDE_SYNC_PYTHON="/nonexistent/python3"))
        self.assertIn("claude-sync: ERROR no usable python3 found", r.stdout)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        self.assertRegex(m.log_lines()[-1], r"Z pid=\d+ pull ERROR no usable python3")

    def test_no_python_with_missing_repo_still_prints_notice(self):
        m = self.machine("a", clone=False)
        r = m.run("pull", env=m.env(CLAUDE_SYNC_PYTHON="/nonexistent/python3"))
        self.assertIn("claude-sync: ERROR no usable python3 found", r.stdout)

    def test_bad_usage_exit_2(self):
        m = self.machine("a")
        r = m.run("frobnicate", expect=2)
        self.assertIn("usage: claude-sync {pull|push|status|dry-run}", r.stderr)
        self.assertEqual(r.stdout, "")
        m.run(expect=2)
        m.run("pull", "extra", expect=2)

    def test_nested_invocation_skips(self):
        m = self.machine("a")
        r = m.run("pull", env=m.env(CLAUDE_SYNC_RECONCILING="1"))
        self.assertEqual(r.stdout, "")
        self.assertIsNone(m.base())

    def test_project_dir_cannot_shadow_stdlib(self):
        m = self.machine("a")
        proj = self.root / "proj"
        proj.mkdir()
        (proj / "json.py").write_text("raise RuntimeError('shadowed')\n")
        (proj / "subprocess.py").write_text("raise RuntimeError('shadowed')\n")
        m.run("pull", cwd=proj, env=m.env(PYTHONPATH=str(proj)))
        self.assertIsNotNone(m.base())

    def test_hook_env_inside_other_repo(self):          # spec test 31
        m = self.machine("a")
        other = self.root / "proj"
        subprocess.run(["git", "init", "-q", str(other)], env=m.env(), check=True)
        (other / "f.txt").write_text("mine\n")
        subprocess.run(["git", "-C", str(other), "add", "f.txt"], env=m.env(), check=True)
        before = subprocess.run(["git", "-C", str(other), "status", "--porcelain"], env=m.env(),
                                capture_output=True, text=True).stdout
        m.run("pull", cwd=other, env=m.env(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other),
                                           GIT_INDEX_FILE=str(other / ".git/index")))
        self.assertIsNotNone(m.base())
        self.assertEqual((other / "f.txt").read_text(), "mine\n")
        after = subprocess.run(["git", "-C", str(other), "status", "--porcelain"], env=m.env(),
                               capture_output=True, text=True).stdout
        self.assertIn("A  f.txt", after)
        self.assertEqual(before, after)
        head = subprocess.run(["git", "-C", str(other), "rev-parse", "-q", "--verify", "HEAD"],
                              env=m.env(), capture_output=True)
        self.assertNotEqual(head.returncode, 0)  # the other repo stayed unborn: nothing was committed or reset there


if __name__ == "__main__":
    unittest.main()
