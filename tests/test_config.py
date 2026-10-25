from __future__ import annotations

import unittest
from pathlib import Path

from claude_sync import config


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = config.load({"HOME": "/h/x"})
        self.assertEqual(c.home, Path("/h/x"))
        self.assertEqual(c.claude, Path("/h/x/.claude"))
        self.assertEqual(c.repo, Path("/h/x/.claude-sync"))
        self.assertEqual(c.data, Path("/h/x/.claude-sync/data"))
        self.assertEqual(c.branch, "main")
        self.assertEqual(c.key, Path("/h/x/.config/git-crypt/claude-sync.key"))
        self.assertFalse(c.allow_prune)
        self.assertFalse(c.debug)
        self.assertIsNone(c.lock_wait_override)
        self.assertTrue(c.me)

    def test_overrides(self):
        c = config.load({
            "HOME": "/h/x",
            "CLAUDE_SYNC_REPO": "/r/repo",
            "CLAUDE_SYNC_USER": "bob",
            "CLAUDE_SYNC_BRANCH": "dev",
            "CLAUDE_SYNC_KEY": "/k/key",
            "CLAUDE_SYNC_ALLOW_PRUNE": "1",
            "CLAUDE_SYNC_DEBUG": "1",
            "CLAUDE_SYNC_LOCK_WAIT": "2.5",
        })
        self.assertEqual(c.repo, Path("/r/repo"))
        self.assertEqual(c.data, Path("/r/repo/data"))
        self.assertEqual(c.me, "bob")
        self.assertEqual(c.branch, "dev")
        self.assertEqual(c.key, Path("/k/key"))
        self.assertTrue(c.allow_prune)
        self.assertTrue(c.debug)
        self.assertEqual(c.lock_wait_override, 2.5)

    def test_config_dir_ignored(self):
        c = config.load({"HOME": "/h/x", "CLAUDE_CONFIG_DIR": "/elsewhere"})
        self.assertEqual(c.claude, Path("/h/x/.claude"))

    def test_flags_need_exactly_one(self):
        c = config.load({"HOME": "/h/x", "CLAUDE_SYNC_ALLOW_PRUNE": "yes", "CLAUDE_SYNC_DEBUG": "0"})
        self.assertFalse(c.allow_prune)
        self.assertFalse(c.debug)


if __name__ == "__main__":
    unittest.main()
