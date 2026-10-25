from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_sync.merge import MergeError, links, merge_content, merge_exec, merge_text, strict_json
from claude_sync.paths import Content
from tests.harness import base_env


def C(data: bytes, exec_bit: bool = False) -> Content:
    return Content(data, exec_bit)


class MergeTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="cs-merge-")
        self.addCleanup(shutil.rmtree, root, True)
        # hermetic: no XDG_CONFIG_HOME, a temporary global config, no system config
        patcher = mock.patch.dict(os.environ, base_env(Path(root)), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clean_merge_different_places(self):        # spec test 3
        b = b"1\n2\n3\n4\n5\n"
        l = b"1x\n2\n3\n4\n5\n"
        r = b"1\n2\n3\n4\n5x\n"
        self.assertEqual(merge_text(l, b, r, False), b"1x\n2\n3\n4\n5x\n")

    def test_conflict_returns_none(self):               # spec test 4
        b = b"1\n2\n3\n"
        self.assertIsNone(merge_text(b"1\nL\n3\n", b, b"1\nR\n3\n", False))

    def test_empty_base_shares_common_lines(self):      # verified behaviour, spec 6.9
        l = b"- [A](a.md) x\n- [L1](l1.md) z\n"
        r = b"- [A](a.md) x\n- [R1](r1.md) w\n"
        self.assertEqual(merge_text(l, None, r, True),
                         b"- [A](a.md) x\n- [L1](l1.md) z\n- [R1](r1.md) w\n")
        self.assertIsNone(merge_text(l, None, r, False))

    def test_memory_union_keeps_both_additions(self):   # spec test 5
        b = C(b"- [A](a.md)\n")
        l = C(b"- [A](a.md)\n- [L1](l1.md)\n")
        r = C(b"- [A](a.md)\n- [R1](r1.md)\n")
        out = merge_content("memory/p/MEMORY.md", l, b, r, False)
        self.assertIsNotNone(out)
        self.assertEqual(links(out.data), {"a.md", "l1.md", "r1.md"})

    def test_memory_union_no_base(self):
        l = C(b"- [A](a.md) x\n- [L1](l1.md) z\n")
        r = C(b"- [A](a.md) x\n- [R1](r1.md) w\n")
        out = merge_content("memory/p/MEMORY.md", l, None, r, False)
        self.assertEqual(links(out.data), {"a.md", "l1.md", "r1.md"})

    def test_memory_removal_respected(self):            # spec test 28: separate hunks
        b = C(b"- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n- [D](d.md)\n")
        l = C(b"- [A](a.md)\n- [C](c.md)\n- [D](d.md)\n")                           # local removed B
        r = C(b"- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n- [D](d.md)\n- [N](n.md)\n")  # remote added N
        out = merge_content("memory/p/MEMORY.md", l, b, r, False)
        self.assertEqual(links(out.data), {"a.md", "c.md", "d.md", "n.md"})

    def test_memory_link_lost_returns_none(self):
        # both sides keep c.md; the merge result would have to keep it. A result that lost a
        # required link is rejected. Exercised through merge_content with a stub merge.
        b = C(b"- [A](a.md)\n")
        l = C(b"- [A](a.md)\n- [L1](l1.md)\n")
        r = C(b"- [A](a.md)\n- [R1](r1.md)\n")
        with mock.patch("claude_sync.merge.merge_text", return_value=b"- [A](a.md)\n- [R1](r1.md)\n"):
            self.assertIsNone(merge_content("MEMORY.md", l, b, r, False))

    def test_links(self):
        self.assertEqual(links(b"x [a](a.md) [b](dir/b.md) [c](c.txt) [d](http://x/d.md)"),
                         {"a.md", "dir/b.md", "http://x/d.md"})

    def test_json_clean_merge_parses(self):
        b = C(b'{\n "a": 1,\n "b": 2,\n "c": 3\n}\n')
        l = C(b'{\n "a": 10,\n "b": 2,\n "c": 3\n}\n')
        r = C(b'{\n "a": 1,\n "b": 2,\n "c": 30\n}\n')
        out = merge_content("settings.json", l, b, r, False)
        self.assertIsNotNone(out)
        self.assertEqual(out.data, b'{\n "a": 10,\n "b": 2,\n "c": 30\n}\n')

    def test_json_both_sides_keys_kept(self):           # spec test 6
        self.test_json_clean_merge_parses()

    def test_json_invalid_merge_rejected(self):         # spec test 6
        # R carries a comment line (a remote .json with comments syncs whole, but a merge must not
        # produce it): the text merge is clean, the result is not strict JSON.
        b = C(b'{\n "a": 1,\n "b": 2,\n "c": 3\n}\n')
        l = C(b'{\n "a": 1,\n "b": 2,\n "c": 30\n}\n')
        r = C(b'// note\n{\n "a": 1,\n "b": 2,\n "c": 3\n}\n')
        self.assertIsNotNone(merge_text(l.data, b.data, r.data, False))
        self.assertIsNone(merge_content("settings.json", l, b, r, False))

    def test_json_duplicate_key_rejected(self):         # spec test 6
        self.assertFalse(strict_json(b'{"x": 1, "y": 2, "x": 3}'))
        b = C(b'{\n "a": 1,\n "m": 2,\n "z": 3\n}\n')
        l = C(b'{\n "x": 1,\n "a": 1,\n "m": 2,\n "z": 3\n}\n')    # x added at the top
        r = C(b'{\n "a": 1,\n "m": 2,\n "x": 9,\n "z": 3\n}\n')    # x added before z
        self.assertIsNotNone(merge_text(l.data, b.data, r.data, False))   # a clean line merge
        self.assertIsNone(merge_content("settings.json", l, b, r, False))

    def test_strict_json(self):
        self.assertTrue(strict_json(b'{"a": [1, 2, {"b": null}]}'))
        self.assertFalse(strict_json(b"not json"))
        self.assertFalse(strict_json(b"\xff\xfe"))
        self.assertFalse(strict_json(b'{"a": {"x": 1, "x": 2}}'))

    def test_merge_error_raises(self):
        with self.assertRaises(MergeError):
            merge_text(b"a\0b\n", b"a\n", b"a\n", False)

    def test_merge_error_propagates_from_merge_content(self):
        with self.assertRaises(MergeError):
            merge_content("CLAUDE.md", C(b"a\0b\n"), C(b"a\n"), C(b"c\n"), False)

    def test_binary_never_merged(self):
        self.assertIsNone(merge_content("skills/s/img.png", C(b"1\n"), C(b"0\n"), C(b"2\n"), False))

    def test_merge_content_text_exec_bit(self):
        b = C(b"1\n2\n3\n4\n5\n", False)
        l = C(b"1x\n2\n3\n4\n5\n", True)
        r = C(b"1\n2\n3\n4\n5x\n", False)
        out = merge_content("skills/s/run.sh", l, b, r, False)
        self.assertEqual((out.data, out.exec), (b"1x\n2\n3\n4\n5x\n", True))

    def test_merge_exec_rules(self):
        self.assertTrue(merge_exec(False, False, True, False))    # local unchanged: take remote
        self.assertTrue(merge_exec(True, False, False, False))    # local changed: keep local
        self.assertFalse(merge_exec(False, None, True, False))    # no base: local
        self.assertTrue(merge_exec(False, None, True, True))      # no base, fresh: remote

    def test_merge_text_uses_cleaned_env(self):
        seen = {}
        real = subprocess.run

        def spy(argv, **kw):
            seen["argv"], seen["env"] = argv, kw.get("env")
            return real(argv, **kw)

        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent"}):
            with mock.patch("claude_sync.merge.subprocess.run", side_effect=spy):
                merge_text(b"a\n", b"a\n", b"a\n", False)
        self.assertIsInstance(seen["argv"], list)
        self.assertNotIn("GIT_DIR", seen["env"])
        self.assertNotIn("GIT_INDEX_FILE", seen["env"])
        self.assertNotIn("XDG_CONFIG_HOME", seen["env"])
        self.assertEqual(seen["argv"][:3], ["git", "merge-file", "-p"])


if __name__ == "__main__":
    unittest.main()
