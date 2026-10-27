from __future__ import annotations

from tests.harness import SyncTestCase

LOCAL = "projects/-Users-alice-p/memory/"      # under ~/.claude
REPO = "memory/-Users-__USER__-p/"             # under data/
HEADER = b"\x00GITCRYPT"


class GitCryptTest(SyncTestCase):
    def pair(self):
        """Two locked clones of a git-crypt remote; the tool unlocks each with the throwaway key."""
        key = self.make_gitcrypt_remote()
        a, b = self.machine("a"), self.machine("b")
        env_a, env_b = a.env(CLAUDE_SYNC_KEY=str(key)), b.env(CLAUDE_SYNC_KEY=str(key))
        a.run("pull", env=env_a)
        b.run("pull", env=env_b)
        return a, b, env_a, env_b

    def break_filter(self, m) -> dict:
        """Remove the git-crypt filter config; the key file the run is given does not exist."""
        m.git("config", "--remove-section", "filter.git-crypt")
        return m.env(CLAUDE_SYNC_KEY=str(self.root / "gone.key"))

    def test_round_trip_two_machines(self):                  # spec test 18
        a, b, env_a, env_b = self.pair()
        rel = LOCAL + "n.md"
        a.write(rel, "secret on a\n")
        a.run("push", env=env_a)
        b.run("pull", env=env_b)
        self.assertEqual(b.read(rel), "secret on a\n")
        blob = self._git(self.remote, "cat-file", "blob", f"main:data/{REPO}n.md", binary=True)
        self.assertTrue(blob.startswith(HEADER))
        self.assertNotIn(b"secret on a", blob)
        for blob in self.remote_data_blobs():
            self.assertTrue(blob.startswith(HEADER))

    def test_merge_reads_plaintext(self):                    # spec test 18: B and R are read as plaintext
        a, b, env_a, env_b = self.pair()
        rel = LOCAL + "m.md"
        base = "".join(f"line {i}\n" for i in range(1, 11))
        a.write(rel, base)
        a.run("push", env=env_a)
        b.run("pull", env=env_b)
        a.write(rel, base.replace("line 1\n", "line 1 edited on a\n"))
        b.write(rel, base.replace("line 10\n", "line 10 edited on b\n"))
        b.run("push", env=env_b)
        a.run("pull", env=env_a)
        merged = base.replace("line 1\n", "line 1 edited on a\n").replace("line 10\n", "line 10 edited on b\n")
        self.assertEqual(a.read(rel), merged)
        self.assertEqual(a.conflicts(), [])
        a.run("push", env=env_a)
        b.run("pull", env=env_b)
        self.assertEqual(b.read(rel), merged)

    def test_filter_missing_stops_before_write(self):        # filter config gone and no key counts as locked
        a, b, env_a, env_b = self.pair()
        rel = LOCAL + "f.md"
        a.write(rel, "before\n")
        a.run("push", env=env_a)
        b.run("pull", env=env_b)
        b.write(rel, "changed on b\n")
        b.run("push", env=env_b)                              # a remote change that a has not seen
        self.assertNotEqual(b.read(rel), a.read(rel))
        broken = self.break_filter(a)
        r = a.run("pull", env=broken, expect=1)
        self.assertIn("locked", r.stdout)
        self.assertEqual(a.read(rel), "before\n")             # stopped; the message need not say "ciphertext"

    def test_filter_attribute_off_ciphertext_guard(self):    # spec test 18, last sentence: the filter silently does not run
        a, b, env_a, env_b = self.pair()
        rel = LOCAL + "g.md"
        a.write(rel, "before\n")
        a.run("push", env=env_a)
        b.run("pull", env=env_b)
        b.write(rel, "changed on b\n")
        b.run("push", env=env_b)
        with open(a.repo / ".git" / "info" / "attributes", "a") as fh:
            fh.write("data/** !filter\n")
        r = a.run("pull", env=env_a, expect=1)
        self.assertIn("ciphertext", r.stdout)
        self.assertEqual(a.read(rel), "before\n")
        for p in (a.home / ".claude").rglob("*"):
            if p.is_file():
                self.assertFalse(p.read_bytes().startswith(HEADER), p)      # ciphertext is never written locally

    def test_filter_missing_never_pushes_plaintext(self):    # plaintext guard, first line of defence
        a, b, env_a, env_b = self.pair()
        rel = LOCAL + "p.md"
        a.write(rel, "before\n")
        a.run("push", env=env_a)
        broken = self.break_filter(a)
        tip = self.remote_tip()
        a.write(rel, "edit\n")
        r = a.run("push", env=broken, expect=1)
        self.assertIn("locked", r.stdout)
        self.assertEqual(self.remote_tip(), tip)
        for blob in self.remote_data_blobs():
            self.assertTrue(blob.startswith(HEADER))

    def test_plaintext_guard_unit(self):                     # a clean filter that does not encrypt
        a, b, env_a, env_b = self.pair()
        rel = LOCAL + "q.md"
        a.write(rel, "before\n")
        a.run("push", env=env_a)
        a.git("config", "filter.git-crypt.clean", "cat")      # still counts as unlocked: key and both filters are set
        tip, base = self.remote_tip(), a.base()
        a.write(rel, "plain edit\n")
        r = a.run("push", env=env_a, expect=1)
        self.assertIn("refusing to commit unencrypted", r.stdout)
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(a.base(), base)
        self.assertEqual(a.git("diff", "--cached", "--name-only"), "")        # the index was reset
        self.assertEqual(a.git("rev-parse", "HEAD"), base)
        self.assertEqual(a.read(rel), "plain edit\n")                          # the local edit is untouched
        for blob in self.remote_data_blobs():
            self.assertTrue(blob.startswith(HEADER))

    def test_locked_repo_without_key(self):                  # spec test 22
        self.make_gitcrypt_remote()                          # without it the remote is plaintext and the pull would succeed
        a = self.machine("a")
        r = a.run("pull", expect=1)                          # CLAUDE_SYNC_KEY points at a missing file
        self.assertIn("ERROR", r.stdout)
        self.assertFalse((a.home / ".claude").exists() and any((a.home / ".claude").rglob("*")))
