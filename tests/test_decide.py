from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_sync.decide import Act, Outcome, decide
from claude_sync.paths import Content
from tests.harness import base_env


def C(data: bytes, exec_bit: bool = False) -> Content:
    return Content(data, exec_bit)


B3 = C(b"1\n2\n3\n4\n5\n")
L3 = C(b"1x\n2\n3\n4\n5\n")
R3 = C(b"1\n2\n3\n4\n5x\n")


class DecideTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="cs-decide-")
        self.addCleanup(shutil.rmtree, root, True)
        patcher = mock.patch.dict(os.environ, base_env(Path(root)), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rule1_equal(self):
        self.assertEqual(decide("CLAUDE.md", C(b"a"), C(b"b"), C(b"a"), False), Outcome(Act.NOTHING))
        self.assertEqual(decide("CLAUDE.md", None, C(b"b"), None, False).act, Act.NOTHING)
        self.assertEqual(decide("CLAUDE.md", None, None, None, False).act, Act.NOTHING)
        # same bytes, different exec bit is not equal
        self.assertNotEqual(decide("skills/s/a.sh", C(b"a", False), C(b"a", False), C(b"a", True), False).act,
                            Act.NOTHING)

    def test_remote_empty_never_applied(self):          # Review Focus 1
        o = decide("CLAUDE.md", C(b"local\n"), C(b"local\n"), C(b""), False)
        self.assertEqual(o.act, Act.KEEP_LOCAL)
        self.assertEqual(o.warn, "remote empty file not applied")
        self.assertFalse(o.local_win)
        # also when local differs from base
        o = decide("CLAUDE.md", C(b"local2\n"), C(b"local\n"), C(b""), False)
        self.assertEqual((o.act, o.local_win), (Act.KEEP_LOCAL, False))
        # an empty local file does not trigger the guard
        self.assertEqual(decide("CLAUDE.md", C(b""), C(b"x"), C(b""), False).act, Act.NOTHING)
        self.assertEqual(decide("CLAUDE.md", C(b""), C(b""), C(b"new"), False), Outcome(Act.WRITE, C(b"new")))

    def test_unchanged_local_takes_remote(self):
        self.assertEqual(decide("CLAUDE.md", C(b"a"), C(b"a"), C(b"b"), False), Outcome(Act.WRITE, C(b"b")))

    def test_unchanged_local_trashed_on_remote_delete(self):
        self.assertEqual(decide("CLAUDE.md", C(b"a"), C(b"a"), None, False), Outcome(Act.TRASH))

    def test_remote_edit_beats_local_delete(self):
        o = decide("CLAUDE.md", None, C(b"a"), C(b"b"), False)
        self.assertEqual((o.act, o.content, o.warn, o.local_win),
                         (Act.WRITE, C(b"b"), "remote edit beats local delete", False))

    def test_added_remote_only(self):
        self.assertEqual(decide("CLAUDE.md", None, None, C(b"b"), False), Outcome(Act.WRITE, C(b"b")))

    def test_local_edit_beats_remote_delete(self):
        o = decide("CLAUDE.md", C(b"mine"), C(b"a"), None, False)
        self.assertEqual((o.act, o.warn, o.local_win), (Act.KEEP_LOCAL, "local edit beats remote delete", True))
        self.assertIsNone(o.content)

    def test_merge_clean(self):
        o = decide("CLAUDE.md", L3, B3, R3, False)
        self.assertEqual(o.act, Act.WRITE)
        self.assertTrue(o.merged)
        self.assertEqual(o.content, C(b"1x\n2\n3\n4\n5x\n"))
        self.assertFalse(o.local_win)

    def test_merge_conflict_keep_both(self):
        o = decide("CLAUDE.md", C(b"1\nL\n3\n"), C(b"1\n2\n3\n"), C(b"1\nR\n3\n"), False)
        self.assertEqual(o.act, Act.KEEP_BOTH)
        self.assertFalse(o.merged)
        o = decide("skills/s/img.png", C(b"L"), C(b"B"), C(b"R"), False)       # binary
        self.assertEqual(o.act, Act.KEEP_BOTH)

    def test_both_added_conflict_fresh_takes_remote(self):
        o = decide("CLAUDE.md", C(b"mine\n"), None, C(b"theirs\n"), True)
        self.assertEqual(o.act, Act.TAKE_REMOTE)

    def test_both_added_conflict_migration_keeps_local(self):
        o = decide("CLAUDE.md", C(b"mine\n"), None, C(b"theirs\n"), False)
        self.assertEqual(o.act, Act.KEEP_BOTH)

    def test_both_added_merges_shared_lines(self):
        o = decide("memory/p/MEMORY.md", C(b"- [A](a.md) x\n- [L](l.md) z\n"), None,
                   C(b"- [A](a.md) x\n- [R](r.md) w\n"), False)
        self.assertEqual((o.act, o.merged), (Act.WRITE, True))

    def test_exec_only_change_takes_remote_bit(self):
        o = decide("skills/s/run.sh", C(b"x\n", False), C(b"x\n", False), C(b"x\n", True), False)
        self.assertEqual(o.act, Act.WRITE)
        self.assertTrue(o.content.exec)

    def test_exec_bit_changed_here_and_content_changed_remote_keeps_local_bit(self):
        o = decide("skills/s/run.sh", C(b"1\n2\n3\n4\n5\n", True), C(b"1\n2\n3\n4\n5\n", False),
                   C(b"1\n2\n3\n4\n5x\n", False), False)
        self.assertEqual(o.act, Act.WRITE)
        self.assertTrue(o.content.exec)

    SAME, OLD = b"same\n", b"old\n"

    def test_identical_content_local_lacks_bit_takes_remote_bit(self):
        o = decide("skills/x/tool.py", C(self.SAME, False), C(self.OLD, False), C(self.SAME, True), False)
        self.assertEqual(o, Outcome(Act.WRITE, content=C(self.SAME, True)))

    def test_identical_content_local_bit_change_kept(self):
        o = decide("skills/x/tool.py", C(self.SAME, True), C(self.OLD, False), C(self.SAME, False), False)
        self.assertEqual(o, Outcome(Act.NOTHING))

    def test_identical_content_no_base_keeps_local_bit(self):
        o = decide("skills/x/tool.py", C(self.SAME, False), None, C(self.SAME, True), False)
        self.assertEqual(o, Outcome(Act.NOTHING))

    def test_identical_content_no_base_fresh_takes_remote_bit(self):
        o = decide("skills/x/tool.py", C(self.SAME, False), None, C(self.SAME, True), True)
        self.assertEqual(o, Outcome(Act.WRITE, content=C(self.SAME, True)))

    def test_identical_content_text_path_is_not_a_merge(self):
        o = decide("skills/x/x.md", C(self.SAME, False), C(self.OLD, False), C(self.SAME, True), False)
        self.assertEqual(o, Outcome(Act.WRITE, content=C(self.SAME, True)))

    def test_outcome_is_frozen(self):
        with self.assertRaises(Exception):
            Outcome(Act.NOTHING).act = Act.WRITE


if __name__ == "__main__":
    unittest.main()
