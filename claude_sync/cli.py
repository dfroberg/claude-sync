"""Command line dispatch (spec 6.12) and the run wrapper (spec 6.11)."""
from __future__ import annotations

import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List, Mapping, Optional

from claude_sync import config, pull, push
from claude_sync.checks import SkipRun, StopRun, _uses_gitcrypt, is_locked, prepare
from claude_sync.context import BASE_REF, PENDING_REF, Ctx, Report
from claude_sync.fsops import CiphertextError, is_ciphertext, prune_older_than
from claude_sync.gitio import Git, GitError
from claude_sync.lock import acquire
from claude_sync.log import Log
from claude_sync.merge import MergeError

USAGE = "usage: claude-sync {pull|push|status|dry-run}"
ALIASES = {"dry": "dry-run"}
COMMANDS = ("pull", "push", "status", "dry-run")
LOCK_WAIT = {"pull": 20.0, "push": 60.0, "dry-run": 20.0}
EXPECTED_ERRORS = (StopRun, GitError, MergeError, CiphertextError)
RETENTION_DAYS = 30
TAIL_BYTES = 256 * 1024
SUMMARY_LINE = re.compile(r" (pull|push) INFO (pull|push) done in ")
UNKNOWN = "unknown (a run is in progress or was interrupted)"


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def _last_summary(ctx: Ctx) -> str:
    try:
        with open(ctx.log.path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return "none"
    for line in reversed(text.splitlines()):
        if SUMMARY_LINE.search(line):
            return line
    return "none"


def _is_locked(ctx: Ctx) -> bool:
    if _uses_gitcrypt(ctx.cfg.repo) and is_locked(ctx):
        return True
    try:
        with open(ctx.cfg.data / "CLAUDE.md", "rb") as fh:
            return is_ciphertext(fh.read(16))
    except OSError:
        return False


def _local_changes(ctx: Ctx, base: Optional[str], head: Optional[str]) -> str:
    if _is_locked(ctx):
        return "locked"
    try:
        if (base is not None and head != base) or ctx.git.data_dirty():
            return UNKNOWN
        s = push.scan(ctx)
    except (StopRun, GitError, OSError):
        return "unknown"
    return f"{len(s.changed)} changed, {len(s.deleted)} deleted"


def _status(ctx: Ctx) -> int:
    git, cfg = ctx.git, ctx.cfg
    base, head = git.rev(BASE_REF), git.rev("HEAD")
    try:
        folders = sum(1 for p in (cfg.repo / "conflicts").iterdir() if p.is_dir())
    except OSError:
        folders = 0
    rows = [
        ("base", base or "none"),
        ("head", head or "none"),
        ("origin", git.rev(f"refs/remotes/origin/{cfg.branch}") or "none"),
        ("pending", "yes" if git.rev(PENDING_REF) else "no"),
        ("fresh-machine marker", "yes" if (cfg.repo / ".fresh-machine").exists() else "no"),
        ("local changes", _local_changes(ctx, base, head)),
        ("conflict folders", str(folders)),
        ("last", _last_summary(ctx)),
    ]
    for key, value in rows:
        print(f"{key}: {value}")
    return 0


def _dry_run(ctx: Ctx) -> int:
    prepare(ctx)
    s = push.scan(ctx)
    rows = [(rel, "M" if (ctx.cfg.data / rel).is_file() else "A") for rel in s.changed]
    rows += [(rel, "D") for rel in s.deleted]
    for rel, letter in sorted(rows):
        print(f"{letter} {rel}")
    return 0


HANDLERS = {"pull": pull.run_pull, "push": push.run_push, "status": _status, "dry-run": _dry_run}


def main(argv: List[str], env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    if len(argv) != 1 or ALIASES.get(argv[0], argv[0]) not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    command = ALIASES.get(argv[0], argv[0])
    cfg = config.load(env)
    log = Log(cfg.repo / "last-sync.log", command, cfg.debug)
    if env.get("CLAUDE_SYNC_RECONCILING") == "1":
        log.info("nested invocation; skipping")
        return 0
    git = Git(cfg.repo, log, base_env=None if env is os.environ else env,
              optional_locks=(command != "status"))
    report = Report()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    ctx = Ctx(cfg=cfg, log=log, git=git, report=report, stamp=stamp)
    locked = command in LOCK_WAIT
    started = time.monotonic()
    skipped = False
    rc = 0
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    try:
        if locked:
            wait = cfg.lock_wait_override if cfg.lock_wait_override is not None else LOCK_WAIT[command]
            fd = acquire(cfg.repo, wait, log)
            if fd is None:
                if command == "dry-run":
                    log.notice("claude-sync: another sync run holds the lock; dry-run checked nothing")
                skipped = True
                return 0
            ctx.lock_fd = fd
        rc = HANDLERS[command](ctx)
    except SystemExit:
        log.warn("terminated by signal")
        raise
    except SkipRun as e:
        log.info(str(e))
        if command == "dry-run":
            log.notice(f"claude-sync: {e}")
        report.noop = True
        rc = 0
    except EXPECTED_ERRORS as e:
        log.error(str(e))
        report.fail(str(e))
        rc = 1
    except Exception as e:  # noqa: BLE001 - last line of defence: log the traceback, exit 1
        log.exception(f"{type(e).__name__}: {e}")
        report.fail(f"{type(e).__name__}: {e}")
        rc = 1
    finally:
        if not skipped:
            _finish(ctx, command, time.monotonic() - started)
    return rc


def _finish(ctx: Ctx, command: str, seconds: float) -> None:
    report, log = ctx.report, ctx.log
    if command in ("pull", "push") and ctx.lock_fd is not None:
        line = report.summary(command, seconds)
        (log.debug if report.noop else log.info)(line)
    if ctx.lock_fd is not None:
        _retention(ctx)
    msg = report.notice(ctx.cfg.home)
    if msg:
        log.notice(msg)


def _retention(ctx: Ctx) -> None:
    now = time.time()
    for name in ("conflicts", "trash"):
        for path in prune_older_than(ctx.cfg.repo / name, RETENTION_DAYS, now):
            ctx.log.info(f"removed {name}/{path.name} (older than {RETENTION_DAYS} days)")
