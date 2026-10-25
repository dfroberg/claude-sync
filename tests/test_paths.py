from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_sync.paths import (
    DIRS, FILES, TEXT_EXT, TOKEN, Content, canon, canon_content, decanon, decanon_content,
    is_text, is_tmp, safe_rel, scan_local, to_local, to_repo, under, via_symlink,
)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cs-paths-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.claude = self.tmp / ".claude"
        self.claude.mkdir()

    def put(self, rel, text="x\n"):
        p = self.claude / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class PathsTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(TOKEN, "__USER__")
        self.assertEqual(tuple(FILES), ("CLAUDE.md", "settings.json", "mcp_settings.json", "keybindings.json"))
        self.assertEqual(tuple(DIRS), ("skills", "agents", "commands", "hooks"))
        self.assertEqual(set(TEXT_EXT), {".md", ".json", ".jsonl", ".txt", ".sh", ".bash", ".zsh", ".lua",
                                         ".toml", ".yaml", ".yml", ".cfg"})

    def test_canon_round_trip(self):
        raw = b"see /Users/alice/x and -Users-alice-proj- and /Users/alicex/"
        c = canon(raw, "alice")
        self.assertEqual(c, b"see /Users/__USER__/x and -Users-__USER__-proj- and /Users/alicex/")
        self.assertEqual(decanon(c, "bob"), b"see /Users/bob/x and -Users-bob-proj- and /Users/alicex/")

    def test_canon_prefix_user_untouched(self):  # Review Focus 4
        self.assertEqual(canon(b"/Users/alice/ -Users-alice- -Users-al-x-Users-al-", "al"),
                         b"/Users/alice/ -Users-alice- -Users-__USER__-x-Users-__USER__-")

    def test_mapping(self):
        self.assertEqual(to_repo("projects/-Users-alice-p/memory/a/b.md", "alice"), "memory/-Users-__USER__-p/a/b.md")
        self.assertEqual(to_local("memory/-Users-__USER__-p/a/b.md", "bob"), "projects/-Users-bob-p/memory/a/b.md")
        self.assertEqual(to_repo("skills/s/SKILL.md", "alice"), "skills/s/SKILL.md")
        self.assertIsNone(to_repo("projects/-Users-alice-p/transcript.jsonl", "alice"))
        self.assertIsNone(to_repo("plugins/x.json", "alice"))
        self.assertEqual(to_repo("projects/other-proj/memory/a.md", "alice"), "memory/other-proj/a.md")
        self.assertIsNone(to_local("skills/../../.ssh/authorized_keys", "bob"))
        self.assertIsNone(to_local("memory/../x.md", "bob"))

    def test_mapping_files_and_edges(self):
        for f in FILES:
            self.assertEqual(to_repo(f, "alice"), f)
            self.assertEqual(to_local(f, "alice"), f)
        for d in DIRS:
            self.assertEqual(to_repo(d + "/x/y.md", "alice"), d + "/x/y.md")
            self.assertEqual(to_local(d + "/x/y.md", "alice"), d + "/x/y.md")
            self.assertIsNone(to_repo(d, "alice"))
            self.assertIsNone(to_local(d, "alice"))
        self.assertIsNone(to_repo("projects/p/memory", "alice"))
        self.assertIsNone(to_local("memory/p", "alice"))
        self.assertIsNone(to_repo("projects/p/memory/../x", "alice"))
        self.assertIsNone(to_repo("/skills/x", "alice"))

    def test_safe_rel_and_under(self):
        root = Path(tempfile.mkdtemp(prefix="cs-under-")).resolve()
        self.addCleanup(shutil.rmtree, str(root), True)
        for bad in ["", "/etc/x", "a//b", "a/./b", "a/../b", ".."]:
            self.assertFalse(safe_rel(bad))
        self.assertTrue(safe_rel("skills/s/a.md"))
        with self.assertRaises(ValueError):
            under(root, "skills/../../x")
        self.assertEqual(under(root, "skills/s/a.md"), root / "skills/s/a.md")

    def test_under_refuses_symlinked_parent_escape(self):
        root = Path(tempfile.mkdtemp(prefix="cs-under-")).resolve()
        outside = Path(tempfile.mkdtemp(prefix="cs-out-")).resolve()
        self.addCleanup(shutil.rmtree, str(root), True)
        self.addCleanup(shutil.rmtree, str(outside), True)
        os.symlink(outside, root / "link")
        with self.assertRaises(ValueError):
            under(root, "link/x.md")

    def test_content_helpers_leave_binary_alone(self):
        raw = Content(b"/Users/alice/x", True)
        self.assertEqual(canon_content("a.png", raw, "alice"), raw)
        self.assertEqual(canon_content("a.md", raw, "alice"), Content(b"/Users/__USER__/x", True))
        self.assertEqual(decanon_content("a.md", Content(b"/Users/__USER__/x", False), "bob"),
                         Content(b"/Users/bob/x", False))
        self.assertEqual(decanon_content("a.bin", Content(b"/Users/__USER__/x", False), "bob"),
                         Content(b"/Users/__USER__/x", False))

    def test_is_text(self):
        for n in ["a.md", "b.JSON", "c.sh", "d.jsonl", "e.yml", "x/y/z.cfg"]:
            self.assertTrue(is_text(n), n)
        for n in ["a.png", "b.bin", "c", "d.md.bak"]:
            self.assertFalse(is_text(n), n)

    def test_is_tmp(self):
        self.assertTrue(is_tmp("a.md.sync-tmp.123"))
        self.assertFalse(is_tmp("a.md"))


class ScanTest(TmpCase):
    def test_scan_collects_allowlist_only(self):
        for f in FILES:
            self.put(f)
        self.put("skills/s/SKILL.md")
        self.put("agents/a.md")
        self.put("projects/-Users-alice-p/memory/m.md")
        self.put("projects/-Users-alice-p/transcript.jsonl")
        self.put("plugins/x.json")
        self.put("skills/s/SKILL.md.sync-tmp.42")
        files, skipped = scan_local(self.claude, "alice")
        self.assertEqual(set(files), set(FILES) | {"skills/s/SKILL.md", "agents/a.md",
                                                   "memory/-Users-__USER__-p/m.md"})
        self.assertEqual(files["skills/s/SKILL.md"], self.claude / "skills/s/SKILL.md")
        self.assertEqual(skipped, set())

    def test_scan_missing_claude_dirs_is_empty(self):
        self.assertEqual(scan_local(self.claude, "alice"), ({}, set()))

    def test_scan_skips_symlinked_dirs(self):  # Review Focus 2
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("o\n")
        self.put("skills/real/SKILL.md")
        os.symlink(outside, self.claude / "skills" / "linked")
        files, skipped = scan_local(self.claude, "alice")
        self.assertIn("skills/real/SKILL.md", files)
        self.assertNotIn("skills/linked/SKILL.md", files)
        self.assertIn("skills/linked/", skipped)

    def test_scan_skips_symlinked_file_inside_dir(self):
        self.put("skills/real/SKILL.md")
        (self.tmp / "elsewhere.md").write_text("e\n")
        os.symlink(self.tmp / "elsewhere.md", self.claude / "skills" / "real" / "link.md")
        files, skipped = scan_local(self.claude, "alice")
        self.assertNotIn("skills/real/link.md", files)
        self.assertIn("skills/real/link.md", skipped)

    def test_scan_skips_top_level_file_symlink(self):
        (self.tmp / "elsewhere").mkdir()
        (self.tmp / "elsewhere" / "real.md").write_text("r\n")
        os.symlink(self.tmp / "elsewhere" / "real.md", self.claude / "CLAUDE.md")
        files, skipped = scan_local(self.claude, "alice")
        self.assertNotIn("CLAUDE.md", files)
        self.assertIn("CLAUDE.md", skipped)

    def test_scan_symlinked_top_level_dir(self):
        (self.tmp / "elsewhere").mkdir()
        os.symlink(self.tmp / "elsewhere", self.claude / "hooks")
        files, skipped = scan_local(self.claude, "alice")
        self.assertEqual(files, {})
        self.assertIn("hooks/", skipped)

    def test_scan_symlinked_memory_trees(self):
        (self.tmp / "elsewhere").mkdir()
        (self.claude / "projects" / "-Users-alice-a").mkdir(parents=True)
        os.symlink(self.tmp / "elsewhere", self.claude / "projects" / "-Users-alice-a" / "memory")
        os.symlink(self.tmp / "elsewhere", self.claude / "projects" / "-Users-alice-b")
        files, skipped = scan_local(self.claude, "alice")
        self.assertEqual(files, {})
        self.assertEqual(skipped, {"memory/-Users-__USER__-a/", "memory/-Users-__USER__-b/"})

    def test_scan_unreadable_dir_raises(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.put("skills/locked/SKILL.md")
        locked = self.claude / "skills" / "locked"
        locked.chmod(0)
        try:
            with self.assertRaises(PermissionError):
                scan_local(self.claude, "alice")
        finally:
            locked.chmod(0o755)

    def test_via_symlink(self):
        (self.tmp / "outside").mkdir()
        self.put("skills/real/x")
        os.symlink(self.tmp / "outside", self.claude / "skills" / "linked")
        (self.tmp / "e.md").write_text("e")
        os.symlink(self.tmp / "e.md", self.claude / "CLAUDE.md")
        self.assertTrue(via_symlink(self.claude, "skills/linked/SKILL.md"))
        self.assertTrue(via_symlink(self.claude, "skills/linked"))
        self.assertFalse(via_symlink(self.claude, "skills/real/x"))
        self.assertTrue(via_symlink(self.claude, "CLAUDE.md"))
        self.assertFalse(via_symlink(self.claude, "settings.json"))
        self.assertFalse(via_symlink(self.claude, "nothing/here/at/all"))


if __name__ == "__main__":
    unittest.main()
