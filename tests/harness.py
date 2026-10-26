"""Hermetic test harness for the claude-sync rewrite.

Every git or tool process started here gets an environment built by base_env():
a temporary HOME, GIT_CONFIG_GLOBAL pointing at a temporary file, GIT_CONFIG_NOSYSTEM=1
and no XDG_CONFIG_HOME (it is set on the owner's Mac and would let git reach the real
~/.config/git/config even with a temporary HOME).
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
FORBIDDEN = [REAL_HOME / ".claude", REAL_HOME / ".claude-sync", REAL_HOME / ".config" / "git-crypt"]
WORKTREE = Path(__file__).resolve().parent.parent
TOOL = WORKTREE / "bin" / "claude-sync"

# A stand-in for a Claude session that keeps editing files: appends "round N" to each file every 50 ms.
EDITOR = (
    "import os, sys, time\n"
    "root, rounds, rels = sys.argv[1], int(sys.argv[2]), sys.argv[3:]\n"
    "for n in range(rounds):\n"
    "    for rel in rels:\n"
    "        path = os.path.join(root, rel)\n"
    "        os.makedirs(os.path.dirname(path), exist_ok=True)\n"
    "        with open(path, 'a') as fh:\n"
    "            fh.write('round %d\\n' % n)\n"
    "    time.sleep(0.05)\n"
)

Change = Optional[Union[str, bytes, Tuple[bytes, bool]]]


def assert_not_real(path) -> None:
    p = Path(path).resolve()
    for f in FORBIDDEN:
        if p == f or f in p.parents:
            raise AssertionError(f"test path {p} is inside the real {f}")


def base_env(root: Path) -> dict:
    keep = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
    path = ":".join(["/opt/homebrew/bin", "/run/current-system/sw/bin",
                     f"/etc/profiles/per-user/{pwd.getpwuid(os.getuid()).pw_name}/bin",
                     keep.get("PATH", "/usr/bin:/bin")])
    return {**keep, "PATH": path, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_TERMINAL_PROMPT": "0",
            "CLAUDE_SYNC_PYTHON": sys.executable}


def wait_for(path: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {path}")


class Machine:
    """One simulated computer: its own HOME, its own clone of the sync repo."""

    def __init__(self, case: "SyncTestCase", name: str, user: str) -> None:
        self.case = case
        self.root = case.root
        self.name = name
        self.user = user
        self.home = case.root / name / "home"
        self.repo = self.home / ".claude-sync"
        self.claude = self.home / ".claude"
        self.key = case.root / name / "no.key"
        for p in (self.home, self.repo, self.key):
            assert_not_real(p)
        self.home.mkdir(parents=True, exist_ok=True)

    def env(self, **extra: str) -> dict:
        env = base_env(self.root)
        env.update({"HOME": str(self.home), "CLAUDE_SYNC_REPO": str(self.repo),
                    "CLAUDE_SYNC_USER": self.user, "CLAUDE_SYNC_KEY": str(self.key)})
        env.update(extra)
        for k in ("HOME", "CLAUDE_SYNC_REPO", "CLAUDE_SYNC_KEY"):
            assert_not_real(env[k])
        return env

    def run(self, *args: str, env: Optional[dict] = None, timeout: float = 120,
            expect: Optional[int] = 0, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        cp = subprocess.run([str(TOOL), *args], env=env or self.env(), capture_output=True, text=True,
                            timeout=timeout, cwd=str(cwd or self.root))
        if expect is not None and cp.returncode != expect:
            raise AssertionError(f"claude-sync {' '.join(args)} exited {cp.returncode}, expected {expect}\n"
                                 f"stdout: {cp.stdout}\nstderr: {cp.stderr}\nlog:\n" + "\n".join(self.log_lines()))
        return cp

    def popen(self, *args: str, env: Optional[dict] = None, cwd: Optional[Path] = None) -> subprocess.Popen:
        return subprocess.Popen([str(TOOL), *args], env=env or self.env(), cwd=str(cwd or self.root),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def popen_editor(self, rels: List[str], rounds: int) -> subprocess.Popen:
        """Start a child that appends "round N" (N = 0 .. rounds - 1) to every rel under ~/.claude, 50 ms apart."""
        assert_not_real(self.claude)
        return subprocess.Popen([sys.executable, "-c", EDITOR, str(self.claude), str(rounds), *rels],
                                env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def write(self, rel: str, data: Union[str, bytes], exec_bit: bool = False) -> Path:
        p = self.claude / rel
        assert_not_real(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        p.chmod(0o755 if exec_bit else 0o644)
        return p

    def read(self, rel: str) -> Optional[str]:
        try:
            return (self.claude / rel).read_bytes().decode()
        except FileNotFoundError:
            return None

    def git(self, *args: str) -> str:
        cp = subprocess.run(["git", "-C", str(self.repo), *args], env=self.env(), capture_output=True, text=True)
        if cp.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed ({cp.returncode}): {cp.stderr}")
        return cp.stdout.rstrip("\n")

    def ref(self, name: str) -> Optional[str]:
        cp = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", name],
                            env=self.env(), capture_output=True, text=True)
        return cp.stdout.strip() if cp.returncode == 0 else None

    def base(self) -> Optional[str]:
        return self.ref("refs/claude-sync/base")

    def log_lines(self) -> List[str]:
        try:
            return (self.repo / "last-sync.log").read_text(errors="replace").splitlines()
        except OSError:
            return []

    def _stamped(self, top: str) -> List[str]:
        out: List[str] = []
        base = self.repo / top
        if not base.is_dir():
            return out
        for stamp in base.iterdir():
            if not stamp.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(stamp):
                for f in files:
                    out.append((Path(dirpath) / f).relative_to(stamp).as_posix())
        return sorted(out)

    def conflicts(self) -> List[str]:
        return self._stamped("conflicts")

    def trash(self) -> List[str]:
        return self._stamped("trash")


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cs-test-")).resolve()
        assert_not_real(self.root)
        self.addCleanup(shutil.rmtree, str(self.root), True)
        (self.root / "gitconfig").write_text(
            "[user]\n\tname = Test\n\temail = test@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n[core]\n\thooksPath = /dev/null\n")
        (self.root / "_home").mkdir()
        self.remote = self.root / "remote.git"
        self._init_remote()
        scratch = self.root / "_seed"
        self._clone(scratch)
        (scratch / "bin").mkdir()
        (scratch / "data").mkdir()
        shutil.copyfile(WORKTREE / ".gitignore", scratch / ".gitignore")
        (scratch / "bin" / "README").write_text("seed\n")
        (scratch / "data" / "CLAUDE.md").write_text("# shared\n")
        self._git(scratch, "add", "-A")
        self._git(scratch, "commit", "-q", "-m", "seed")
        self._git(scratch, "push", "-q", "origin", "HEAD:main")
        shutil.rmtree(scratch)

    # internal git helpers: base_env plus a temporary HOME, never the parent environment
    def _env(self) -> dict:
        env = base_env(self.root)
        env["HOME"] = str(self.root / "_home")
        return env

    def _git(self, cwd: Path, *args: str, binary: bool = False):
        cp = subprocess.run(["git", "-C", str(cwd), *args], env=self._env(), capture_output=True)
        if cp.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {cp.stderr.decode(errors='replace')}")
        return cp.stdout if binary else cp.stdout.decode().rstrip("\n")

    def _init_remote(self) -> None:
        cp = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                            env=self._env(), capture_output=True)
        if cp.returncode != 0:
            raise AssertionError(cp.stderr.decode())

    def _clone(self, dest: Path) -> None:
        cp = subprocess.run(["git", "clone", "-q", f"file://{self.remote}", str(dest)],
                            env=self._env(), capture_output=True)
        if cp.returncode != 0:
            raise AssertionError(cp.stderr.decode())

    def synced(self, *names: str, user: str = "alice") -> List[Machine]:
        """Make one machine per name and run a first pull on each, so each has a base."""
        machines = [self.machine(n, user=user) for n in names]
        for m in machines:
            m.run("pull")
        return machines

    def machine(self, name: str, user: str = "alice", clone: bool = True) -> Machine:
        m = Machine(self, name, user)
        if clone:
            cp = subprocess.run(["git", "clone", "-q", f"file://{self.remote}", str(m.repo)],
                                env=m.env(), capture_output=True)
            if cp.returncode != 0:
                raise AssertionError(cp.stderr.decode())
        return m

    def remote_commit(self, changes: Dict[str, Change], message: str = "other",
                      root_relative: bool = False) -> str:
        other = self.root / "_other"
        if (other / ".git").is_dir():
            self._git(other, "fetch", "-q", "origin")
            self._git(other, "reset", "-q", "--hard", "origin/main")
        else:
            self._clone(other)
        for rel, val in changes.items():
            p = other / rel if root_relative else other / "data" / rel
            if val is None:
                if p.exists():
                    p.unlink()
                continue
            exec_bit: Optional[bool] = None
            if isinstance(val, tuple):
                data, exec_bit = val
            elif isinstance(val, str):
                data = val.encode()
            else:
                data = val
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            if exec_bit is not None:
                p.chmod(0o755 if exec_bit else 0o644)
        self._git(other, "add", "-A")
        self._git(other, "commit", "-q", "-m", message)
        self._git(other, "push", "-q", "origin", "HEAD:main")
        return self._git(other, "rev-parse", "HEAD")

    def remote_tip(self) -> str:
        return self._git(self.remote, "rev-parse", "main")

    def remote_paths(self) -> List[str]:
        out = self._git(self.remote, "ls-tree", "-r", "-z", "--name-only", "main", "--", "data/", binary=True)
        return sorted(r.decode()[len("data/"):] for r in out.split(b"\0") if r)

    def make_gitcrypt_remote(self) -> Path:
        path = self._env()["PATH"]
        if shutil.which("git-crypt", path=path) is None:
            self.skipTest("git-crypt is not installed")
        shutil.rmtree(self.remote)
        self._init_remote()
        scratch = self.root / "_gcseed"
        self._clone(scratch)
        self._git(scratch, "config", "user.name", "Test")
        env = self._env()
        key = self.root / "test.key"
        cp = subprocess.run(["git-crypt", "init"], cwd=str(scratch), env=env, capture_output=True)
        if cp.returncode != 0:
            raise AssertionError(f"git-crypt init failed: {cp.stderr.decode()}")
        (scratch / ".gitattributes").write_text("data/** filter=git-crypt diff=git-crypt\n")
        (scratch / "bin").mkdir()
        (scratch / "data").mkdir()
        shutil.copyfile(WORKTREE / ".gitignore", scratch / ".gitignore")
        (scratch / "bin" / "README").write_text("seed\n")
        (scratch / "data" / "CLAUDE.md").write_text("# shared\n")
        self._git(scratch, "add", "-A")
        self._git(scratch, "commit", "-q", "-m", "seed")
        self._git(scratch, "push", "-q", "origin", "HEAD:main")
        cp = subprocess.run(["git-crypt", "export-key", str(key)], cwd=str(scratch), env=env, capture_output=True)
        if cp.returncode != 0:
            raise AssertionError(f"git-crypt export-key failed: {cp.stderr.decode()}")
        shutil.rmtree(scratch)
        shutil.rmtree(self.root / "_other", ignore_errors=True)
        return key
