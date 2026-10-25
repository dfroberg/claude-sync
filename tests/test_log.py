from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_sync.log import Log


class LogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cs-log-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.p = self.dir / "last-sync.log"

    def test_line_format(self):
        p = self.p
        Log(p, "pull", False).warn("x")
        self.assertRegex(p.read_text(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ pid=\d+ pull WARN x\n$")

    def test_levels(self):
        lg = Log(self.p, "push", False)
        lg.info("a")
        lg.error("b")
        lines = self.p.read_text().splitlines()
        self.assertTrue(lines[0].endswith(" push INFO a"))
        self.assertTrue(lines[1].endswith(" push ERROR b"))

    def test_debug_only_when_enabled(self):
        Log(self.p, "pull", False).debug("hidden")
        self.assertFalse(self.p.exists())
        Log(self.p, "pull", True).debug("shown")
        lines = self.p.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(" pull DEBUG shown"))

    def test_newlines_in_message_folded(self):
        Log(self.p, "pull", False).info("a\nb\r\nc")
        self.assertEqual(len(self.p.read_text().splitlines()), 1)
        self.assertTrue(self.p.read_text().endswith(" pull INFO a | b | c\n"))

    def test_rotation_over_5mb(self):
        p = self.p
        p.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        Log(p, "pull", False).info("after")
        self.assertTrue((p.parent / "last-sync.log.1").exists())
        self.assertLess(p.stat().st_size, 200)

    def test_no_rotation_at_or_below_5mb(self):
        p = self.p
        p.write_bytes(b"x" * (5 * 1024 * 1024))
        Log(p, "pull", False).info("after")
        self.assertFalse((p.parent / "last-sync.log.1").exists())

    def test_notice_truncated(self):
        out = io.StringIO()
        Log(self.p, "pull", False, out=out).notice("a" * 300)
        self.assertEqual(out.getvalue(), "a" * 200 + "\n")

    def test_notice_one_line(self):
        out = io.StringIO()
        Log(self.p, "pull", False, out=out).notice("x\ny\nz")
        self.assertEqual(out.getvalue(), "x | y | z\n")

    def test_exception_one_line_per_traceback_line(self):
        lg = Log(self.p, "pull", False)
        try:
            raise ValueError("boom")
        except ValueError:
            lg.exception("crashed")
        lines = self.p.read_text().splitlines()
        self.assertGreaterEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith(" pull ERROR crashed"))
        self.assertTrue(all(" pull ERROR " in ln for ln in lines))
        self.assertTrue(lines[-1].endswith("ValueError: boom"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_log_does_not_raise(self):
        ro = self.dir / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            self.assertIsNone(Log(ro / "sub" / "last-sync.log", "pull", False).warn("x"))
            self.assertIsNone(Log(ro / "last-sync.log", "pull", False).warn("x"))
        finally:
            ro.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
