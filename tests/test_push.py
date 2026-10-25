from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest

from tests.harness import SyncTestCase, wait_for

LOCAL = "projects/-Users-alice-p/memory/"      # under ~/.claude
REPO = "memory/-Users-__USER__-p/"             # under data/
PENDING = "refs/claude-sync/pending"


class PushTest(SyncTestCase):
    def remote_text(self, repo_rel: str) -> str:
        return self._git(self.remote, "show", f"main:data/{repo_rel}", binary=True).decode()

    def remote_mode(self, repo_rel: str) -> str:
        return self._git(self.remote, "ls-tree", "main", f"data/{repo_rel}").split()[0]

    def go_offline(self, m, push_only: bool = False):
        """Point origin (or only its push url) at an ssh that cannot resolve its host."""
        fake = self.root / "fakessh"
        fake.write_text("#!/bin/sh\necho 'ssh: Could not resolve hostname example.invalid: "
                        "nodename nor servname provided, or not known' >&2\nexit 255\n")
        fake.chmod(0o755)
        if push_only:
            m.git("config", "remote.origin.pushurl", "ssh://example.invalid/x")
        else:
            m.git("remote", "set-url", "origin", "ssh://example.invalid/x")
        return m.env(GIT_SSH_COMMAND=str(fake))

    def go_online(self, m):
        m.git("remote", "set-url", "origin", f"file://{self.remote}")
        subprocess.run(["git", "-C", str(m.repo), "config", "--unset", "remote.origin.pushurl"],
                       env=m.env(), capture_output=True)

    def run_held(self, m, point: str, between, *args: str):
        """Start `claude-sync <args>` held at `point`, call between() while it waits, then release it."""
        hold = m.repo / f".hold-{point}"
        p = m.popen(*args, env=m.env(CLAUDE_SYNC_TEST_HOLD=point))
        try:
            wait_for(hold)
            between()
            hold.unlink()
            out, _err = p.communicate(timeout=90)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        return p.returncode, out

    def test_dod_two_machines(self):                         # spec test 1 end to end
        a, b = self.synced("a", "b")
        other_rel = LOCAL + "b.md"
        a.write("CLAUDE.md", "edit on a\n")
        a.run("push")
        self.assertEqual(self.remote_text("CLAUDE.md"), "edit on a\n")
        self.assertEqual(a.base(), self.remote_tip())
        self.assertIsNone(a.ref(PENDING))
        self.assertEqual(a.git("status", "--porcelain", "--ignored", "--", "data"), "")
        b.write(other_rel, "edit on b\n")
        b.run("pull")
        self.assertEqual(b.read("CLAUDE.md"), "edit on a\n")
        self.assertEqual(b.read(other_rel), "edit on b\n")
        self.assertTrue(any(" INFO push done" in ln and "1 pushed" in ln for ln in a.log_lines()))

    def test_noop_push_touches_no_network(self):
        a, = self.synced("a")
        os.rename(self.root / "remote.git", self.root / "gone.git")
        r = a.run("push")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertFalse(any(" WARN " in ln or " ERROR " in ln or " INFO push done" in ln
                             for ln in a.log_lines()))

    def test_local_delete_trashed_on_other_machine(self):    # spec test 8
        a, b = self.synced("a", "b")
        self.remote_commit({"skills/s/a.md": "a\n", "skills/s/keep.md": "k\n"})
        a.run("pull")
        b.run("pull")
        os.unlink(a.claude / "skills/s/a.md")
        a.run("push")
        self.assertEqual(self.remote_paths(), sorted(["CLAUDE.md", "skills/s/keep.md"]))
        r = b.run("pull")
        self.assertIsNone(b.read("skills/s/a.md"))
        self.assertEqual(b.read("skills/s/keep.md"), "k\n")
        self.assertEqual(b.trash(), ["skills/s/a.md"])
        self.assertEqual(r.stdout, "")

    def test_local_mass_delete_refused_then_allowed(self):   # spec test 9, local half
        a, = self.synced("a")
        rels = [f"skills/s/f{i}.md" for i in range(30)]
        self.remote_commit({r: f"{i}\n" for i, r in enumerate(rels)})
        a.run("pull")
        for r in rels[:25]:
            os.unlink(a.claude / r)
        tip, base = self.remote_tip(), a.base()
        r = a.run("push", expect=1)
        self.assertIn("claude-sync: ERROR refusing to push: would delete 25 files and keep only 6", r.stdout)
        self.assertLessEqual(len(r.stdout.rstrip("\n")), 200)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(a.base(), base)
        self.assertEqual(a.git("status", "--porcelain", "--", "data"), "")
        a.run("push", env=a.env(CLAUDE_SYNC_ALLOW_PRUNE="1"))
        self.assertEqual(self.remote_paths(), sorted(["CLAUDE.md"] + rels[25:]))

    def test_local_delete_not_outnumbering_kept_files_pushes(self):
        a, = self.synced("a")
        rels = [f"skills/s/f{i}.md" for i in range(60)]
        self.remote_commit({r: f"{i}\n" for i, r in enumerate(rels)})
        a.run("pull")
        for r in rels[:21]:                                   # 21 deletes, 40 files kept
            os.unlink(a.claude / r)
        a.run("push")
        self.assertEqual(len(self.remote_paths()), 40)

    def test_push_without_base_pulls_first(self):            # spec test 10, push half
        b = self.machine("b")
        b.write(LOCAL + "only.md", "mine\n")
        tip = self.remote_tip()
        env = self.go_offline(b)
        r = b.run("push", env=env)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.remote_tip(), tip)
        self.assertIsNone(b.base())
        self.assertEqual(sum(" WARN " in ln for ln in b.log_lines()), 1)
        self.go_online(b)
        b.run("push")
        self.assertIsNotNone(b.base())
        self.assertIn(REPO + "only.md", self.remote_paths())
        self.assertEqual(b.read("CLAUDE.md"), "# shared\n")
        self.assertEqual(b.base(), self.remote_tip())

    def test_empty_file_guard(self):                         # spec test 17
        a, = self.synced("a")
        rel, other = LOCAL + "x.md", LOCAL + "y.md"
        self.remote_commit({REPO + "x.md": "content\n"})
        a.run("pull")
        a.write(rel, "")                                      # young: maybe a write in progress
        a.write(other, "y\n")
        a.run("push")
        self.assertEqual(a.read(rel), "")
        self.assertEqual(self.remote_text(REPO + "x.md"), "content\n")
        self.assertEqual(self.remote_text(REPO + "y.md"), "y\n")
        self.assertEqual(a.trash(), [])
        t = time.time() - 11 * 60                             # old: restored, the empty file goes to trash
        os.utime(a.claude / rel, (t, t))
        tip = self.remote_tip()
        a.run("push")
        self.assertEqual(a.read(rel), "content\n")
        self.assertEqual(a.trash(), [rel])
        stamp = next((a.repo / "trash").iterdir())
        self.assertEqual((stamp / rel).read_bytes(), b"")
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.remote_text(REPO + "x.md"), "content\n")
        self.assertTrue(any(" WARN " in ln and "x.md" in ln for ln in a.log_lines()))

    def test_exec_bit_pushed(self):                          # spec test 25, push half
        a, b = self.synced("a", "b")
        a.write("hooks/x.sh", b"#!/bin/sh\necho\n", exec_bit=True)
        a.run("push")
        self.assertEqual(self.remote_mode("hooks/x.sh"), "100755")
        b.run("pull")
        self.assertEqual((b.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o755)
        os.chmod(a.claude / "hooks/x.sh", 0o644)              # chmod only
        a.run("push")
        self.assertEqual(self.remote_mode("hooks/x.sh"), "100644")
        b.run("pull")
        self.assertEqual((b.claude / "hooks/x.sh").stat().st_mode & 0o777, 0o644)
        self.assertEqual(b.conflicts(), [])

    def test_leftovers_never_committed(self):                # spec test 26
        a, = self.synced("a")
        stray = a.repo / "data/stray.md"
        tmp = a.repo / "data/CLAUDE.md.sync-tmp.4242"
        stray.write_text("stray\n")
        tmp.write_text("tmp\n")
        a.run("push")                                         # nothing to send, leftovers still go
        self.assertFalse(stray.exists() or tmp.exists())
        stray.write_text("stray\n")
        tmp.write_text("tmp\n")
        a.write(LOCAL + "real.md", "real\n")
        a.run("push")
        self.assertEqual(self.remote_paths(), sorted(["CLAUDE.md", REPO + "real.md"]))
        self.assertFalse(stray.exists() or tmp.exists())
        self.assertEqual(a.git("status", "--porcelain", "--ignored", "--", "data"), "")

    def test_rejected_push_keeps_local_edit(self):           # spec test 30
        a, b = self.synced("a", "b")
        rel = LOCAL + "n.md"
        self.remote_commit({REPO + "n.md": "1\n2\n3\n4\n5\n"})
        a.run("pull")
        b.run("pull")
        a.write(rel, "1x\n2\n3\n4\n5\n")

        def b_pushes():
            b.write(rel, "1\n2\n3\n4\n5x\n")
            b.run("push")

        rc, out = self.run_held(a, "push:before-push", b_pushes, "push")
        self.assertEqual(rc, 0)
        merged = "1x\n2\n3\n4\n5x\n"
        self.assertEqual(a.read(rel), merged)
        self.assertNotEqual(a.read(rel), "1\n2\n3\n4\n5x\n")
        self.assertEqual(self.remote_text(REPO + "n.md"), merged)
        self.assertEqual(a.base(), self.remote_tip())
        self.assertEqual(a.conflicts(), [])
        self.assertTrue(any("push rejected" in ln for ln in a.log_lines()))
        b.run("pull")
        self.assertEqual(b.read(rel), merged)

    def test_rejected_push_conflict_keeps_both(self):       # spec test 30, same lines
        a, b = self.synced("a", "b")
        rel = LOCAL + "n.md"
        self.remote_commit({REPO + "n.md": "1\n2\n3\n"})
        a.run("pull")
        b.run("pull")
        a.write(rel, "1\nlocal\n3\n")

        def b_pushes():
            b.write(rel, "1\nremote\n3\n")
            b.run("push")

        rc, out = self.run_held(a, "push:before-push", b_pushes, "push")
        self.assertEqual(rc, 0)
        self.assertEqual(a.read(rel), "1\nlocal\n3\n")
        self.assertEqual(a.conflicts(), [rel])
        stamp = next((a.repo / "conflicts").iterdir())
        self.assertEqual((stamp / rel).read_text(), "1\nremote\n3\n")
        self.assertEqual(self.remote_text(REPO + "n.md"), "1\nlocal\n3\n")
        self.assertRegex(out, r"^claude-sync: 1 conflicts kept both")

    def test_rejected_push_different_files_both_land(self):  # spec test 30 variant
        a, b = self.synced("a", "b")
        mine, theirs = LOCAL + "mine.md", LOCAL + "theirs.md"
        a.write(mine, "from a\n")

        def b_pushes():
            b.write(theirs, "from b\n")
            b.run("push")

        rc, out = self.run_held(a, "push:before-push", b_pushes, "push")
        self.assertEqual(rc, 0)
        self.assertEqual(self.remote_text(REPO + "mine.md"), "from a\n")
        self.assertEqual(self.remote_text(REPO + "theirs.md"), "from b\n")
        self.assertEqual(a.read(theirs), "from b\n")
        self.assertEqual(a.conflicts(), [])
        b.run("pull")
        self.assertEqual(b.read(mine), "from a\n")
        self.assertEqual(a.base(), self.remote_tip())

    def test_push_never_deletes_symlinked_paths(self):       # Review Focus 2, spec test 34
        a, = self.synced("a")
        self.remote_commit({"skills/linked/SKILL.md": "remote skill\n"})
        a.run("pull")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("outside\n")
        shutil.rmtree(a.claude / "skills/linked")
        os.symlink(outside, a.claude / "skills/linked")
        a.write("CLAUDE.md", "# shared\nlocal edit\n")       # so a push really happens
        a.run("push")
        self.assertIn("skills/linked/SKILL.md", self.remote_paths())
        self.assertEqual(self.remote_text("CLAUDE.md"), "# shared\nlocal edit\n")
        self.assertEqual((outside / "SKILL.md").read_text(), "outside\n")
        self.assertTrue(any("skipped 1 paths under symlinks" in ln for ln in a.log_lines()))

    def test_push_after_unseen_landed_push_is_silent(self):  # spec test 27, push variant
        a, = self.synced("a")
        rel = LOCAL + "n.md"
        a.write(rel, "mine\n")
        (a.repo / "data" / REPO).mkdir(parents=True)
        (a.repo / "data" / REPO / "n.md").write_text("mine\n")
        a.git("add", "-A", "--", "data")
        a.git("commit", "-q", "-m", "sync: by hand")
        pushed = a.git("rev-parse", "HEAD")
        a.git("push", "-q", "origin", "HEAD:main")
        a.git("update-ref", PENDING, pushed)                   # landed, but the base never moved
        r = a.run("push")
        self.assertEqual(r.stdout, "")
        self.assertEqual(a.base(), pushed)
        self.assertIsNone(a.ref(PENDING))
        self.assertEqual(a.conflicts(), [])
        self.assertEqual(self.remote_tip(), pushed)
        self.assertFalse(any(" ERROR " in ln for ln in a.log_lines()))

    def test_odd_names_round_trip_push(self):                # Review Focus 3, push half
        a, b = self.synced("a", "b")
        a.write(LOCAL + "MEMORY notes.md", "a\n")
        a.write(LOCAL + "résumé.md", "1\n2\n3\n")
        a.run("push")
        self.assertIn(REPO + "MEMORY notes.md", self.remote_paths())
        self.assertIn(REPO + "résumé.md", self.remote_paths())
        b.run("pull")
        self.assertEqual(b.read(LOCAL + "MEMORY notes.md"), "a\n")
        self.assertEqual(b.read(LOCAL + "résumé.md"), "1\n2\n3\n")
        os.unlink(a.claude / (LOCAL + "résumé.md"))
        a.run("push")
        self.assertNotIn(REPO + "résumé.md", self.remote_paths())
        b.run("pull")
        self.assertIsNone(b.read(LOCAL + "résumé.md"))
        self.assertEqual(b.trash(), [LOCAL + "résumé.md"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_push_stops_when_inner_pull_fails(self):         # spec test 32
        a, = self.synced("a")
        self.remote_commit({"skills/ro/a.md": "v1\n"})
        a.run("pull")
        target_dir = a.claude / "skills/ro"
        a.write("CLAUDE.md", "# shared\nlocal edit\n")
        self.remote_commit({"skills/ro/a.md": "v2\n"})
        base_before = a.base()
        os.chmod(target_dir, 0o555)
        self.addCleanup(os.chmod, target_dir, 0o755)
        tip = self.remote_tip()
        r = a.run("push", expect=1)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(a.base(), base_before)
        self.assertEqual(a.git("status", "--porcelain", "--", "data"), "")
        self.assertEqual(a.git("rev-parse", "HEAD"), base_before)
        self.assertIn("claude-sync: ERROR", r.stdout)
        self.assertEqual(a.read("CLAUDE.md"), "# shared\nlocal edit\n")

    def test_push_completes_killed_first_pull(self):         # spec test 33
        self.remote_commit({"skills/s/r.md": "r\n"})
        a = self.machine("a")
        a.write(LOCAL + "only.md", "only\n")
        hold = a.repo / ".hold-pull:before-base"
        p = a.popen("pull", env=a.env(CLAUDE_SYNC_TEST_HOLD="pull:before-base"))
        try:
            wait_for(hold)
        finally:
            p.kill()
            p.communicate()
        self.assertIsNone(a.base())
        r = a.run("push", env=a.env(CLAUDE_SYNC_LOCK_WAIT="5"))     # must not wait on its own lock
        self.assertIsNotNone(a.base())
        self.assertIn(REPO + "only.md", self.remote_paths())
        self.assertEqual(a.read("skills/s/r.md"), "r\n")
        self.assertEqual(a.base(), self.remote_tip())
        self.assertEqual(r.stdout, "")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unreadable_dir_stops_push(self):                # spec test 35
        a, = self.synced("a")
        self.remote_commit({REPO + "x.md": "x\n"})
        a.run("pull")
        folder = a.claude / "projects/-Users-alice-p/memory"
        a.write("CLAUDE.md", "# shared\nlocal edit\n")       # a push would otherwise happen
        tip = self.remote_tip()
        os.chmod(folder, 0)
        self.addCleanup(os.chmod, folder, 0o755)
        try:
            r = a.run("push", expect=1)
        finally:
            os.chmod(folder, 0o755)
        self.assertEqual(self.remote_tip(), tip)
        self.assertIn(REPO + "x.md", self.remote_paths())
        self.assertIn("claude-sync: ERROR cannot scan", r.stdout)
        self.assertEqual(a.git("status", "--porcelain", "--", "data"), "")

    def test_nothing_staged_no_commit(self):                 # spec test 37
        a, = self.synced("a")
        self.remote_commit({REPO + "x.md": "content\n"})
        a.run("pull")
        a.write(LOCAL + "x.md", "")                           # young 0-byte file, non-empty data version
        tip, head = self.remote_tip(), a.git("rev-parse", "HEAD")
        r = a.run("push")
        self.assertEqual(self.remote_tip(), tip)
        self.assertIsNone(a.ref(PENDING))
        self.assertEqual(a.git("rev-parse", "HEAD"), head)
        self.assertEqual(r.stdout, "")
        self.assertEqual(a.read(LOCAL + "x.md"), "")

    def test_type_clash_directory_replaces_remote_file(self):  # spec test 24, push half
        a, b = self.synced("a", "b")
        a.write("skills/foo/inner.md", "local dir\n")
        self.remote_commit({"skills/foo": "remote file\n"})
        b.run("pull")
        self.assertEqual(b.read("skills/foo"), "remote file\n")
        a.run("pull")                                         # keep-both: the directory stays
        self.assertEqual(a.conflicts(), ["skills/foo"])
        a.run("push")
        self.assertEqual(self.remote_paths(), sorted(["CLAUDE.md", "skills/foo/inner.md"]))
        b.run("pull")
        self.assertEqual(b.read("skills/foo/inner.md"), "local dir\n")
        self.assertEqual(b.trash(), ["skills/foo"])

    def test_top_level_symlink_not_pushed(self):             # spec test 34
        a, = self.synced("a")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "real.md").write_text("outside\n")
        (a.claude / "CLAUDE.md").unlink()
        os.symlink(outside / "real.md", a.claude / "CLAUDE.md")
        a.write(LOCAL + "z.md", "z\n")                        # so a push really happens
        a.run("push")
        self.assertEqual(self.remote_text("CLAUDE.md"), "# shared\n")
        self.assertEqual(self.remote_text(REPO + "z.md"), "z\n")
        self.assertEqual((outside / "real.md").read_text(), "outside\n")
        self.assertTrue((a.claude / "CLAUDE.md").is_symlink())

    def test_push_offline_keeps_edit_and_retries(self):
        a, = self.synced("a")
        rel = LOCAL + "n.md"
        a.write(rel, "mine\n")
        env = self.go_offline(a, push_only=True)              # fetch works, the push itself is offline
        tip = self.remote_tip()
        r = a.run("push", env=env)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(a.read(rel), "mine\n")
        self.assertIsNotNone(a.ref(PENDING))
        self.assertEqual(a.git("rev-parse", "HEAD"), a.base())
        self.assertTrue(any(" WARN " in ln and "offline" in ln for ln in a.log_lines()))
        self.go_online(a)
        a.run("push")
        self.assertEqual(self.remote_text(REPO + "n.md"), "mine\n")
        self.assertIsNone(a.ref(PENDING))
        self.assertEqual(a.base(), self.remote_tip())

    def test_plaintext_guard_refuses_unencrypted_blob(self):
        key = self.make_gitcrypt_remote()
        a = self.machine("a")
        a.key.write_bytes(key.read_bytes())
        a.run("pull")
        self.assertEqual(a.read("CLAUDE.md"), "# shared\n")
        a.git("config", "filter.git-crypt.clean", "cat")      # the clean filter silently stops encrypting
        a.write(LOCAL + "n.md", "secret\n")
        tip = self.remote_tip()
        r = a.run("push", expect=1)
        self.assertIn("claude-sync: ERROR refusing to commit unencrypted data/", r.stdout)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(a.git("diff", "--cached", "--name-only"), "")
        self.assertEqual(a.git("rev-parse", "HEAD"), a.base())
        self.assertIsNone(a.ref(PENDING))      # `cat` as the clean filter also makes git see CLAUDE.md as modified


if __name__ == "__main__":
    unittest.main()
