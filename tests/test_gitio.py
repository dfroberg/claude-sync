from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from claude_sync.gitio import Change, Git, GitError, classify
from claude_sync.log import Log
from tests.harness import SyncTestCase


class GitioTest(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.m = self.machine("a")
        self.log = Log(self.root / "test.log", "test", True)
        self.g = Git(self.m.repo, self.log, base_env=self.m.env())

    def local_commit(self, rel, text, message="local"):
        p = self.m.repo / "data" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.g.add_data()
        return self.g.commit(message)

    def test_classify(self):
        self.assertEqual(classify("ssh: Could not resolve hostname github.com: nodename nor servname", False), "offline")  # substring match
        self.assertEqual(classify("fatal: unable to access: Could not resolve host: github.com", False), "offline")
        self.assertEqual(classify("ssh: connect to host github.com port 22: Operation timed out", False), "offline")
        self.assertEqual(classify("ERROR: Repository not found.", False), "error")
        self.assertEqual(classify("", True), "offline")
        for s in ["Network is unreachable", "Connection timed out", "Connection refused"]:
            self.assertEqual(classify(s, False), "offline", s)

    def test_git_error_str(self):
        e = GitError(["fetch", "origin"], 128, "hint: a\nhint: b\nl1\nl2\nl3\nl4\nl5\n")
        self.assertEqual((e.args_, e.rc), (["fetch", "origin"], 128))
        s = str(e)
        self.assertIn("fetch origin", s)
        self.assertNotIn("hint:", s)
        self.assertNotIn("l1", s)
        for line in ("l2", "l3", "l4", "l5"):
            self.assertIn(line, s)

    def test_diff_raw_add_modify_delete_mode(self):
        g = self.g
        c1 = self.remote_commit({"a.md": "1\n", "b.md": "1\n", "c.md": "1\n", "d.sh": (b"x\n", False)})
        c2 = self.remote_commit({"a.md": "2\n", "b.md": None, "e.md": "new\n", "d.sh": (b"x\n", True)})
        self.assertEqual(g.fetch("main"), "ok")
        ch = {c.path: c for c in g.diff_raw(c1, c2)}
        self.assertEqual(sorted(ch), ["a.md", "b.md", "d.sh", "e.md"])
        self.assertIsInstance(ch["a.md"], Change)
        a = ch["a.md"]
        self.assertEqual((a.status, a.old_mode, a.new_mode), ("M", "100644", "100644"))
        self.assertEqual(a.old_blob, self.m.git("rev-parse", f"{c1}:data/a.md"))
        self.assertEqual(a.new_blob, self.m.git("rev-parse", f"{c2}:data/a.md"))
        b = ch["b.md"]
        self.assertEqual((b.status, b.new_mode, b.new_blob), ("D", "000000", None))
        self.assertEqual(b.old_blob, self.m.git("rev-parse", f"{c1}:data/b.md"))
        e = ch["e.md"]
        self.assertEqual((e.status, e.old_mode, e.old_blob), ("A", "000000", None))
        self.assertEqual(len(e.new_blob), 40)
        d = ch["d.sh"]
        self.assertEqual((d.status, d.old_mode, d.new_mode), ("M", "100644", "100755"))
        self.assertEqual(d.old_blob, d.new_blob)

    def test_diff_raw_from_empty_tree(self):
        g = self.g
        self.remote_commit({"a.md": "1\n", "x/y.md": "2\n"})
        g.fetch("main")
        ch = g.diff_raw(g.empty_tree(), "origin/main")
        self.assertEqual({c.path for c in ch}, {"CLAUDE.md", "a.md", "x/y.md"})
        self.assertEqual({c.status for c in ch}, {"A"})

    def test_diff_raw_odd_names(self):  # Review Focus 3
        g = self.g
        self.remote_commit({"memory/-Users-__USER__-p/MEMORY notes.md": "a\n", "memory/-Users-__USER__-p/résumé.md": "b\n"})
        g.fetch("main")
        paths = {c.path for c in g.diff_raw(g.empty_tree(), "origin/main")}
        self.assertIn("memory/-Users-__USER__-p/MEMORY notes.md", paths)
        self.assertIn("memory/-Users-__USER__-p/résumé.md", paths)

    def test_tree_blobs(self):
        g = self.g
        self.remote_commit({"a.md": "1\n", "s/run.sh": (b"x\n", True), "memory/p/MEMORY notes.md": "n\n"})
        g.fetch("main")
        t = g.tree_blobs("origin/main")
        self.assertEqual(set(t), {"CLAUDE.md", "a.md", "s/run.sh", "memory/p/MEMORY notes.md"})
        self.assertEqual(t["a.md"], ("100644", self.m.git("rev-parse", "origin/main:data/a.md")))
        self.assertEqual(t["s/run.sh"][0], "100755")
        self.assertEqual(g.tree_blobs(g.empty_tree()), {})

    def test_checkout_to_runs_filter_from_attr_source(self):
        g, m = self.g, self.m
        self.remote_commit({".gitattributes": "data/** filter=up\n"}, root_relative=True)
        self.remote_commit({"a.txt": "hello\n"})
        m.git("pull", "-q", "origin", "main")
        m.git("config", "filter.up.smudge", "sed s/$/XXXX/")
        tmp = self.root / "tmpco"
        tmp.mkdir()
        g.checkout_to("HEAD", ["a.txt"], tmp)
        self.assertEqual((tmp / "data/a.txt").read_bytes(), b"helloXXXX\n")
        self.assertEqual(m.git("status", "--porcelain"), "")
        g.checkout_to("HEAD", [], tmp)  # no-op

    def test_checkout_to_literal_pathspec(self):
        g = self.g
        self.remote_commit({":weird*.md": "w\n", "plain.md": "p\n"})
        g.fetch("main")
        tmp = self.root / "tmpco"
        tmp.mkdir()
        g.checkout_to("origin/main", [":weird*.md"], tmp)
        self.assertEqual((tmp / "data" / ":weird*.md").read_text(), "w\n")
        self.assertFalse((tmp / "data" / "plain.md").exists())

    def test_fetch_updates_stale_tracking_ref(self):  # spec test 29
        g, m = self.g, self.m
        old = m.git("rev-parse", "origin/main")
        new = self.remote_commit({"x.md": "1\n"})
        m.git("update-ref", "refs/remotes/origin/main", old)
        self.assertEqual(g.fetch("main"), "ok")
        self.assertEqual(g.rev("origin/main"), new)

    def test_fetch_error_vs_offline(self):
        g, m = self.g, self.m
        m.git("remote", "set-url", "origin", "file:///nonexistent")
        self.assertEqual(g.fetch("main"), "error")
        self.assertTrue(any(" ERROR " in ln for ln in (self.root / "test.log").read_text().splitlines()))
        fake = self.root / "fakessh"
        fake.write_text("#!/bin/sh\nsleep 5\n")
        fake.chmod(0o755)
        m.git("remote", "set-url", "origin", "ssh://example.invalid/x")
        g2 = Git(m.repo, self.log, base_env=m.env(GIT_SSH_COMMAND=str(fake)))
        t0 = time.monotonic()
        self.assertEqual(g2.fetch("main", timeout=1), "offline")
        self.assertLess(time.monotonic() - t0, 3)

    def test_timeout_kills_group_and_clears_locks(self):
        g, m = self.g, self.m
        fake = self.root / "fakessh"
        fake.write_text("#!/bin/sh\nsleep 5\n")
        fake.chmod(0o755)
        m.git("remote", "set-url", "origin", "ssh://example.invalid/x")
        g2 = Git(m.repo, self.log, base_env=m.env(GIT_SSH_COMMAND=str(fake)))
        lock = m.repo / ".git" / "index.lock"
        lock.write_text("")
        with self.assertRaises(subprocess.TimeoutExpired):
            g2.run("fetch", "-q", "origin", timeout=1)
        self.assertFalse(lock.exists())

    def test_push_ok_and_rejected(self):
        g = self.g
        self.local_commit("mine.md", "1\n")
        self.assertEqual(g.push("main"), "ok")
        self.assertEqual(self.remote_tip(), g.rev("HEAD"))
        # a second machine view: remote moves ahead, then a local commit on the old tip is rejected
        self.remote_commit({"other.md": "x\n"})
        self.local_commit("mine2.md", "2\n")
        self.assertEqual(g.push("main"), "rejected")

    def test_push_error_and_offline(self):
        g, m = self.g, self.m
        m.git("remote", "set-url", "origin", "file:///nonexistent")
        self.assertEqual(g.push("main"), "error")
        fake = self.root / "fakessh"
        fake.write_text("#!/bin/sh\nsleep 5\n")
        fake.chmod(0o755)
        m.git("remote", "set-url", "origin", "ssh://example.invalid/x")
        g2 = Git(m.repo, self.log, base_env=m.env(GIT_SSH_COMMAND=str(fake)))
        self.assertEqual(g2.push("main", timeout=1), "offline")

    def test_inherited_git_env_ignored(self):  # spec 6.2, unit half of spec test 31
        m = self.m
        other = self.root / "other"
        subprocess.run(["git", "init", "-q", str(other)], env=m.env(), check=True)
        g2 = Git(m.repo, self.log, base_env=m.env(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other),
                                                  GIT_INDEX_FILE=str(other / ".git/index")))
        self.assertEqual(g2.rev("HEAD"), m.git("rev-parse", "HEAD"))
        g3 = Git(m.repo, self.log, base_env=m.env(
            GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=str(other),
            GIT_CONFIG_PARAMETERS="'core.bare'='true'"))
        self.assertEqual(g3.run("config", "core.hooksPath").stdout.strip(), b"/dev/null")  # injected config ignored

    def test_env_strip_list(self):
        g = Git(self.m.repo, self.log, base_env=self.m.env(
            GIT_CONFIG_PARAMETERS="'x.y'='z'", GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="a.b",
            GIT_CONFIG_VALUE_0="c", GIT_CONFIG_KEY_17="d.e", GIT_CONFIG_VALUE_17="f",
            GIT_ASKPASS="/bin/echo", SSH_ASKPASS="/bin/echo", GIT_SSH_COMMAND="ssh -v", GIT_DIR="/x"))
        env = g._env(None)
        for k in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                  "GIT_CONFIG_KEY_17", "GIT_CONFIG_VALUE_17", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR"):
            self.assertNotIn(k, env)
        self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -v")  # the user's own choice is kept

    def test_optional_queries_do_not_raise(self):
        g, m = self.g, self.m
        old = m.git("rev-parse", "HEAD")
        self.assertIsNone(g.rev("refs/nope"))
        self.assertEqual(g.rev("HEAD"), old)
        self.assertEqual(g.head_branch(), "main")
        new = self.local_commit("n.md", "n\n")
        self.assertTrue(g.is_ancestor(old, new))
        self.assertFalse(g.is_ancestor(new, old))
        with self.assertRaises(GitError):
            g.is_ancestor("refs/nope", new)
        m.git("checkout", "-q", "--detach")
        self.assertIsNone(g.head_branch())

    def test_refs(self):
        g, m = self.g, self.m
        sha = m.git("rev-parse", "HEAD")
        g.update_ref("refs/claude-sync/base", sha)
        self.assertEqual(m.base(), sha)
        g.delete_ref("refs/claude-sync/base")
        self.assertIsNone(m.base())
        g.delete_ref("refs/claude-sync/base")  # missing: no error
        self.assertEqual(g.empty_tree(), "4b825dc642cb6eb9a060e54bf8d69288fbee4904")

    def test_clear_locks_by_age(self):
        g, m = self.g, self.m
        lock = m.repo / ".git" / "index.lock"
        reflock = m.repo / ".git" / "refs" / "heads" / "main.lock"
        for p, age in ((lock, 120), (reflock, 120)):
            p.write_text("")
            os.utime(p, (time.time() - age, time.time() - age))
        removed = g.clear_locks(60)
        self.assertEqual(sorted(removed), sorted([str(lock), str(reflock)]))
        self.assertFalse(lock.exists())
        lock.write_text("")
        os.utime(lock, (time.time() - 5, time.time() - 5))
        self.assertEqual(g.clear_locks(60), [])
        self.assertTrue(lock.exists())
        self.assertEqual(g.clear_locks(0), [str(lock)])
        self.assertFalse(lock.exists())

    def test_work_tree_helpers(self):
        g, m = self.g, self.m
        self.assertFalse(g.data_dirty())
        self.assertEqual(g.dirty_outside_data(), [])
        (m.repo / "data" / "new.md").write_text("n\n")
        (m.repo / "data" / "junk.sync-tmp.1").write_text("j\n")
        self.assertTrue(g.data_dirty())
        self.assertFalse(g.staged_data())
        g.add_data()
        self.assertTrue(g.staged_data())
        before = g.rev("HEAD")
        after = g.commit("add: a message with 'quotes' and spaces")
        self.assertNotEqual(before, after)
        self.assertEqual(after, g.rev("HEAD"))
        self.assertEqual(m.git("log", "-1", "--format=%s"), "add: a message with 'quotes' and spaces")
        self.assertFalse(g.staged_data())
        self.assertTrue((m.repo / "data" / "junk.sync-tmp.1").exists())  # ignored by .gitignore, not committed
        self.assertNotIn("data/junk.sync-tmp.1", m.git("ls-tree", "-r", "--name-only", "HEAD"))
        (m.repo / "data" / "stray.md").write_text("s\n")
        g.clean_data()
        self.assertFalse((m.repo / "data" / "stray.md").exists())
        self.assertFalse((m.repo / "data" / "junk.sync-tmp.1").exists())
        (m.repo / "bin" / "README").write_text("edited\n")
        (m.repo / "data" / "CLAUDE.md").write_text("edited too\n")
        self.assertEqual(g.dirty_outside_data(), ["bin/README"])
        g.reset_hard("HEAD")
        self.assertEqual(g.dirty_outside_data(), [])
        self.assertEqual((m.repo / "data" / "CLAUDE.md").read_text(), "# shared\n")
