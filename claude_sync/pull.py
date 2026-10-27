"""Pull (spec 6.7): three-way, per path, never replacing a local edit without a merge or a kept copy."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from claude_sync.checks import StopRun, prepare
from claude_sync.context import BASE_REF, PENDING_REF, Ctx
from claude_sync.decide import Act, Outcome, decide
from claude_sync.fsops import (
    atomic_write, copy_aside, is_ciphertext, move_aside, put_aside, read_content, stat_key,
)
from claude_sync.gitio import Change
from claude_sync.merge import MergeError
from claude_sync.paths import (
    Content, canon_content, decanon_content, safe_rel, to_local, under, via_symlink,
)

REGULAR_MODES = ("100644", "100755")
REPLACING = (Act.WRITE, Act.TRASH, Act.TAKE_REMOTE)
MAX_REDECIDE = 3
MASS_DELETE_MIN = 20
FRESH_MARKER = ".fresh-machine"


def fetch_origin(ctx: Ctx) -> Optional[str]:
    """Fetch, resolve a pending push (spec 6.4) and return origin's sha; None when offline."""
    git, cfg = ctx.git, ctx.cfg
    result = git.fetch(cfg.branch)
    if result == "offline":
        return None
    if result == "error":
        raise StopRun("git fetch from origin failed")
    origin = git.rev(f"refs/remotes/origin/{cfg.branch}")
    if origin is None:
        raise StopRun(f"origin/{cfg.branch} not found after fetch")
    pending = git.rev(PENDING_REF)
    if pending is not None and git.is_ancestor(pending, origin):
        ctx.log.info(f"pending push {pending[:7]} landed; moving the base to it")
        git.reset_hard(pending)
        git.update_ref(BASE_REF, pending)
    git.delete_ref(PENDING_REF)     # last: a kill before this line repeats the steps above
    return origin


def run_pull(ctx: Ctx) -> int:
    prepare(ctx)
    origin = fetch_origin(ctx)
    if origin is None:
        return 0
    base = ctx.git.rev(BASE_REF)        # a resolved pending push may have moved it
    if base is not None and origin == base:
        return 0
    pull_steps(ctx, origin)
    return 0


def _slurp(path: Path, what: str) -> Optional[Content]:
    try:
        return read_content(path)
    except OSError as e:
        raise StopRun(f"cannot read {what}: {e.strerror}")


def _no_ciphertext(c: Optional[Content], rel: str) -> None:
    if c is not None and is_ciphertext(c.data):
        raise StopRun(f"ciphertext read from git for {rel}")


def _check_changes(changes: List[Change], me: str) -> Tuple[List[Tuple[Change, str]], int]:
    """Validate the diff (spec 6.3) and map it to local paths; returns (placed, dropped count)."""
    placed: List[Tuple[Change, str]] = []
    dropped = 0
    for c in changes:
        if c.status == "T" or (c.status != "D" and c.new_mode not in REGULAR_MODES):
            raise StopRun(f"unsupported entry in data/: {c.path} mode {c.new_mode}")
        if not safe_rel(c.path):
            raise StopRun(f"unsafe path in data/: {c.path}")
        local_rel = to_local(c.path, me)
        if local_rel is None:
            dropped += 1
        else:
            placed.append((c, local_rel))
    return placed, dropped


def _load_remote(ctx: Ctx, origin: str, changes: List[Change]) -> Dict[str, Content]:
    """R for every non-delete change: from data/ when HEAD holds the same blob, else one temp checkout."""
    git, cfg = ctx.git, ctx.cfg
    wanted = [c for c in changes if c.status != "D"]
    if not wanted:
        return {}
    head = git.tree_blobs("HEAD")
    out: Dict[str, Content] = {}
    fetch: List[Change] = []
    for c in wanted:
        if head.get(c.path) == (c.new_mode, c.new_blob):
            raw = _slurp(cfg.data / c.path, f"data/{c.path}")
            if raw is None:
                raise StopRun(f"data/{c.path} is missing from the work tree")
            _no_ciphertext(raw, c.path)
            out[c.path] = Content(raw.data, c.new_mode == "100755")
        else:
            fetch.append(c)
    if fetch:
        tmp = Path(tempfile.mkdtemp(prefix="claude-sync-"))
        try:
            git.checkout_to(origin, [c.path for c in fetch], tmp)
            for c in fetch:
                raw = _slurp(tmp / "data" / c.path, f"remote {c.path}")
                if raw is None:
                    raise StopRun(f"remote file {c.path} was not checked out")
                _no_ciphertext(raw, c.path)
                out[c.path] = Content(raw.data, c.new_mode == "100755")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def _load_base(ctx: Ctx, changes: List[Change]) -> Dict[str, Content]:
    """B from the data/ work tree, which the invariant makes equal to the base tree (spec 6.7 step 7)."""
    out: Dict[str, Content] = {}
    for c in changes:
        b = _slurp(ctx.cfg.data / c.path, f"data/{c.path}")
        if b is not None:
            _no_ciphertext(b, c.path)
            out[c.path] = b
    return out


def _read_local(ctx: Ctx, local, local_rel: str) -> Optional[Content]:
    raw = _slurp(local, local_rel)
    return None if raw is None else canon_content(local_rel, raw, ctx.cfg.me)


def _decide_path(ctx: Ctx, local: Path, local_rel: str, B: Optional[Content], R: Optional[Content],
                 fresh: bool) -> Outcome:
    """Decide one path; a local change between the read and the replace is read again (spec 6.8)."""
    redecides = 0
    while True:
        key = stat_key(local)               # before the read: a later change then always shows
        L = _read_local(ctx, local, local_rel)
        try:
            outcome = decide(local_rel, L, B, R, fresh)
        except MergeError as e:
            raise StopRun(f"merge failed for {local_rel}: {e}")
        if outcome.act not in REPLACING:
            return outcome
        ctx.checkpoint("pull:before-replace")
        if stat_key(local) == key:
            return outcome
        if redecides >= MAX_REDECIDE:
            ctx.log.warn(f"{local_rel} kept changing during the pull")
            if R is None:       # a remote delete: nothing to park, keep the local file
                return Outcome(Act.KEEP_LOCAL, warn="local edit beats remote delete", local_win=True)
            return Outcome(Act.KEEP_BOTH)
        redecides += 1


def pull_steps(ctx: Ctx, origin: str) -> None:
    """Spec 6.7 steps 4 to 9, under the lock the caller holds. Any StopRun leaves the base where it was."""
    git, cfg, log, report = ctx.git, ctx.cfg, ctx.log, ctx.report
    me = cfg.me
    base = git.rev(BASE_REF)
    fresh = False
    if base is None:
        merge_base = git.empty_tree()
        fresh = (cfg.repo / FRESH_MARKER).exists()
    else:
        merge_base = base
        if not git.is_ancestor(base, origin):
            log.info("remote history was rewritten")
    changes = git.diff_raw(merge_base, origin)
    report.remote_changes = len(changes)
    _mass_delete_guard(ctx, origin, changes)
    placed, dropped = _check_changes(changes, me)

    remote = _load_remote(ctx, origin, [c for c, _ in placed])
    old = _load_base(ctx, [c for c, _ in placed]) if base is not None else {}

    for c, local_rel in sorted(placed, key=lambda t: (t[0].status != "D", t[0].path)):
        if via_symlink(cfg.claude, local_rel):
            report.skipped_symlinks += 1
            continue
        try:
            local = under(cfg.claude, local_rel)
        except ValueError:
            raise StopRun(f"unsafe path in data/: {c.path}")
        R = remote.get(c.path)
        outcome = _decide_path(ctx, local, local_rel, old.get(c.path), R, fresh)
        _apply(ctx, outcome, local, local_rel, R)

    if report.skipped_symlinks:
        log.info(f"skipped {report.skipped_symlinks} paths under symlinks")
    if dropped:
        log.warn(f"skipped {dropped} paths outside the allowlist")

    ctx.checkpoint("pull:before-reset")
    git.reset_hard(origin)
    ctx.checkpoint("pull:before-base")
    git.update_ref(BASE_REF, origin)
    (cfg.repo / FRESH_MARKER).unlink(missing_ok=True)


def _mass_delete_guard(ctx: Ctx, origin: str, changes: List[Change]) -> None:
    """Spec 6.7 step 6: more than 20 deletes that also outnumber the files left in origin's data/."""
    deletes = sum(1 for c in changes if c.status == "D")
    if deletes <= MASS_DELETE_MIN or ctx.cfg.allow_prune:
        return
    kept = len(ctx.git.tree_blobs(origin))
    if deletes > kept:
        raise StopRun(f"remote deletes {deletes} files and keeps {kept}; "
                      "run CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync pull if intended")


def _apply(ctx: Ctx, outcome: Outcome, local: Path, local_rel: str, R: Optional[Content]) -> None:
    try:
        _apply_outcome(ctx, outcome, local, local_rel, R)
    except (OSError, ValueError) as e:
        # Any filesystem error that is not a type clash stops the run before the base moves (spec 6.7 step 8).
        raise StopRun(f"cannot write {local_rel}: {getattr(e, 'strerror', None) or e}")


def _keep_both(ctx: Ctx, local_rel: str, R: Optional[Content], note: str = "") -> None:
    assert R is not None
    put_aside(ctx.conflicts_root, local_rel, decanon_content(local_rel, R, ctx.cfg.me))
    ctx.log.warn(f"kept both: {local_rel}{note}")
    ctx.report.kept_both += 1
    ctx.report.conflicts_dir = ctx.conflicts_root


def _apply_outcome(ctx: Ctx, outcome: Outcome, local: Path, local_rel: str, R: Optional[Content]) -> None:
    me, log, report = ctx.cfg.me, ctx.log, ctx.report
    act = outcome.act
    if act is Act.NOTHING:
        return
    if outcome.warn:
        log.warn(f"{outcome.warn}: {local_rel}")
    if act is Act.WRITE:
        assert outcome.content is not None
        try:
            atomic_write(local, decanon_content(local_rel, outcome.content, me))
        except (IsADirectoryError, NotADirectoryError):
            log.warn(f"type clash at {local_rel}")
            _keep_both(ctx, local_rel, R)
            return
        report.written += 1
        if outcome.merged:
            report.merged += 1
    elif act is Act.TRASH:
        move_aside(local, ctx.trash_root, local_rel)
        report.trashed += 1
    elif act is Act.KEEP_LOCAL:
        if outcome.local_win:
            report.local_wins += 1
    elif act is Act.KEEP_BOTH:
        _keep_both(ctx, local_rel, R)
    elif act is Act.TAKE_REMOTE:
        assert R is not None
        # The local file is copied (it stays in place) before the remote file replaces it, so it is
        # never replaced without a saved copy (spec 6.9).
        copy_aside(local, ctx.conflicts_root, local_rel)
        atomic_write(local, decanon_content(local_rel, R, me))
        log.warn(f"remote version taken: {local_rel} (new machine, local copy saved)")
        report.took_remote += 1
        report.conflicts_dir = ctx.conflicts_root
    else:
        raise StopRun(f"unexpected action {act.value} for {local_rel}")
