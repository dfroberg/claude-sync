"""Push (spec 6.10): scan ~/.claude against data/, build one linear commit on the remote tip."""
from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from claude_sync.checks import StopRun, _uses_gitcrypt, prepare, restore_invariant
from claude_sync.context import BASE_REF, PENDING_REF, Ctx
from claude_sync.fsops import CiphertextError, atomic_write, copy_aside, is_ciphertext, read_content
from claude_sync.paths import (
    Content, UnderCache, canon_content, decanon_content, scan_local, to_local, under, via_symlink,
)
from claude_sync.pull import fetch_origin, pull_steps

MAX_ATTEMPTS = 3
MASS_DELETE_MIN = 20
EMPTY_FILE_AGE = 600
CRYPT_HEADER = b"\x00GITCRYPT"


@dataclass
class Scan:
    changed: Dict[str, Content] = field(default_factory=dict)    # repo_rel -> new canonical content
    deleted: List[str] = field(default_factory=list)             # repo_rel
    local: Dict[str, Path] = field(default_factory=dict)         # repo_rel -> local regular file
    skipped: Set[str] = field(default_factory=set)               # symlinked or special paths left alone

    @property
    def empty(self) -> bool:
        return not self.changed and not self.deleted


def _shown(ctx: Ctx, path) -> str:
    """A path for messages: relative to ~/.claude when it is inside it."""
    try:
        return Path(path).relative_to(ctx.cfg.claude).as_posix()
    except ValueError:
        return str(path)


def _read(ctx: Ctx, path: Path) -> Optional[Content]:
    try:
        return read_content(path)
    except OSError as e:
        raise StopRun(f"cannot scan {_shown(ctx, path)}: {e.strerror or e}")


def _covered_by_skip(rel: str, skipped: Set[str]) -> bool:
    return any(rel == s or (s.endswith("/") and rel.startswith(s)) for s in skipped)


def scan(ctx: Ctx) -> Scan:
    cfg = ctx.cfg
    try:
        local, skipped = scan_local(cfg.claude, cfg.me)
    except OSError as e:
        raise StopRun(f"cannot scan {_shown(ctx, e.filename or cfg.claude)}: {e.strerror or e}")
    out = Scan(skipped=set(skipped))
    data_path = UnderCache(cfg.data)
    for rel in sorted(local):
        path = local[rel]
        raw = _read(ctx, path)
        if raw is None:                  # vanished since the listing: confirmed absent, so not a local file
            continue
        out.local[rel] = path
        new = canon_content(rel, raw, cfg.me)
        old = _read(ctx, data_path(rel))
        if old is not None and is_ciphertext(old.data):
            raise StopRun("data/ holds ciphertext; repo is not unlocked")
        if old is None or old.data != new.data or old.exec != new.exec:
            out.changed[rel] = new
    for rel in sorted(ctx.git.tree_blobs("HEAD")):
        if rel in out.local:
            continue
        local_rel = to_local(rel, cfg.me)
        if local_rel is None or via_symlink(cfg.claude, local_rel) or _covered_by_skip(rel, out.skipped):
            continue
        out.deleted.append(rel)
    return out


def _host() -> str:
    try:
        cp = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, timeout=5)
        name = cp.stdout.decode("utf-8", "replace").strip() if cp.returncode == 0 else ""
        if name:
            return name
    except Exception:  # noqa: BLE001 - any failure falls back to the socket name
        pass
    return socket.gethostname().split(".")[0]


def _empty_file_guard(ctx: Ctx, s: Scan) -> None:
    """Spec 6.10 step 5: a 0-byte local file over a non-empty data version is never pushed."""
    cfg, log = ctx.cfg, ctx.log
    now = time.time()
    for rel in sorted(s.changed):
        if s.changed[rel].data:
            continue
        old = _read(ctx, under(cfg.data, rel))
        if old is None or not old.data:
            continue
        local = s.local[rel]
        try:
            age = now - os.lstat(local).st_mtime
        except OSError as e:
            raise StopRun(f"cannot scan {_shown(ctx, local)}: {e.strerror or e}")
        if age >= EMPTY_FILE_AGE:
            local_rel = to_local(rel, cfg.me)
            assert local_rel is not None
            try:
                copy_aside(local, ctx.trash_root, local_rel)
                atomic_write(local, decanon_content(rel, old, cfg.me))
            except (OSError, ValueError, CiphertextError) as e:
                raise StopRun(f"cannot restore {local_rel}: {getattr(e, 'strerror', None) or e}")
            log.warn(f"empty local file restored from the synced version: {local_rel}")
        del s.changed[rel]


def _mass_delete_guard(ctx: Ctx, s: Scan) -> None:
    """Spec 6.10 step 6, the predicate of the pull side: more than 20 deletes that outnumber the files kept."""
    d = len(s.deleted)
    if d <= MASS_DELETE_MIN or ctx.cfg.allow_prune:
        return
    tracked = ctx.git.tree_blobs("HEAD")
    kept = len(tracked) - d + sum(1 for rel in s.changed if rel not in tracked)
    if d > kept:
        restore_invariant(ctx)
        raise StopRun(f"refusing to push: would delete {d} files and keep only {kept}; "
                      "CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync push if intended")


def _write_data(ctx: Ctx, s: Scan) -> None:
    """Deletions first, so a file replaced by a folder (or the reverse) never blocks the write."""
    data = ctx.cfg.data
    try:
        for rel in s.deleted:
            target = under(data, rel)
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
            parent = target.parent
            while parent != data:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = parent.parent
        for rel in sorted(s.changed):
            atomic_write(under(data, rel), s.changed[rel])
    except (OSError, ValueError, CiphertextError) as e:
        restore_invariant(ctx)
        raise StopRun(f"cannot write data/: {getattr(e, 'strerror', None) or e}")


def _staged_added_or_modified(ctx: Ctx) -> List[Tuple[str, str]]:
    out = ctx.git.run("diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev",
                      "--diff-filter=AMT", "--", "data/").stdout
    toks = out.split(b"\0")
    found: List[Tuple[str, str]] = []
    i = 0
    while i + 1 < len(toks):
        if toks[i].startswith(b":"):
            fields = toks[i][1:].decode().split(" ")
            found.append((toks[i + 1].decode("utf-8", "surrogateescape"), fields[3]))
            i += 2
        else:
            i += 1
    return found


def _plaintext_guard(ctx: Ctx) -> None:
    """In a git-crypt repo every staged blob must be ciphertext, or the public remote would get plaintext."""
    if not _uses_gitcrypt(ctx.cfg.repo):
        return
    staged = _staged_added_or_modified(ctx)
    if not staged:
        return
    raw = ctx.git.run("cat-file", "--batch", input="".join(f"{oid}\n" for _, oid in staged).encode()).stdout
    pos = 0
    for path, oid in staged:
        eol = raw.index(b"\n", pos)
        header = raw[pos:eol].split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            _reject_staged(ctx, f"cannot read staged {path}")
        size = int(header[2])
        blob = raw[eol + 1:eol + 1 + size]
        pos = eol + 1 + size + 1
        if not blob.startswith(CRYPT_HEADER):
            _reject_staged(ctx, f"refusing to commit unencrypted {path}")


def _reject_staged(ctx: Ctx, message: str) -> None:
    ctx.git.run("reset", "-q")
    restore_invariant(ctx)
    raise StopRun(message)


def _pull_inside_push(ctx: Ctx, origin: str) -> None:
    try:
        pull_steps(ctx, origin)
    except StopRun:
        restore_invariant(ctx)
        raise


def run_push(ctx: Ctx) -> int:
    git, cfg, log, report = ctx.git, ctx.cfg, ctx.log, ctx.report
    prepare(ctx)
    if git.rev(BASE_REF) is None:
        origin = fetch_origin(ctx)
        if origin is None:
            return 0
        _pull_inside_push(ctx, origin)
        if git.rev(BASE_REF) is None:
            raise StopRun("first pull did not set the base")
    s = scan(ctx)
    if s.skipped:
        (log.debug if s.empty else log.info)(f"skipped {len(s.skipped)} paths under symlinks")
    if s.empty:
        log.debug("no changes")
        report.noop = True
        return 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        base0 = git.rev(BASE_REF)
        origin = fetch_origin(ctx)
        if origin is None:
            return 0
        base = git.rev(BASE_REF)
        if origin != base:
            _pull_inside_push(ctx, origin)
        if git.rev(BASE_REF) != base0:
            s = scan(ctx)
            if s.empty:
                return 0
        _empty_file_guard(ctx, s)
        _mass_delete_guard(ctx, s)
        _write_data(ctx, s)
        git.add_data()
        if not git.staged_data():
            log.debug("no changes")
            report.noop = True
            restore_invariant(ctx)
            return 0
        _plaintext_guard(ctx)
        ctx.checkpoint("push:before-commit")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha = git.commit(f"sync: {cfg.me}@{_host()} {stamp}")
        git.update_ref(PENDING_REF, sha)
        ctx.checkpoint("push:before-push")
        result = git.push(cfg.branch)
        ctx.checkpoint("push:after-push")
        if result == "ok":
            git.update_ref(BASE_REF, sha)
            git.delete_ref(PENDING_REF)
            report.pushed = len(s.changed) + len(s.deleted)
            return 0
        if result == "offline":
            restore_invariant(ctx)      # pending stays: the next run that fetches resolves it
            log.warn("push did not complete; the next run checks whether it landed")
            return 0
        if result == "rejected":
            restore_invariant(ctx)
            git.delete_ref(PENDING_REF)
            if attempt < MAX_ATTEMPTS:
                s = scan(ctx)
            continue
        restore_invariant(ctx)
        raise StopRun("git push to origin failed")
    raise StopRun(f"push rejected {MAX_ATTEMPTS} times")
