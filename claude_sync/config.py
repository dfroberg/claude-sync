from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class Config:
    home: Path
    claude: Path
    repo: Path
    data: Path
    me: str
    branch: str
    key: Path
    allow_prune: bool
    debug: bool
    lock_wait_override: Optional[float]


def _lock_wait(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load(env: Mapping[str, str]) -> Config:
    home = Path(env.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)
    repo = Path(env["CLAUDE_SYNC_REPO"]) if env.get("CLAUDE_SYNC_REPO") else home / ".claude-sync"
    key = Path(env["CLAUDE_SYNC_KEY"]) if env.get("CLAUDE_SYNC_KEY") else home / ".config" / "git-crypt" / "claude-sync.key"
    return Config(
        home=home,
        claude=home / ".claude",
        repo=repo,
        data=repo / "data",
        me=env.get("CLAUDE_SYNC_USER") or pwd.getpwuid(os.getuid()).pw_name,
        branch=env.get("CLAUDE_SYNC_BRANCH") or "main",
        key=key,
        allow_prune=env.get("CLAUDE_SYNC_ALLOW_PRUNE") == "1",
        debug=env.get("CLAUDE_SYNC_DEBUG") == "1",
        lock_wait_override=_lock_wait(env.get("CLAUDE_SYNC_LOCK_WAIT")),
    )
