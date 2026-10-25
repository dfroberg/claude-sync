from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_sync import fsops, paths
from claude_sync.fsops import (
    CiphertextError, atomic_write, copy_aside, is_ciphertext, move_aside, prune_older_than,
    put_aside, read_content, stat_key,
)
from claude_sync.paths import Content


class FsopsTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="cs-fsops-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.d), True)

    def test_content_is_shared(self):
        self.assertIs(fsops.Content, paths.Content)

    def test_atomic_write_sets_exec_and_leaves_no_tmp(self):
        d = self.d
        atomic_write(d / "a" / "s.sh", Content(b"#!/bin/sh\n", True))
        self.assertTrue((d / "a" / "s.sh").stat().st_mode & 0o100)
        self.assertEqual((d / "a" / "s.sh").read_bytes(), b"#!/bin/sh\n")
        atomic_write(d / "a" / "s.sh", Content(b"x", False))
        self.assertEqual((d / "a" / "s.sh").stat().st_mode & 0o777, 0o644)
        self.assertEqual(os.listdir(d / "a"), ["s.sh"])

    def test_atomic_write_refuses_symlink(self):
        d = self.d
        (d / "outside").write_text("orig")
        os.symlink(d / "outside", d / "link")
        with self.assertRaises(ValueError):
            atomic_write(d / "link", Content(b"new", False))
        self.assertEqual((d / "outside").read_text(), "orig")
        self.assertTrue((d / "link").is_symlink())

    def test_read_content_missing_vs_unreadable(self):
        d = self.d
        self.assertIsNone(read_content(d / "nope"))
        (d / "dir").mkdir()
        self.assertIsNone(read_content(d / "dir"))
        (d / "file").write_text("f")
        self.assertIsNone(read_content(d / "file" / "child"))
        (d / "x.sh").write_bytes(b"abc")
        (d / "x.sh").chmod(0o755)
        self.assertEqual(read_content(d / "x.sh"), Content(b"abc", True))
        (d / "target.txt").write_text("secret target")
        os.symlink(d / "target.txt", d / "link")
        with self.assertRaises(OSError) as cm:
            read_content(d / "link")
        self.assertEqual(cm.exception.errno, errno.ELOOP)
        os.symlink(d / "dir", d / "dirlink")
        with self.assertRaises(OSError):
            read_content(d / "dirlink")
        os.symlink(d / "nowhere", d / "dangling")
        with self.assertRaises(OSError):
            read_content(d / "dangling")
        if os.geteuid() != 0:
            (d / "secret").write_text("s")
            (d / "secret").chmod(0)
            try:
                with self.assertRaises(PermissionError):
                    read_content(d / "secret")
            finally:
                (d / "secret").chmod(0o644)

    def test_stat_key(self):
        d = self.d
        self.assertIsNone(stat_key(d / "nope"))
        (d / "f").write_bytes(b"abcd")
        st = (d / "f").stat()
        self.assertEqual(stat_key(d / "f"), (4, st.st_mtime_ns))

    def test_copy_aside_keeps_source(self):
        d = self.d
        src = d / "src.sh"
        src.write_text("echo\n")
        src.chmod(0o755)
        out = copy_aside(src, d / "conflicts" / "stamp", "skills/s/run.sh")
        self.assertEqual(out, d / "conflicts" / "stamp" / "skills/s/run.sh")
        self.assertEqual(src.read_text(), "echo\n")
        self.assertEqual(out.read_text(), "echo\n")
        self.assertEqual(out.stat().st_mode & 0o777, 0o755)
        out2 = copy_aside(src, d / "conflicts" / "stamp", "skills/s/run.sh")
        self.assertEqual(out2.name, "run.sh.1")

    def test_move_aside(self):
        d = self.d
        src = d / "src.md"
        src.write_text("m")
        out = move_aside(src, d / "trash" / "stamp", "memory/p/n.md")
        self.assertFalse(src.exists())
        self.assertEqual(out.read_text(), "m")
        src.write_text("m2")
        out2 = move_aside(src, d / "trash" / "stamp", "memory/p/n.md")
        self.assertEqual(out2.name, "n.md.1")
        self.assertEqual(out.read_text(), "m")

    def test_aside_refuses_unsafe_rel(self):
        d = self.d
        with self.assertRaises(ValueError):
            put_aside(d / "r", "../x", Content(b"x", False))
        with self.assertRaises(ValueError):
            move_aside(d / "nope", d / "r", "a/../../x")

    def test_type_clash_directory_in_the_way(self):
        d = self.d
        (d / "x").mkdir()
        (d / "x" / "f").write_text("keep")
        with self.assertRaises(IsADirectoryError):
            atomic_write(d / "x", Content(b"new", False))
        self.assertEqual((d / "x" / "f").read_text(), "keep")
        self.assertEqual(sorted(os.listdir(d / "x")), ["f"])
        self.assertEqual(sorted(os.listdir(d)), ["x"])

    def test_type_clash_file_in_the_way(self):
        d = self.d
        (d / "x").write_text("file")
        with self.assertRaises(NotADirectoryError):
            atomic_write(d / "x" / "y", Content(b"new", False))
        self.assertEqual((d / "x").read_text(), "file")

    def test_ciphertext_refused(self):
        d = self.d
        with self.assertRaises(CiphertextError):
            atomic_write(d / "c", Content(b"\x00GITCRYPT\x00abc", False))
        self.assertFalse((d / "c").exists())
        self.assertTrue(is_ciphertext(b"\x00GITCRYPT\x00abc"))
        self.assertFalse(is_ciphertext(b"plain"))

    def test_put_aside_never_overwrites(self):
        d = self.d
        first = put_aside(d / "conflicts" / "s", "skills/s/SKILL.md", Content(b"one", False))
        self.assertEqual(first, d / "conflicts" / "s" / "skills/s/SKILL.md")
        second = put_aside(d / "conflicts" / "s", "skills/s/SKILL.md", Content(b"two", False))
        self.assertTrue(str(second).endswith("SKILL.md.1"))
        third = put_aside(d / "conflicts" / "s", "skills/s/SKILL.md", Content(b"three", False))
        self.assertTrue(str(third).endswith("SKILL.md.2"))
        self.assertEqual(first.read_bytes(), b"one")
        self.assertEqual(second.read_bytes(), b"two")

    def test_prune_older_than(self):
        d = self.d
        now = 2_000_000_000.0
        old, new = d / "old", d / "new"
        old.mkdir()
        new.mkdir()
        (old / "f").write_text("x")
        os.utime(old, (now - 31 * 86400, now - 31 * 86400))
        os.utime(new, (now - 29 * 86400, now - 29 * 86400))
        removed = prune_older_than(d, 30, now)
        self.assertEqual(removed, [old])
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(prune_older_than(d / "missing", 30, now), [])


if __name__ == "__main__":
    unittest.main()
