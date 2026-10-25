"""Command line dispatch (spec 6.12) and the run wrapper (spec 6.11)."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Mapping, Optional

from claude_sync import config, pull
from claude_sync.checks import StopRun
from claude_sync.context import Ctx, Report
from claude_sync.fsops import CiphertextError
from claude_sync.gitio import Git, GitError
from claude_sync.lock import acquire
from claude_sync.log import Log
from claude_sync.merge import MergeError

USAGE = "usage: claude-sync {pull|push|status|dry-run}"
ALIASES = {"dry": "dry-run"}
COMMANDS = ("pull", "push", "status", "dry-run")
LOCK_WAIT = {"pull": 20.0, "push": 60.0, "dry-run": 20.0}
EXPECTED_ERRORS = (StopRun, GitError, MergeError, CiphertextError)


def _push(ctx: Ctx) -> int:
    ctx.log.warn("push is not implemented yet")
    return 0


def _status(ctx: Ctx) -> int:
    print("not implemented")
    return 0


def _dry_run(ctx: Ctx) -> int:
    ctx.log.warn("dry-run is not implemented yet")
    return 0


HANDLERS = {"pull": pull.run_pull, "push": _push, "status": _status, "dry-run": _dry_run}


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
    git = Git(cfg.repo, log, base_env=None if env is os.environ else env)
    report = Report()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    ctx = Ctx(cfg=cfg, log=log, git=git, report=report, stamp=stamp)
    locked = command in LOCK_WAIT
    started = time.monotonic()
    skipped = False
    rc = 0
    try:
        if locked:
            wait = cfg.lock_wait_override if cfg.lock_wait_override is not None else LOCK_WAIT[command]
            fd = acquire(cfg.repo, wait, log)
            if fd is None:
                skipped = True
                return 0
            ctx.lock_fd = fd
        rc = HANDLERS[command](ctx)
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
    msg = report.notice(ctx.cfg.home)
    if msg:
        log.notice(msg)
