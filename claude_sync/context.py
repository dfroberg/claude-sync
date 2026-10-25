"""Run context: the report, the checkpoint hook and the per-run stamp (spec 6.8, 6.11)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

from claude_sync.config import Config
from claude_sync.gitio import Git
from claude_sync.log import Log

BASE_REF = "refs/claude-sync/base"
PENDING_REF = "refs/claude-sync/pending"
HOLD_WAIT_SECONDS = 60


@dataclass
class Report:
    remote_changes: int = 0
    written: int = 0
    merged: int = 0
    kept_both: int = 0
    trashed: int = 0
    local_wins: int = 0
    pushed: int = 0
    errors: int = 0
    conflicts_dir: Optional[Path] = None
    first_error: Optional[str] = None
    skipped_symlinks: int = 0
    noop: bool = False

    def fail(self, message: str) -> None:
        self.errors += 1
        if self.first_error is None:
            self.first_error = message

    def summary(self, command: str, seconds: float) -> str:
        line = (f"{command} done in {seconds:.1f}s: {self.remote_changes} remote changes, "
                f"{self.written} written, {self.merged} merged, {self.kept_both} kept-both, "
                f"{self.trashed} trashed")
        if command == "push":
            line += f", {self.pushed} pushed"
        return line

    def notice(self, home: Path) -> Optional[str]:
        if self.errors:
            return f"claude-sync: ERROR {self.first_error} (see ~/.claude-sync/last-sync.log)"
        if self.kept_both:
            return (f"claude-sync: {self.kept_both} conflicts kept both, remote copies in "
                    f"{_tilde(self.conflicts_dir, home)} (see last-sync.log)")
        if self.local_wins:
            return f"claude-sync: {self.local_wins} local edits kept over remote deletes (see last-sync.log)"
        return None


def _tilde(path: Optional[Path], home: Path) -> str:
    if path is None:
        return "~/.claude-sync/conflicts"
    text, prefix = str(path), str(home).rstrip(os.sep)
    if text == prefix:
        return "~"
    if text.startswith(prefix + os.sep):
        return "~" + text[len(prefix):]
    return text


@dataclass
class Ctx:
    cfg: Config
    log: Log
    git: Git
    report: Report
    stamp: str
    lock_fd: Optional[int] = None
    fired: Set[str] = field(default_factory=set)

    def checkpoint(self, name: str) -> None:
        """Test hook: hold at `name` until the test deletes $REPO/.hold-<name> (at most once per run)."""
        if os.environ.get("CLAUDE_SYNC_TEST_HOLD") != name or name in self.fired:
            return
        self.fired.add(name)
        marker = self.cfg.repo / f".hold-{name}"
        marker.write_bytes(b"")
        deadline = time.monotonic() + HOLD_WAIT_SECONDS
        while marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    @property
    def conflicts_root(self) -> Path:
        return self.cfg.repo / "conflicts" / self.stamp

    @property
    def trash_root(self) -> Path:
        return self.cfg.repo / "trash" / self.stamp
