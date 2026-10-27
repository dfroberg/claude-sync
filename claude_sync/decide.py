"""The per-path decision table (spec 6.8)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from claude_sync.merge import merge_content, merge_exec
from claude_sync.paths import Content


class Act(Enum):
    NOTHING = "nothing"
    WRITE = "write"
    TRASH = "trash"
    KEEP_LOCAL = "keep_local"
    KEEP_BOTH = "keep_both"
    TAKE_REMOTE = "take_remote"


@dataclass(frozen=True)
class Outcome:
    act: Act
    content: Optional[Content] = None
    warn: Optional[str] = None
    merged: bool = False
    local_win: bool = False


def decide(rel: str, L: Optional[Content], B: Optional[Content], R: Optional[Content], fresh: bool) -> Outcome:
    if L == R:
        return Outcome(Act.NOTHING)
    if R is not None and R.data == b"" and L is not None and L.data != b"":
        return Outcome(Act.KEEP_LOCAL, warn="remote empty file not applied")
    if B is not None and L == B:
        return Outcome(Act.TRASH) if R is None else Outcome(Act.WRITE, content=R)
    if L is None and B is not None:
        return Outcome(Act.WRITE, content=R, warn="remote edit beats local delete")
    if L is None:
        return Outcome(Act.WRITE, content=R)
    if R is None:
        return Outcome(Act.KEEP_LOCAL, warn="local edit beats remote delete", local_win=True)
    if L.data == R.data:
        bit = merge_exec(L.exec, B.exec if B is not None else None, R.exec, fresh)
        return Outcome(Act.NOTHING) if bit == L.exec else Outcome(Act.WRITE, content=Content(L.data, bit))
    merged = merge_content(rel, L, B, R, fresh)
    if merged is not None:
        return Outcome(Act.WRITE, content=merged, merged=True)
    return Outcome(Act.TAKE_REMOTE if (B is None and fresh) else Act.KEEP_BOTH)
