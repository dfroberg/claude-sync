"""Three-way text merge through `git merge-file`, plus the JSON and link checks (spec 6.9)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from claude_sync.gitio import STRIPPED_ENV
from claude_sync.paths import Content, is_text

_LINK = re.compile(rb"\]\(([^)]+\.md)\)")


class MergeError(Exception):
    pass


def links(data: bytes) -> Set[str]:
    return {m.group(1).decode("utf-8", "replace") for m in _LINK.finditer(data)}


def _env() -> Dict[str, str]:
    # The same cleaning as Git.run: no inherited repository variables, no prompts, C locale.
    env = dict(os.environ)
    for k in STRIPPED_ENV:
        env.pop(k, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def merge_text(local: bytes, base: Optional[bytes], remote: bytes, union: bool) -> Optional[bytes]:
    tmp = tempfile.mkdtemp(prefix="claude-sync-merge-")
    try:
        paths = []
        for name, data in (("L", local), ("B", base if base is not None else b""), ("R", remote)):
            p = Path(tmp) / name
            p.write_bytes(data)
            paths.append(str(p))
        argv = ["git", "merge-file", "-p"]
        if union:
            argv.append("--union")
        argv.extend(paths)
        cp = subprocess.run(argv, env=_env(), cwd=tmp, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cp.returncode == 0:
            return cp.stdout
        if 1 <= cp.returncode <= 127:
            return None
        raise MergeError(f"git merge-file failed (rc={cp.returncode}): "
                         + cp.stderr.decode("utf-8", "replace").strip()[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _no_dupes(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k}")
        out[k] = v
    return out


def strict_json(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8"), object_pairs_hook=_no_dupes)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return True


def merge_exec(l: bool, b: Optional[bool], r: bool, fresh: bool) -> bool:
    if b is None:
        return r if fresh else l
    return r if l == b else l


def merge_content(rel: str, L: Content, B: Optional[Content], R: Content, fresh: bool) -> Optional[Content]:
    if not is_text(rel):
        return None
    union = rel.rsplit("/", 1)[-1] == "MEMORY.md"
    out = merge_text(L.data, B.data if B is not None else None, R.data, union)
    if out is None:
        return None
    if union:
        base_links = links(B.data) if B is not None else set()
        ll, rl = links(L.data), links(R.data)
        required = (ll & rl) | (ll - base_links) | (rl - base_links)
        if not required <= links(out):
            return None
    if rel.lower().endswith(".json") and not strict_json(out):
        return None
    return Content(out, merge_exec(L.exec, B.exec if B is not None else None, R.exec, fresh))
