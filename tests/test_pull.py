from __future__ import annotations

import json
import os
import unittest

from tests.harness import SyncTestCase, wait_for

LOCAL = "projects/-Users-alice-p/memory/"      # under ~/.claude
REPO = "memory/-Users-__USER__-p/"             # under data/


class PullTest(SyncTestCase):
    def test_dod_local_edit_survives_pull_from_other_session(self):        # spec test 1
        a, = self.synced("a")
        a.write("CLAUDE.md", "# shared\nlocal edit\n")
        self.remote_commit({REPO + "x.md": "unrelated\n"})   # remote moves, CLAUDE.md untouched
        a.run("pull")                                         # a separate process = another session
        self.assertEqual(a.read("CLAUDE.md"), "# shared\nlocal edit\n")
        self.assertEqual(a.read(LOCAL + "x.md"), "unrelated\n")
        a.run("pull")                                         # and again with nothing new
        self.assertEqual(a.read("CLAUDE.md"), "# shared\nlocal edit\n")

    def test_remote_change_arrives_edit_stays(self):                        # spec test 2
        a, = self.synced("a")
        a.write("CLAUDE.md", "# shared\nlocal edit\n")
        self.remote_commit({"skills/s/SKILL.md": "skill v1\n", REPO + "y.md": "y\n"})
        r = a.run("pull")
        self.assertEqual(r.stdout, "")
        self.assertEqual(a.read("skills/s/SKILL.md"), "skill v1\n")
        self.assertEqual(a.read(LOCAL + "y.md"), "y\n")
        self.assertEqual(a.read("CLAUDE.md"), "# shared\nlocal edit\n")
        self.assertEqual(a.conflicts(), [])

    def test_clean_merge_both_edits_kept(self):                             # spec test 3
        a, = self.synced("a")
        self.remote_commit({REPO + "n.md": "1\n2\n3\n4\n5\n"})
        a.run("pull")
        a.write(LOCAL + "n.md", "1x\n2\n3\n4\n5\n")
        self.remote_commit({REPO + "n.md": "1\n2\n3\n4\n5x\n"})
        r = a.run("pull")
        self.assertEqual(a.read(LOCAL + "n.md"), "1x\n2\n3\n4\n5x\n")
        self.assertEqual(a.conflicts(), [])
        self.assertEqual(r.stdout, "")
        self.assertTrue(any(" INFO pull done" in ln and "1 merged" in ln for ln in a.log_lines()))

    def test_conflict_kept_both_with_notice(self):                          # spec test 4
        a, = self.synced("a")
        rel = LOCAL + "n.md"
        self.remote_commit({REPO + "n.md": "1\n2\n3\n"})
        a.run("pull")
        a.write(rel, "1\nlocal\n3\n")
        self.remote_commit({REPO + "n.md": "1\nremote\n3\n"})
        r = a.run("pull")
        self.assertEqual(a.read(rel), "1\nlocal\n3\n")
        self.assertEqual(a.conflicts(), [rel])
        self.assertRegex(r.stdout, r"^claude-sync: 1 conflicts kept both, remote copies in ~/\.claude-sync/conflicts/")
        self.assertEqual(len(r.stdout.splitlines()), 1)
        self.assertTrue(any(" WARN kept both: " in ln for ln in a.log_lines()))
        stamp = next((a.repo / "conflicts").iterdir())
        self.assertEqual((stamp / rel).read_text(), "1\nremote\n3\n")

    def test_memory_index_union(self):                                      # spec test 5
        a, = self.synced("a")
        self.remote_commit({REPO + "MEMORY.md": "- [A](a.md)\n"})
        a.run("pull")
        a.write(LOCAL + "MEMORY.md", "- [A](a.md)\n- [L1](l1.md)\n")
        self.remote_commit({REPO + "MEMORY.md": "- [A](a.md)\n- [R1](r1.md)\n"})
        a.run("pull")
        text = a.read(LOCAL + "MEMORY.md")
        for link in ("a.md", "l1.md", "r1.md"):
            self.assertIn(f"]({link})", text)
        self.assertEqual(a.conflicts(), [])

    def test_settings_json_both_sides_kept(self):                           # spec test 6
        a, = self.synced("a")
        base = '{\n "a": 1,\n "b": 2,\n "c": 3,\n "d": 4,\n "e": 5\n}\n'
        self.remote_commit({"settings.json": base})
        a.run("pull")
        a.write("settings.json", base.replace('"a": 1', '"a": 10'))
        self.remote_commit({"settings.json": base.replace('"e": 5', '"e": 50')})
        a.run("pull")
        merged = json.loads(a.read("settings.json"))
        self.assertEqual((merged["a"], merged["e"]), (10, 50))
        self.assertEqual(a.conflicts(), [])

    def test_settings_json_same_key_added_kept_both(self):                  # spec test 6
        a, = self.synced("a")
        base = '{\n "a": 1,\n "m": 2,\n "z": 3\n}\n'
        local = '{\n "x": 1,\n "a": 1,\n "m": 2,\n "z": 3\n}\n'
        remote = '{\n "a": 1,\n "m": 2,\n "x": 9,\n "z": 3\n}\n'
        self.remote_commit({"settings.json": base})
        a.run("pull")
        a.write("settings.json", local)
        self.remote_commit({"settings.json": remote})
        r = a.run("pull")
        self.assertEqual(a.read("settings.json"), local)
        self.assertEqual(a.conflicts(), ["settings.json"])
        self.assertIn("1 conflicts kept both", r.stdout)
        stamp = next((a.repo / "conflicts").iterdir())
        self.assertEqual((stamp / "settings.json").read_text(), remote)

    def test_binary_and_exec_round_trip(self):
        a, = self.synced("a")
        blob = b"/Users/__USER__/\x00\xff"
        self.remote_commit({"skills/s/img.png": blob, "hooks/x.sh": (b"#!/bin/sh\necho /Users/__USER__/\n", True)})
        a.run("pull")
        self.assertEqual((a.claude / "skills/s/img.png").read_bytes(), blob)
        self.assertEqual((a.claude / "hooks/x.sh").read_bytes(), b"#!/bin/sh\necho /Users/alice/\n")
        self.assertEqual((a.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((a.claude / "skills/s/img.png").stat().st_mode & 0o777, 0o644)

    def test_jsonc_remote_update_applied(self):
        a, = self.synced("a")
        v1 = '{\n  // comment\n  "a": 1\n}\n'
        v2 = '{\n  // comment\n  "a": 2\n}\n'
        self.remote_commit({"skills/s/tsconfig.json": v1})
        a.run("pull")
        self.remote_commit({"skills/s/tsconfig.json": v2})
        r = a.run("pull")
        self.assertEqual(a.read("skills/s/tsconfig.json"), v2)
        self.assertEqual(a.conflicts(), [])
        self.assertEqual(r.stdout, "")

    def test_remote_delete(self):                                           # spec test 7
        a, = self.synced("a")
        self.remote_commit({"skills/s/a.md": "a\n", "skills/s/b.md": "b\n"})
        a.run("pull")
        a.write("skills/s/b.md", "b edited\n")
        self.remote_commit({"skills/s/a.md": None, "skills/s/b.md": None})
        r = a.run("pull")
        self.assertIsNone(a.read("skills/s/a.md"))
        self.assertEqual(a.trash(), ["skills/s/a.md"])
        self.assertEqual(a.read("skills/s/b.md"), "b edited\n")
        self.assertTrue(any(" WARN local edit beats remote delete" in ln for ln in a.log_lines()))
        self.assertRegex(r.stdout, r"^claude-sync: 1 local edits kept over remote deletes")

    def test_two_users_rehydrated(self):                                    # spec test 12
        self.remote_commit({REPO + "n.md": "see /Users/__USER__/x and -Users-__USER__-p\n",
                            "CLAUDE.md": "home /Users/__USER__/\n"})
        b = self.machine("b", user="bob")
        b.run("pull")
        self.assertEqual(b.read("projects/-Users-bob-p/memory/n.md"), "see /Users/bob/x and -Users-bob-p\n")
        self.assertEqual(b.read("CLAUDE.md"), "home /Users/bob/\n")

    def test_self_update_updates_work_tree(self):                           # spec test 20
        a, = self.synced("a")
        self.remote_commit({"bin/README": "updated\n"}, root_relative=True)
        a.run("pull")
        self.assertEqual((a.repo / "bin/README").read_text(), "updated\n")
        self.assertEqual(a.base(), a.git("rev-parse", "origin/main"))

    def test_dirty_bin_edit_stops_pull(self):                               # spec test 20, second half
        a, = self.synced("a")
        (a.repo / "bin/README").write_text("hand edit\n")
        self.remote_commit({REPO + "x.md": "x\n"})
        r = a.run("pull", expect=1)
        self.assertIn("claude-sync: ERROR tracked files outside data/ are modified", r.stdout)
        self.assertEqual((a.repo / "bin/README").read_text(), "hand edit\n")
        self.assertIsNone(a.read(LOCAL + "x.md"))

    def test_claude_config_dir_ignored(self):                               # spec test 21
        a = self.machine("a")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        a.run("pull", env=a.env(CLAUDE_CONFIG_DIR=str(elsewhere)))
        self.assertEqual(a.read("CLAUDE.md"), "# shared\n")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_base_moves_and_invariant_holds(self):
        a, = self.synced("a")
        tip = self.remote_commit({REPO + "x.md": "x\n", "skills/s/SKILL.md": "s\n"})
        a.run("pull")
        self.assertEqual(a.base(), tip)
        self.assertEqual(a.git("rev-parse", "HEAD"), tip)
        self.assertEqual(a.git("rev-parse", "origin/main"), tip)
        self.assertEqual(a.git("status", "--porcelain", "--ignored", "--", "data"), "")
        self.assertTrue(any(" INFO pull done in " in ln for ln in a.log_lines()))

    def test_pull_keeps_local_when_remote_emptied(self):                    # Review Focus 1
        a, = self.synced("a")
        self.remote_commit({REPO + "x.md": "content\n"})
        a.run("pull")
        self.remote_commit({REPO + "x.md": ""})
        r = a.run("pull")
        self.assertEqual(a.read(LOCAL + "x.md"), "content\n")
        self.assertTrue(any(" WARN remote empty file not applied" in ln for ln in a.log_lines()))
        self.assertEqual(r.stdout, "")
        self.assertEqual(a.base(), a.git("rev-parse", "origin/main"))

    def test_odd_names_round_trip(self):                                    # Review Focus 3, pull half
        a, = self.synced("a")
        self.remote_commit({REPO + "MEMORY notes.md": "a\n", REPO + "résumé.md": "1\n2\n3\n"})
        a.run("pull")
        self.assertEqual(a.read(LOCAL + "MEMORY notes.md"), "a\n")
        self.assertEqual(a.read(LOCAL + "résumé.md"), "1\n2\n3\n")
        a.write(LOCAL + "résumé.md", "1\nlocal\n3\n")
        self.remote_commit({REPO + "résumé.md": "1\nremote\n3\n"})
        a.run("pull")
        self.assertEqual(a.read(LOCAL + "résumé.md"), "1\nlocal\n3\n")
        self.assertEqual(a.conflicts(), [LOCAL + "résumé.md"])

    def test_edit_during_pull_window_survives(self):                        # Review Focus 5
        a, = self.synced("a")
        rel = LOCAL + "n.md"
        self.remote_commit({REPO + "n.md": "l1\nl2\nl3\nl4\nl5\n"})
        a.run("pull")
        self.remote_commit({REPO + "n.md": "l1\nl2\nl3\nl4\nl5x\n"})
        hold = a.repo / ".hold-pull:before-replace"
        p = a.popen("pull", env=a.env(CLAUDE_SYNC_TEST_HOLD="pull:before-replace"))
        try:
            wait_for(hold)
            path = a.write(rel, "l1 edited during pull\nl2\nl3\nl4\nl5\n")
            st = path.stat()
            os.utime(path, (st.st_atime + 1, st.st_mtime + 1))      # defeat a same-tick mtime
            hold.unlink()
            p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0)
        # the new local bytes were re-read and decided again: merged with the remote edit, never overwritten
        self.assertEqual(a.read(rel), "l1 edited during pull\nl2\nl3\nl4\nl5x\n")
        self.assertEqual(a.conflicts(), [])


if __name__ == "__main__":
    unittest.main()
