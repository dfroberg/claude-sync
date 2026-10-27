"""Atomic writes and the conflicts/ and trash/ helpers."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import List, Optional, Tuple

from claude_sync.paths import Content, under


class CiphertextError(Exception):
    pass


def is_ciphertext(data: bytes) -> bool:
    return data.startswith(b"\x00GITCRYPT")


def read_content(path: Path) -> Optional[Content]:
    # O_NOFOLLOW: a path that became a symlink after the caller's via_symlink check raises
    # OSError (ELOOP) instead of reading the target (spec 6.3).
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    try:
        mode = os.fstat(fd).st_mode
    except BaseException:
        os.close(fd)
        raise
    if stat.S_ISDIR(mode):
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read()
    return Content(data, bool(mode & stat.S_IXUSR))


def stat_key(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (st.st_size, st.st_mtime_ns)


def atomic_write(path: Path, content: Content) -> None:
    path = Path(path)
    if is_ciphertext(content.data):
        raise CiphertextError(f"refusing to write encrypted content to {path}")
    if path.is_symlink():
        raise ValueError("refusing to write through symlink")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (NotADirectoryError, FileExistsError) as e:
        raise NotADirectoryError(str(e)) from e
    if path.is_dir():
        raise IsADirectoryError(f"a directory is in the way at {path}")
    tmp = path.with_name(f"{path.name}.sync-tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(content.data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o755 if content.exec else 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _free_name(root: Path, rel: str) -> Path:
    target = under(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    cand, n = target, 0
    while os.path.lexists(cand):
        n += 1
        cand = target.with_name(f"{target.name}.{n}")
    return cand


def put_aside(root: Path, rel: str, content: Content) -> Path:
    dest = _free_name(root, rel)
    atomic_write(dest, content)
    return dest


def copy_aside(src: Path, root: Path, rel: str) -> Path:
    dest = _free_name(root, rel)
    shutil.copy2(src, dest)
    return dest


def move_aside(src: Path, root: Path, rel: str) -> Path:
    dest = _free_name(root, rel)
    try:
        os.replace(src, dest)
    except OSError:
        shutil.move(str(src), str(dest))
    return dest


def prune_older_than(root: Path, days: int, now: float) -> List[Path]:
    removed: List[Path] = []
    try:
        entries = sorted(Path(root).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return removed
    cutoff = now - days * 86400
    for e in entries:
        try:
            st = os.lstat(e)
            if stat.S_ISDIR(st.st_mode) and st.st_mtime < cutoff:
                shutil.rmtree(e)
                removed.append(e)
        except OSError:
            continue
    return removed
