from __future__ import annotations

import json
import os
import unittest

from tests.harness import SyncTestCase

LOCAL = "projects/-Users-alice-p/memory/"      # under ~/.claude
REPO = "memory/-Users-__USER__-p/"             # under data/
HOOKS = '{"hooks": {"a": 1}}\n'
REMOTE_SETTINGS = '{"model": "x", "hooks": {}}\n'


class PullEdgeTest(SyncTestCase):
    def _new_machine_with_local_files(self, name: str = "b"):
        self.remote_commit({"settings.json": REMOTE_SETTINGS})
        b = self.machine(name)
        b.write("settings.json", HOOKS)
        b.write(LOCAL + "only.md", "local only\n")
        return b

    def test_new_machine_marker_takes_remote(self):           # spec test 10, pull half
        b = self._new_machine_with_local_files()
        (b.repo / ".fresh-machine").touch()
        r = b.run("pull")
        self.assertEqual(json.loads(b.read("settings.json"))["model"], "x")
        self.assertIn("settings.json", b.conflicts())
        stamp = next((b.repo / "conflicts").iterdir())
        self.assertEqual((stamp / "settings.json").read_text(), HOOKS)
        self.assertEqual(b.read(LOCAL + "only.md"), "local only\n")
        self.assertEqual(b.read("CLAUDE.md"), "# shared\n")
        self.assertFalse((b.repo / ".fresh-machine").exists())
        self.assertIsNotNone(b.base())
        self.assertIn("1 conflicts kept both", r.stdout)

    def test_marker_survives_failed_first_pull(self):
        b = self._new_machine_with_local_files()
        (b.repo / ".fresh-machine").touch()
        b.git("remote", "set-url", "origin", "file:///nonexistent/nowhere.git")
        r = b.run("pull", expect=1)
        self.assertIn("claude-sync: ERROR", r.stdout)
        self.assertTrue((b.repo / ".fresh-machine").exists())
        self.assertIsNone(b.base())
        self.assertEqual(b.read("settings.json"), HOOKS)
        b.git("remote", "set-url", "origin", f"file://{self.remote}")
        b.run("pull")                                       # a plain pull still treats the machine as new
        self.assertEqual(json.loads(b.read("settings.json"))["model"], "x")
        self.assertIn("settings.json", b.conflicts())
        self.assertFalse((b.repo / ".fresh-machine").exists())

    def test_fresh_copy_failure_keeps_local(self):             # spec test 36
        b = self._new_machine_with_local_files()
        (b.repo / ".fresh-machine").touch()
        (b.repo / "conflicts").write_text("a regular file, so no stamp folder can be created\n")
        r = b.run("pull", expect=1)
        self.assertEqual(b.read("settings.json"), HOOKS)
        self.assertIsNone(b.base())
        self.assertTrue((b.repo / ".fresh-machine").exists())
        self.assertIn("claude-sync: ERROR cannot write settings.json", r.stdout)

    def test_migration_keeps_local_parks_remote(self):         # spec test 11
        x_rel, y_rel = LOCAL + "x.md", LOCAL + "y.md"
        self.remote_commit({REPO + "x.md": "c1 line\nx2\n", REPO + "y.md": "y\n"})
        b = self.machine("b")                                  # cloned at c1
        b.write(x_rel, "local line\nx2\n")                     # same line 1 as c2 changes below
        b.write(y_rel, "local edit\n")
        self.remote_commit({REPO + "x.md": "remote line\nx2\n"})                       # c2, never applied
        b.git("fetch", "-q", "origin")
        b.git("reset", "-q", "--hard", "origin/main")          # as the old push leaves HEAD
        self.assertIsNone(b.base())
        r = b.run("pull")
        self.assertEqual(b.read(y_rel), "local edit\n")
        self.assertEqual(b.read(x_rel), "local line\nx2\n")     # local kept in place
        self.assertIn(x_rel, b.conflicts())                     # remote copy parked
        stamp = next((b.repo / "conflicts").iterdir())
        self.assertEqual((stamp / x_rel).read_text(), "remote line\nx2\n")
        self.assertEqual(b.base(), b.git("rev-parse", "origin/main"))
        self.assertRegex(r.stdout, r"^claude-sync: \d+ conflicts kept both")

    def test_remote_mass_delete_stops_everything(self):        # spec test 9, remote half
        a, = self.synced("a")
        rels = [f"skills/s/f{i}.md" for i in range(30)]
        self.remote_commit({r: f"{i}\n" for i, r in enumerate(rels)})
        a.run("pull")
        base_before = a.base()
        self.remote_commit({r: None for r in rels[:25]})
        r = a.run("pull", expect=1)
        self.assertEqual(a.base(), base_before)
        self.assertTrue(all(a.read(p) is not None for p in rels))
        self.assertEqual(a.trash(), [])
        self.assertIn("claude-sync: ERROR remote deletes 25 files", r.stdout)
        self.assertLessEqual(len(r.stdout.rstrip("\n")), 200)
        a.run("pull", env=a.env(CLAUDE_SYNC_ALLOW_PRUNE="1"))
        self.assertEqual(len(a.trash()), 25)
        self.assertEqual(a.base(), a.git("rev-parse", "origin/main"))

    def test_remote_delete_under_threshold_applies(self):
        a, = self.synced("a")
        rels = [f"skills/s/f{i}.md" for i in range(30)]
        self.remote_commit({r: f"{i}\n" for i, r in enumerate(rels)})
        a.run("pull")
        self.remote_commit({r: None for r in rels[:20]})       # 20 deletes: not more than 20
        a.run("pull")
        self.assertEqual(len(a.trash()), 20)

    def test_type_clash_kept_both_and_run_continues(self):     # spec test 24, first half
        a, = self.synced("a")
        a.write("skills/foo/inner.md", "local dir\n")           # remote adds a file at skills/foo
        (a.claude / "skills").mkdir(parents=True, exist_ok=True)
        a.write("skills/bar", "local file\n")                    # remote adds skills/bar/SKILL.md
        self.remote_commit({"skills/foo": "remote file\n", "skills/bar/SKILL.md": "remote dir file\n",
                            REPO + "x.md": "other change\n"})
        r = a.run("pull")
        self.assertEqual(a.read("skills/foo/inner.md"), "local dir\n")
        self.assertEqual(a.read("skills/bar"), "local file\n")
        self.assertEqual(a.read(LOCAL + "x.md"), "other change\n")
        self.assertEqual(a.conflicts(), ["skills/bar/SKILL.md", "skills/foo"])
        self.assertIn("2 conflicts kept both", r.stdout)
        self.assertEqual(a.base(), a.git("rev-parse", "origin/main"))
        r2 = a.run("pull")
        self.assertEqual(r2.stdout, "")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_other_write_error_stops_before_base(self):        # spec test 24, second half
        a, = self.synced("a")
        self.remote_commit({"skills/ro/a.md": "v1\n"})
        a.run("pull")
        target_dir = a.claude / "skills/ro"
        base_before = a.base()
        self.remote_commit({"skills/ro/a.md": "v2\n"})
        os.chmod(target_dir, 0o555)
        self.addCleanup(os.chmod, target_dir, 0o755)
        try:
            r = a.run("pull", expect=1)
        finally:
            os.chmod(target_dir, 0o755)
        self.assertEqual(a.base(), base_before)
        self.assertEqual(a.conflicts(), [])
        self.assertIn("claude-sync: ERROR cannot write skills/ro/a.md", r.stdout)
        a.run("pull")
        self.assertEqual(a.read("skills/ro/a.md"), "v2\n")

    def test_pending_landed_moves_base(self):                  # spec test 27, refs variant
        a, = self.synced("a")
        base_before = a.base()
        (a.repo / "data/skills/s").mkdir(parents=True)
        (a.repo / "data/skills/s/mine.md").write_text("mine\n")
        a.git("add", "-A", "--", "data")
        a.git("commit", "-q", "-m", "sync: by hand")
        pushed = a.git("rev-parse", "HEAD")
        a.git("push", "-q", "origin", "HEAD:main")
        a.git("update-ref", "refs/claude-sync/pending", pushed)
        self.assertEqual(a.base(), base_before)                 # the push "landed" but base never moved
        r = a.run("pull")
        self.assertEqual(a.base(), pushed)
        self.assertEqual(a.conflicts(), [])
        self.assertEqual(r.stdout, "")
        self.assertIsNone(a.ref("refs/claude-sync/pending"))
        self.assertEqual(a.git("rev-parse", "HEAD"), pushed)

    def test_exec_bit_chmod_only_pulled(self):                 # spec test 25, pull half
        a, = self.synced("a")
        self.remote_commit({"hooks/x.sh": (b"#!/bin/sh\necho\n", False)})
        a.run("pull")
        self.assertEqual((a.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o644)
        self.remote_commit({"hooks/x.sh": (b"#!/bin/sh\necho\n", True)})
        a.run("pull")
        self.assertEqual((a.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o755)
        self.remote_commit({"hooks/x.sh": (b"#!/bin/sh\necho\n", False)})
        a.run("pull")
        self.assertEqual((a.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o644)
        self.assertEqual(a.conflicts(), [])

    def test_both_added_bit_difference_no_base(self):          # keeps local bit; with marker, remote bit
        self.remote_commit({"hooks/x.sh": (b"#!/bin/sh\necho\n", True)})
        plain = self.machine("plain")
        plain.write("hooks/x.sh", b"#!/bin/sh\necho\n", exec_bit=False)
        plain.run("pull")
        self.assertEqual((plain.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o644)
        marked = self.machine("marked")
        marked.write("hooks/x.sh", b"#!/bin/sh\necho\n", exec_bit=False)
        (marked.repo / ".fresh-machine").touch()
        marked.run("pull")
        self.assertEqual((marked.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual(plain.conflicts() + marked.conflicts(), [])

    def test_pull_never_writes_through_symlinked_dir(self):    # Review Focus 2, spec test 34
        a, = self.synced("a")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "real.md").write_text("real\n")
        (a.claude / "skills").mkdir(parents=True, exist_ok=True)
        os.symlink(outside, a.claude / "skills/linked")
        (a.claude / "CLAUDE.md").unlink()
        os.symlink(outside / "real.md", a.claude / "CLAUDE.md")
        real_before = (outside / "real.md").read_text()
        self.remote_commit({"skills/linked/SKILL.md": "remote skill\n", "CLAUDE.md": "# shared\nremote edit\n"})
        a.run("pull")
        self.assertFalse((outside / "SKILL.md").exists())
        self.assertEqual((outside / "real.md").read_text(), real_before)
        self.assertEqual(os.listdir(outside), ["real.md"])
        self.assertTrue((a.claude / "CLAUDE.md").is_symlink())
        self.assertTrue(any("skipped 2 paths under symlinks" in ln for ln in a.log_lines()))
        self.assertEqual(a.trash(), [])
        self.assertEqual(a.base(), a.git("rev-parse", "origin/main"))

    def test_rewritten_remote_history(self):                   # spec 6.7 step 4: M is the base
        x_rel = LOCAL + "x.md"
        c1 = self.remote_commit({REPO + "x.md": "v1\n"})
        self.remote_commit({REPO + "x.md": "v2\n"})            # c2
        a, = self.synced("a")                                  # base at c2, x.md v2 applied locally
        self.assertEqual(a.read(x_rel), "v2\n")
        self._git(self.remote, "update-ref", "refs/heads/main", c1)     # force-reset the remote to c1
        self.remote_commit({REPO + "y.md": "new\n"})           # c3 on top of c1
        a.run("pull")
        self.assertEqual(a.read(x_rel), "v1\n")                # differs between base and the new tip only
        self.assertEqual(a.read(LOCAL + "y.md"), "new\n")
        self.assertTrue(any(" INFO remote history was rewritten" in ln for ln in a.log_lines()))
        self.assertEqual(a.base(), self.remote_tip())

    def test_remote_symlink_entry_stops(self):
        a, = self.synced("a")
        base_before = a.base()
        self.remote_commit({"skills/s/ok.md": "ok\n"})
        other = self.root / "_other"
        os.symlink("elsewhere", other / "data/skills/s/link")
        self._git(other, "add", "-A")
        self._git(other, "commit", "-q", "-m", "link")
        self._git(other, "push", "-q", "origin", "HEAD:main")
        r = a.run("pull", expect=1)
        self.assertIn("claude-sync: ERROR unsupported entry in data/: skills/s/link mode 120000", r.stdout)
        self.assertEqual(a.base(), base_before)
        self.assertIsNone(a.read("skills/s/ok.md"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_local_file_stops(self):
        a, = self.synced("a")
        rel = LOCAL + "x.md"
        self.remote_commit({REPO + "x.md": "v1\n"})
        a.run("pull")
        base_before = a.base()
        self.remote_commit({REPO + "x.md": "v2\n"})
        os.chmod(a.claude / rel, 0)
        self.addCleanup(os.chmod, a.claude / rel, 0o644)
        r = a.run("pull", expect=1)
        self.assertEqual(a.base(), base_before)
        self.assertIn(f"claude-sync: ERROR cannot read {rel}", r.stdout)

    def test_merge_error_stops_run(self):
        a, = self.synced("a")
        rel = LOCAL + "x.md"
        self.remote_commit({REPO + "x.md": "a\nb\nc\n"})
        a.run("pull")
        base_before = a.base()
        a.write(rel, b"a\nb\nc\n\x00binary\n")
        self.remote_commit({REPO + "x.md": "a\nb\nc\nremote\n"})
        r = a.run("pull", expect=1)
        self.assertEqual(a.base(), base_before)
        self.assertIn(f"claude-sync: ERROR merge failed for {rel}", r.stdout)


if __name__ == "__main__":
    unittest.main()
