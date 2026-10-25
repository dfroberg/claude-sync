from __future__ import annotations

import subprocess

from tests.harness import REAL_HOME, SyncTestCase, assert_not_real


class HarnessTest(SyncTestCase):
    def test_refuses_real_paths(self):
        for p in [REAL_HOME / ".claude", REAL_HOME / ".claude-sync" / "x", REAL_HOME / ".config/git-crypt/k"]:
            with self.assertRaises(AssertionError):
                assert_not_real(p)

    def test_env_is_hermetic(self):
        m = self.machine("a")
        env = m.env()
        self.assertNotIn("XDG_CONFIG_HOME", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertTrue(env["GIT_CONFIG_GLOBAL"].startswith(str(self.root)))
        self.assertTrue(env["HOME"].startswith(str(self.root)))
        out = subprocess.run(["git", "config", "--global", "--list", "--show-origin"],
                             env=env, capture_output=True, text=True).stdout
        self.assertNotIn(str(REAL_HOME / ".config"), out)

    def test_machine_clone_and_remote_commit(self):
        m = self.machine("a")
        sha = self.remote_commit({"memory/-Users-__USER__-p/n.md": "hi\n"})
        self.assertEqual(len(sha), 40)
        self.assertTrue((m.repo / ".git").is_dir())

    def test_remote_paths_and_tip(self):
        self.assertEqual(self.remote_paths(), ["CLAUDE.md"])
        sha = self.remote_commit({"a/b.md": "1\n", "CLAUDE.md": None})
        self.assertEqual(self.remote_tip(), sha)
        self.assertEqual(self.remote_paths(), ["a/b.md"])

    def test_remote_commit_root_relative_and_exec_bit(self):
        self.remote_commit({"bin/README": "changed\n"}, root_relative=True)
        self.remote_commit({"run.sh": (b"#!/bin/sh\n", True)})
        m = self.machine("a")
        m.git("pull", "-q", "origin", "main")
        self.assertEqual((m.repo / "bin/README").read_text(), "changed\n")
        self.assertTrue((m.repo / "data/run.sh").stat().st_mode & 0o100)

    def test_machine_helpers(self):
        m = self.machine("a")
        m.write("skills/s/SKILL.md", "x\n", exec_bit=True)
        self.assertEqual(m.read("skills/s/SKILL.md"), "x\n")
        self.assertIsNone(m.read("nope.md"))
        self.assertTrue((m.home / ".claude/skills/s/SKILL.md").stat().st_mode & 0o100)
        self.assertIsNone(m.base())
        self.assertIsNone(m.ref("refs/nope"))
        self.assertEqual(m.ref("HEAD"), m.git("rev-parse", "HEAD"))
        self.assertEqual(m.log_lines(), [])
        (m.repo / "conflicts/2026-01-01T00-00-00Z/skills/s").mkdir(parents=True)
        (m.repo / "conflicts/2026-01-01T00-00-00Z/skills/s/SKILL.md").write_text("c")
        (m.repo / "trash/2026-01-01T00-00-00Z").mkdir(parents=True)
        (m.repo / "trash/2026-01-01T00-00-00Z/settings.json").write_text("t")
        self.assertEqual(m.conflicts(), ["skills/s/SKILL.md"])
        self.assertEqual(m.trash(), ["settings.json"])

    def test_gitcrypt_remote_is_ciphertext(self):
        key = self.make_gitcrypt_remote()
        self.assertTrue(key.is_file())
        self.assertTrue(str(key).startswith(str(self.root)))
        raw = subprocess.run(["git", "-C", str(self.root / "remote.git"), "cat-file", "-p", "main:data/CLAUDE.md"],
                             env=self.machine("a", clone=False).env(), capture_output=True).stdout
        self.assertTrue(raw.startswith(b"\x00GITCRYPT"))
