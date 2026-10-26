"""Allowlist, path mapping, canonicalisation and the local scan.

This module imports nothing from the package.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

TOKEN = "__USER__"
FILES = ("CLAUDE.md", "settings.json", "mcp_settings.json", "keybindings.json")
DIRS = ("skills", "agents", "commands", "hooks")
TEXT_EXT = (".md", ".json", ".jsonl", ".txt", ".sh", ".bash", ".zsh", ".lua", ".toml", ".yaml", ".yml", ".cfg")


@dataclass(frozen=True)
class Content:
    data: bytes
    exec: bool


def is_text(rel: str) -> bool:
    return rel.lower().endswith(TEXT_EXT)


def is_tmp(name: str) -> bool:
    return ".sync-tmp." in name


def canon(data: bytes, me: str) -> bytes:
    u = os.fsencode(me)
    t = TOKEN.encode()
    return data.replace(b"/Users/" + u + b"/", b"/Users/" + t + b"/").replace(
        b"-Users-" + u + b"-", b"-Users-" + t + b"-")


def decanon(data: bytes, me: str) -> bytes:
    u = os.fsencode(me)
    t = TOKEN.encode()
    return data.replace(b"/Users/" + t + b"/", b"/Users/" + u + b"/").replace(
        b"-Users-" + t + b"-", b"-Users-" + u + b"-")


def safe_rel(rel: str) -> bool:
    if not rel or rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def under(root: Path, rel: str) -> Path:
    if not safe_rel(rel):
        raise ValueError(f"unsafe path {rel}")
    target = Path(root) / rel
    real_root = os.path.realpath(root)
    real_parent = os.path.realpath(target.parent)
    if real_parent != real_root and not real_parent.startswith(real_root.rstrip(os.sep) + os.sep):
        raise ValueError(f"unsafe path {rel}")
    return target


class UnderCache:
    """under() for many paths below one root: the root and each parent folder are resolved once."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._real_root = os.path.realpath(root)
        self._prefix = self._real_root.rstrip(os.sep) + os.sep
        self._parents: Dict[str, bool] = {}

    def __call__(self, rel: str) -> Path:
        if not safe_rel(rel):
            raise ValueError(f"unsafe path {rel}")
        parent = rel.rpartition("/")[0]
        ok = self._parents.get(parent)
        if ok is None:
            real_parent = os.path.realpath(self.root / parent) if parent else self._real_root
            ok = real_parent == self._real_root or real_parent.startswith(self._prefix)
            self._parents[parent] = ok
        if not ok:
            raise ValueError(f"unsafe path {rel}")
        return self.root / rel


def _canon_name(name: str, me: str) -> str:
    return os.fsdecode(canon(os.fsencode(name), me))


def _decanon_name(name: str, me: str) -> str:
    return os.fsdecode(decanon(os.fsencode(name), me))


def to_repo(local_rel: str, me: str) -> Optional[str]:
    if not safe_rel(local_rel):
        return None
    parts = local_rel.split("/")
    if local_rel in FILES:
        return local_rel
    if parts[0] in DIRS and len(parts) >= 2:
        return local_rel
    if parts[0] == "projects" and len(parts) >= 4 and parts[2] == "memory":
        return "/".join(["memory", _canon_name(parts[1], me)] + parts[3:])
    return None


def to_local(repo_rel: str, me: str) -> Optional[str]:
    if not safe_rel(repo_rel):
        return None
    parts = repo_rel.split("/")
    if repo_rel in FILES:
        return repo_rel
    if parts[0] in DIRS and len(parts) >= 2:
        return repo_rel
    if parts[0] == "memory" and len(parts) >= 3:
        return "/".join(["projects", _decanon_name(parts[1], me), "memory"] + parts[2:])
    return None


def canon_content(rel: str, c: Content, me: str) -> Content:
    return Content(canon(c.data, me) if is_text(rel) else c.data, c.exec)


def decanon_content(rel: str, c: Content, me: str) -> Content:
    return Content(decanon(c.data, me) if is_text(rel) else c.data, c.exec)


def _lstat(path: Path) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _raise(err: OSError) -> None:
    raise err


def _walk(top: Path, prefix: str, files: Dict[str, Path], skipped: Set[str]) -> None:
    """Collect regular files under `top` (a real directory) as `prefix/<rel>`."""
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False, onerror=_raise):
        here = Path(dirpath)
        rel_dir = os.path.relpath(dirpath, top)
        base = prefix if rel_dir == "." else prefix + "/" + rel_dir.replace(os.sep, "/")
        for d in sorted(dirnames):
            if is_tmp(d):
                dirnames.remove(d)
            elif (here / d).is_symlink():
                skipped.add(base + "/" + d + "/")
                dirnames.remove(d)
        for f in sorted(filenames):
            if is_tmp(f):
                continue
            st = _lstat(here / f)
            if st is None:
                continue
            if stat.S_ISREG(st.st_mode):
                files[base + "/" + f] = here / f
            else:
                skipped.add(base + "/" + f)


def scan_local(claude: Path, me: str) -> Tuple[Dict[str, Path], Set[str]]:
    files: Dict[str, Path] = {}
    skipped: Set[str] = set()
    claude = Path(claude)
    for f in FILES:
        st = _lstat(claude / f)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            skipped.add(f)
        elif stat.S_ISREG(st.st_mode):
            files[f] = claude / f
    for d in DIRS:
        st = _lstat(claude / d)
        if st is None:
            continue
        if stat.S_ISLNK(st.st_mode):
            skipped.add(d + "/")
        elif stat.S_ISDIR(st.st_mode):
            _walk(claude / d, d, files, skipped)
    projects = claude / "projects"
    st = _lstat(projects)
    if st is None:
        return files, skipped
    if stat.S_ISLNK(st.st_mode):
        skipped.add("memory/")
        return files, skipped
    if not stat.S_ISDIR(st.st_mode):
        return files, skipped
    for proj in sorted(os.listdir(projects)):
        prefix = "memory/" + _canon_name(proj, me)
        pst = _lstat(projects / proj)
        if pst is None:
            continue
        if stat.S_ISLNK(pst.st_mode):
            skipped.add(prefix + "/")
            continue
        if not stat.S_ISDIR(pst.st_mode):
            continue
        mem = projects / proj / "memory"
        mst = _lstat(mem)
        if mst is None:
            continue
        if stat.S_ISLNK(mst.st_mode):
            skipped.add(prefix + "/")
        elif stat.S_ISDIR(mst.st_mode):
            _walk(mem, prefix, files, skipped)
    return files, skipped


def via_symlink(claude: Path, local_rel: str) -> bool:
    cur = Path(claude)
    for comp in local_rel.split("/"):
        cur = cur / comp
        st = _lstat(cur)
        if st is None:
            return False
        if stat.S_ISLNK(st.st_mode):
            return True
    return False
