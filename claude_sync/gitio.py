"""Thin wrapper around the git command line.

Every call is `git -C <repo> ...` as an argument list with the inherited GIT_* repository
variables removed (spec 6.2).
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from claude_sync.log import Log

STRIPPED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_ASKPASS", "SSH_ASKPASS",
)
STRIPPED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
OFFLINE_PATTERNS = (
    "could not resolve host", "network is unreachable", "connection timed out",
    "connection refused", "operation timed out", "ssh: connect to host",
)
REJECT_PATTERNS = ("[rejected]", "non-fast-forward", "fetch first", "stale info")


def strip_git_env(env: Dict[str, str]) -> None:
    """Remove the inherited repository, config-injection and askpass variables in place (spec 6.2)."""
    for k in list(env):
        if k in STRIPPED_ENV or k.startswith(STRIPPED_PREFIXES):
            del env[k]


class GitError(Exception):
    def __init__(self, args_: List[str], rc: int, stderr: str) -> None:
        super().__init__(args_, rc, stderr)
        self.args_ = list(args_)
        self.rc = rc
        self.stderr = stderr

    def __str__(self) -> str:
        lines = [ln for ln in self.stderr.splitlines() if ln.strip() and not ln.startswith("hint:")]
        return f"git {' '.join(self.args_)} failed (rc={self.rc}): " + " | ".join(lines[-4:])


def classify(stderr: str, timed_out: bool) -> str:
    if timed_out:
        return "offline"
    low = stderr.lower()
    return "offline" if any(p in low for p in OFFLINE_PATTERNS) else "error"


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    old_mode: str
    new_mode: str
    old_blob: Optional[str]
    new_blob: Optional[str]


def _blob(oid: str) -> Optional[str]:
    return None if set(oid) <= {"0"} else oid


class Git:
    def __init__(self, repo: Path, log: Log, base_env: Optional[Mapping[str, str]] = None,
                 optional_locks: bool = True) -> None:
        self.repo = Path(repo)
        self.log = log
        self.base_env = base_env
        self.optional_locks = optional_locks
        self._empty_tree: Optional[str] = None

    def _env(self, extra: Optional[Mapping[str, str]]) -> Dict[str, str]:
        env = dict(self.base_env if self.base_env is not None else os.environ)
        strip_git_env(env)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
        if not self.optional_locks:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        if extra:
            env.update(extra)
        return env

    def run(self, *args: str, timeout: Optional[float] = None, input: Optional[bytes] = None,
            env: Optional[Dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess:
        argv = ["git", "-C", str(self.repo), *args]
        self.log.debug("git " + " ".join(args))
        proc = subprocess.Popen(
            argv, env=self._env(env), start_new_session=True,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            self.clear_locks(0)
            raise subprocess.TimeoutExpired(argv, timeout)
        except BaseException:
            self._kill(proc)
            raise
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, err.decode("utf-8", "replace"))
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    @staticmethod
    def _kill(proc: "subprocess.Popen[bytes]") -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass

    def _text(self, *args: str, **kw) -> str:
        return self.run(*args, **kw).stdout.decode("utf-8", "replace")

    def clear_locks(self, min_age: float) -> List[str]:
        gitdir = self.repo / ".git"
        cands = [gitdir / "index.lock", gitdir / "HEAD.lock", gitdir / "packed-refs.lock"]
        refs = gitdir / "refs"
        if refs.is_dir():
            cands.extend(sorted(refs.rglob("*.lock")))
        removed: List[str] = []
        now = time.time()
        for p in cands:
            try:
                age = now - os.lstat(p).st_mtime
                if min_age <= 0 or age >= min_age:
                    os.unlink(p)
                    removed.append(str(p))
            except OSError:
                continue
        return removed

    def rev(self, ref: str) -> Optional[str]:
        cp = self.run("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
        return cp.stdout.decode().strip() if cp.returncode == 0 else None

    def is_ancestor(self, a: str, b: str) -> bool:
        cp = self.run("merge-base", "--is-ancestor", a, b, check=False)
        if cp.returncode == 0:
            return True
        if cp.returncode == 1:
            return False
        raise GitError(["merge-base", "--is-ancestor", a, b], cp.returncode, cp.stderr.decode("utf-8", "replace"))

    def head_branch(self) -> Optional[str]:
        cp = self.run("symbolic-ref", "-q", "--short", "HEAD", check=False)
        return cp.stdout.decode().strip() if cp.returncode == 0 else None

    def empty_tree(self) -> str:
        if self._empty_tree is None:
            self._empty_tree = self._text("hash-object", "-t", "tree", "/dev/null").strip()
        return self._empty_tree

    def update_ref(self, ref: str, sha: str) -> None:
        self.run("update-ref", ref, sha)

    def delete_ref(self, ref: str) -> None:
        self.run("update-ref", "-d", ref)

    def fetch(self, branch: str, timeout: float = 20) -> str:
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        try:
            self.run("fetch", "-q", "origin", refspec, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log.warn(f"fetch timed out after {timeout:g}s, treating as offline")
            return "offline"
        except GitError as e:
            kind = classify(e.stderr, False)
            if kind == "offline":
                self.log.warn(f"offline: {e}")
            else:
                self.log.error(str(e))
            return kind
        return "ok"

    def push(self, branch: str, timeout: float = 30) -> str:
        try:
            self.run("push", "-q", "origin", f"HEAD:{branch}", timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log.warn(f"push timed out after {timeout:g}s, treating as offline")
            return "offline"
        except GitError as e:
            low = e.stderr.lower()
            if any(p in low for p in REJECT_PATTERNS):
                self.log.warn(f"push rejected: {e}")
                return "rejected"
            kind = classify(e.stderr, False)
            if kind == "offline":
                self.log.warn(f"offline: {e}")
            else:
                self.log.error(str(e))
            return kind
        return "ok"

    def diff_raw(self, a: str, b: str) -> List[Change]:
        # --no-abbrev: the default raw output abbreviates blob ids, which cannot be compared
        # with the full ids from tree_blobs.
        out = self.run("diff", "--raw", "-z", "--no-renames", "--no-abbrev", a, b, "--", "data/").stdout
        toks = out.split(b"\0")
        changes: List[Change] = []
        i = 0
        while i + 1 < len(toks):
            meta = toks[i]
            if not meta.startswith(b":"):
                i += 1
                continue
            om, nm, ob, nb, st = meta[1:].decode().split(" ")
            path = toks[i + 1].decode("utf-8", "surrogateescape")
            if path.startswith("data/"):
                path = path[len("data/"):]
            changes.append(Change(path, st[0], om, nm, _blob(ob), _blob(nb)))
            i += 2
        return changes

    def tree_blobs(self, rev: str) -> Dict[str, Tuple[str, str]]:
        out = self.run("ls-tree", "-r", "-z", rev, "--", "data/").stdout
        blobs: Dict[str, Tuple[str, str]] = {}
        for rec in out.split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            mode, _typ, oid = meta.decode().split(" ")
            p = path.decode("utf-8", "surrogateescape")
            blobs[p[len("data/"):] if p.startswith("data/") else p] = (mode, oid)
        return blobs

    def checkout_to(self, rev: str, paths: List[str], dest: Path) -> None:
        if not paths:
            return
        dest = Path(dest)
        spec = b"".join(b":(literal)data/" + p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)
        self.run(f"--work-tree={dest}", "checkout", rev, "--pathspec-from-file=-", "--pathspec-file-nul",
                 input=spec, env={"GIT_ATTR_SOURCE": rev, "GIT_INDEX_FILE": str(dest / ".index")})

    def reset_hard(self, rev: str) -> None:
        self.run("reset", "-q", "--hard", rev)

    def clean_data(self) -> None:
        self.run("clean", "-qfdx", "--", "data")

    def data_dirty(self) -> bool:
        return bool(self.run("status", "--porcelain", "--", "data").stdout.strip())

    def dirty_outside_data(self) -> List[str]:
        out = self.run("status", "--porcelain", "-z", "--no-renames", "--untracked-files=no",
                       "--", ".", ":(exclude)data").stdout
        return [rec[3:].decode("utf-8", "surrogateescape") for rec in out.split(b"\0") if rec]

    def add_data(self) -> None:
        self.run("add", "-A", "--", "data")

    def staged_data(self) -> bool:
        cp = self.run("diff", "--cached", "--quiet", "--", "data", check=False)
        if cp.returncode == 0:
            return False
        if cp.returncode == 1:
            return True
        raise GitError(["diff", "--cached", "--quiet", "--", "data"], cp.returncode,
                       cp.stderr.decode("utf-8", "replace"))

    def commit(self, message: str) -> str:
        self.run("commit", "-q", "-m", message)
        return self._text("rev-parse", "HEAD").strip()
