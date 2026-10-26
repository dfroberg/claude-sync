from __future__ import annotations

import os
import subprocess
import time
import unittest.mock
from pathlib import Path

from claude_sync import config
from claude_sync.checks import SkipRun, StopRun, ensure_unlocked, prepare, restore_invariant
from claude_sync.context import BASE_REF, Ctx, Report
from claude_sync.gitio import Git
from claude_sync.log import Log
from tests.harness import SyncTestCase


class ChecksTest(SyncTestCase):
    def ctx(self, m):
        cfg = config.load(m.env())
        log = Log(m.repo / "last-sync.log", "pull", True)
        return Ctx(cfg, log, Git(m.repo, log, base_env=m.env()), Report(), "2026-10-25T16-31-02Z")

    def log_text(self, m):
        return "\n".join(m.log_lines())

    def age(self, path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t))

    def test_stale_index_lock_removed(self):
        m = self.machine("a")
        lock = m.repo / ".git" / "index.lock"
        lock.write_text("")
        self.age(lock, 120)
        prepare(self.ctx(m))
        self.assertFalse(lock.exists())
        self.assertIn("WARN", self.log_text(m))

    def test_fresh_index_lock_kept(self):
        m = self.machine("a")
        lock = m.repo / ".git" / "index.lock"
        lock.write_text("")
        self.age(lock, 5)
        with self.assertRaises(SkipRun):
            prepare(self.ctx(m))
        self.assertTrue(lock.exists())

    def test_unremovable_stale_index_lock_stops(self):
        m = self.machine("a")
        lock = m.repo / ".git" / "index.lock"
        lock.write_text("")
        self.age(lock, 120)
        with unittest.mock.patch("claude_sync.gitio.os.unlink", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(StopRun) as cm:
                prepare(self.ctx(m))
        self.assertIn("could not be removed", str(cm.exception))
        self.assertTrue(lock.exists())

    def _dead_rebase(self, m, sha_text):
        d = m.repo / ".git" / "rebase-merge"
        d.mkdir()
        (d / "autostash").write_text(sha_text + "\n")
        return d

    def test_dead_rebase_removed_and_autostash_kept(self):
        m = self.machine("a")
        (m.repo / "data" / "CLAUDE.md").write_text("# edited\n")
        sha = m.git("stash", "create")
        self.assertTrue(sha)
        m.git("checkout", "--", "data")
        d = self._dead_rebase(m, sha)
        self.assertEqual(m.git("stash", "list"), "")
        prepare(self.ctx(m))
        self.assertFalse(d.exists())
        self.assertIn("autostash of a killed sync run", m.git("stash", "list"))

    def test_dead_rebase_apply_removed(self):
        m = self.machine("a")
        d = m.repo / ".git" / "rebase-apply"
        d.mkdir()
        prepare(self.ctx(m))
        self.assertFalse(d.exists())

    def test_dead_rebase_with_bad_autostash_still_cleared(self):
        m = self.machine("a")
        plain = m.git("rev-parse", "HEAD")                      # a plain commit, not a stash commit
        d = self._dead_rebase(m, plain)
        prepare(self.ctx(m))
        self.assertFalse(d.exists())
        self.assertNotIn("autostash of a killed sync run", m.git("stash", "list"))
        self.assertIn("WARN", self.log_text(m))
        d2 = self._dead_rebase(m, "not-a-sha")
        prepare(self.ctx(m))
        self.assertFalse(d2.exists())

    def test_detached_head_keeps_bin_edit_and_stops(self):
        m = self.machine("a")
        m.git("checkout", "-q", "--detach")
        (m.repo / "bin" / "README").write_text("hand edit\n")
        with self.assertRaises(StopRun):
            prepare(self.ctx(m))
        self.assertEqual(m.git("symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual((m.repo / "bin" / "README").read_text(), "hand edit\n")

    def test_detached_head_reattached(self):
        m = self.machine("a")
        head = m.git("rev-parse", "HEAD")
        m.git("checkout", "-q", "--detach")
        prepare(self.ctx(m))
        self.assertEqual(m.git("symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(m.git("rev-parse", "main"), head)

    def test_head_on_other_branch_reattached(self):
        m = self.machine("a")
        m.git("checkout", "-q", "-b", "other")
        prepare(self.ctx(m))
        self.assertEqual(m.git("symbolic-ref", "HEAD"), "refs/heads/main")

    def test_dirty_bin_stops(self):                          # spec test 20, second half
        m = self.machine("a")
        (m.repo / "bin" / "README").write_text("hand edit\n")
        with self.assertRaises(StopRun) as cm:
            prepare(self.ctx(m))
        self.assertIn("tracked files outside data/ are modified", str(cm.exception))
        self.assertIn("bin/README", str(cm.exception))

    def test_restore_removes_untracked_and_tmp(self):        # spec test 26, unit half
        m = self.machine("a")
        (m.repo / "data" / "stray.md").write_text("x")
        (m.repo / "data" / "CLAUDE.md.sync-tmp.99").write_text("y")
        prepare(self.ctx(m))
        self.assertFalse((m.repo / "data" / "stray.md").exists())
        self.assertFalse((m.repo / "data" / "CLAUDE.md.sync-tmp.99").exists())
        self.assertEqual(m.git("status", "--porcelain"), "")

    def test_restore_removes_ignored_tmp_only(self):         # spec test 26
        m = self.machine("a")
        (m.repo / "data" / "x.md.sync-tmp.7").write_text("y")
        self.assertEqual(m.git("status", "--porcelain", "--", "data"), "")    # ignored: invisible to porcelain
        prepare(self.ctx(m))
        self.assertFalse((m.repo / "data" / "x.md.sync-tmp.7").exists())

    def test_restore_resets_modified_data_to_base(self):
        m = self.machine("a")
        (m.repo / "data" / "CLAUDE.md").write_text("# edited\n")
        prepare(self.ctx(m))
        self.assertEqual((m.repo / "data" / "CLAUDE.md").read_text(), "# shared\n")

    def test_restore_resets_to_base(self):
        m = self.machine("a")
        base = m.git("rev-parse", "HEAD")
        m.git("update-ref", BASE_REF, base)
        (m.repo / "data" / "later.md").write_text("later\n")
        m.git("add", "-A")
        m.git("commit", "-q", "-m", "later")
        self.assertNotEqual(m.git("rev-parse", "HEAD"), base)
        prepare(self.ctx(m))
        self.assertEqual(m.git("rev-parse", "HEAD"), base)
        self.assertFalse((m.repo / "data" / "later.md").exists())
        self.assertIn("restored data/ to " + base[:7], self.log_text(m))

    def test_restore_without_base_keeps_head(self):
        m = self.machine("a")
        (m.repo / "data" / "later.md").write_text("later\n")
        m.git("add", "-A")
        m.git("commit", "-q", "-m", "later")
        head = m.git("rev-parse", "HEAD")
        restore_invariant(self.ctx(m))
        self.assertEqual(m.git("rev-parse", "HEAD"), head)
        self.assertTrue((m.repo / "data" / "later.md").exists())

    def _gitattributes_remote(self):
        self.remote_commit({".gitattributes": "data/** filter=git-crypt diff=git-crypt\n"}, root_relative=True)

    def test_locked_repo_without_key_stops(self):
        self._gitattributes_remote()
        m = self.machine("a")
        with self.assertRaises(StopRun) as cm:
            prepare(self.ctx(m))
        self.assertIn("repo is git-crypt locked and no usable key at " + str(m.key), str(cm.exception))

    def test_keys_without_filter_config_counts_as_locked(self):
        self._gitattributes_remote()
        m = self.machine("a")
        keys = m.repo / ".git" / "git-crypt" / "keys"
        keys.mkdir(parents=True)
        (keys / "default").write_text("not a real key")
        with self.assertRaises(StopRun):
            ensure_unlocked(self.ctx(m))

    def test_plain_repo_needs_no_unlock(self):
        m = self.machine("a")
        ensure_unlocked(self.ctx(m))

    def test_bad_key_stops_and_is_never_logged(self):
        self._gitattributes_remote()
        m = self.machine("a")
        m.key.write_text("SECRET-KEY-BYTES-12345")
        with self.assertRaises(StopRun):
            prepare(self.ctx(m))
        self.assertNotIn("SECRET-KEY-BYTES-12345", self.log_text(m))

    def test_unlock_with_real_key(self):
        key = self.make_gitcrypt_remote()
        m = self.machine("a")
        self.assertTrue((m.repo / "data" / "CLAUDE.md").read_bytes().startswith(b"\x00GITCRYPT"))
        m.key.write_bytes(key.read_bytes())
        prepare(self.ctx(m))
        self.assertEqual((m.repo / "data" / "CLAUDE.md").read_text(), "# shared\n")

    def test_report_notice_and_summary(self):
        home = Path("/h/me")
        r = Report()
        self.assertIsNone(r.notice(home))
        self.assertFalse(r.noop)
        self.assertEqual(r.summary("pull", 0.84),
                         "pull done in 0.8s: 0 remote changes, 0 written, 0 merged, 0 kept-both, 0 trashed")
        r.remote_changes, r.written, r.merged, r.kept_both, r.trashed, r.pushed = 3, 2, 1, 0, 4, 5
        self.assertEqual(r.summary("pull", 12.34),
                         "pull done in 12.3s: 3 remote changes, 2 written, 1 merged, 0 kept-both, 4 trashed")
        self.assertEqual(r.summary("push", 1.0),
                         "push done in 1.0s: 3 remote changes, 2 written, 1 merged, 0 kept-both, 4 trashed, 5 pushed")
        r.local_wins = 2
        self.assertEqual(r.notice(home), "claude-sync: 2 local edits kept over remote deletes (see last-sync.log)")
        r.kept_both = 2
        r.conflicts_dir = home / ".claude-sync" / "conflicts" / "2026-10-25T16-31-02Z"
        self.assertEqual(r.notice(home), "claude-sync: 2 conflicts kept both, remote copies in "
                         "~/.claude-sync/conflicts/2026-10-25T16-31-02Z (see last-sync.log)")
        r.fail("boom")
        r.fail("second")
        self.assertEqual((r.errors, r.first_error), (2, "boom"))
        self.assertEqual(r.notice(home), "claude-sync: ERROR boom (see ~/.claude-sync/last-sync.log)")
        self.assertEqual(Report().first_error, None)

    def test_report_notice_took_remote(self):
        home = Path("/h/me")
        folder = home / ".claude-sync" / "conflicts" / "2026-10-25T16-31-02Z"
        r = Report()
        r.took_remote = 3
        r.conflicts_dir = folder
        d = "~/.claude-sync/conflicts/2026-10-25T16-31-02Z"
        self.assertEqual(r.notice(home), f"claude-sync: 3 files replaced by the remote version, "
                         f"local copies in {d} (see last-sync.log)")
        r.kept_both = 2
        self.assertEqual(r.notice(home), f"claude-sync: 2 conflicts kept both and 3 files replaced by the "
                         f"remote version, copies in {d} (see last-sync.log)")

    def test_report_summary_took_remote_only_when_nonzero(self):
        r = Report()
        self.assertNotIn("replaced by remote", r.summary("pull", 1.0))
        r.took_remote = 3
        self.assertEqual(r.summary("pull", 1.0),
                         "pull done in 1.0s: 0 remote changes, 0 written, 0 merged, 0 kept-both, 0 trashed"
                         ", 3 replaced by remote")
        self.assertEqual(r.summary("push", 1.0),
                         "push done in 1.0s: 0 remote changes, 0 written, 0 merged, 0 kept-both, 0 trashed"
                         ", 3 replaced by remote, 0 pushed")

    def test_ctx_roots_and_checkpoint(self):
        m = self.machine("a")
        ctx = self.ctx(m)
        self.assertEqual(ctx.conflicts_root, m.repo / "conflicts" / "2026-10-25T16-31-02Z")
        self.assertEqual(ctx.trash_root, m.repo / "trash" / "2026-10-25T16-31-02Z")
        ctx.checkpoint("pull:before-replace")                   # variable unset: no-op
        self.assertFalse((m.repo / ".hold-pull:before-replace").exists())
