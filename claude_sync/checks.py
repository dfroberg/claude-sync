"""Start checks for every locked run (spec 6.6) and the invariant restore (spec 6.4)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from claude_sync.context import BASE_REF, Ctx

UNLOCK_TIMEOUT = 60
MAX_NAMED = 5


class StopRun(Exception):
    pass


class SkipRun(Exception):
    pass


def prepare(ctx: Ctx) -> None:
    git, log, cfg = ctx.git, ctx.log, ctx.cfg
    for path in git.clear_locks(60):
        log.warn(f"removed stale git lock {path}")
    if (cfg.repo / ".git" / "index.lock").exists():
        raise SkipRun("git busy: .git/index.lock is held by another git process; skipping")
    _clear_dead_rebase(ctx)
    _attach_head(ctx)
    dirty = git.dirty_outside_data()
    if dirty:
        named = ", ".join(dirty[:MAX_NAMED]) + (f" and {len(dirty) - MAX_NAMED} more" if len(dirty) > MAX_NAMED else "")
        raise StopRun(f"tracked files outside data/ are modified: {named}")
    ensure_unlocked(ctx)
    restore_invariant(ctx)


def _clear_dead_rebase(ctx: Ctx) -> None:
    gitdir = ctx.cfg.repo / ".git"
    for name in ("rebase-merge", "rebase-apply"):
        d = gitdir / name
        if not d.is_dir():
            continue
        try:
            sha = (d / "autostash").read_text().strip()
        except OSError:
            sha = ""
        if sha:
            cp = ctx.git.run("stash", "store", "-m", "autostash of a killed sync run", sha, check=False)
            if cp.returncode == 0:
                ctx.log.warn("kept the dead rebase's autostash as a stash entry")
            else:
                ctx.log.warn("could not keep the dead rebase's autostash: "
                             + cp.stderr.decode("utf-8", "replace").strip()[-200:])
        ctx.log.warn(f"removing dead {name} dir left by a killed run")
        shutil.rmtree(d, ignore_errors=True)


def _attach_head(ctx: Ctx) -> None:
    branch = ctx.cfg.branch
    if ctx.git.head_branch() == branch:
        return
    ctx.log.warn(f"HEAD is not on {branch}; reattaching it without touching the work tree")
    if ctx.git.rev("HEAD") is not None:
        ctx.git.run("update-ref", f"refs/heads/{branch}", "HEAD")
    ctx.git.run("symbolic-ref", "HEAD", f"refs/heads/{branch}")


def _uses_gitcrypt(repo: Path) -> bool:
    try:
        return "filter=git-crypt" in (repo / ".gitattributes").read_text(errors="replace")
    except OSError:
        return False


def _is_locked(ctx: Ctx) -> bool:
    if not (ctx.cfg.repo / ".git" / "git-crypt" / "keys" / "default").exists():
        return True
    for key in ("filter.git-crypt.smudge", "filter.git-crypt.clean"):
        cp = ctx.git.run("config", "--get", key, check=False)
        if cp.returncode != 0 or not cp.stdout.strip():
            return True
    return False


def is_locked(ctx: Ctx) -> bool:
    return _is_locked(ctx)


def ensure_unlocked(ctx: Ctx) -> None:
    cfg = ctx.cfg
    if not _uses_gitcrypt(cfg.repo) or not _is_locked(ctx):
        return
    if cfg.key.exists():
        try:
            cp = subprocess.run(["git-crypt", "unlock", str(cfg.key)], cwd=str(cfg.repo),
                                env=ctx.git._env(None), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=UNLOCK_TIMEOUT)
            if cp.returncode != 0:
                lines = cp.stderr.decode("utf-8", "replace").strip().splitlines()
                ctx.log.warn(f"git-crypt unlock failed (rc={cp.returncode}): {lines[0] if lines else ''}")
        except (OSError, subprocess.TimeoutExpired) as e:
            ctx.log.warn(f"git-crypt unlock failed: {type(e).__name__}")
    if _is_locked(ctx):
        raise StopRun(f"repo is git-crypt locked and no usable key at {cfg.key}")


def restore_invariant(ctx: Ctx) -> None:
    git = ctx.git
    head = git.rev("HEAD")
    target = git.rev(BASE_REF) or head
    if target is None:
        return
    if head != target or git.data_dirty():
        git.reset_hard(target)
        ctx.log.info(f"restored data/ to {target[:7]}")
    git.clean_data()
