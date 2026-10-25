# claude-sync rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bash `bin/claude-sync` with a three-way sync in Python that never overwrites a local edit without a merge or a kept copy, plus the matching dotnix hook changes.

**Architecture:** A bash shim picks a Python 3.9+ interpreter and runs the `claude_sync` package from the repo it lives in. Each machine keeps a local base ref; pull diffs base to `origin/main` and decides per path with a pure decision function and `git merge-file`; push scans `~/.claude` against the `data/` work tree and builds a linear commit on the remote tip. An `fcntl.flock` lock serialises runs.

**Tech Stack:** Python 3.9+ standard library only (`unittest`, `subprocess`, `fcntl`, `json`, `dataclasses`), git 2.54, git-crypt 0.8, bash for the shim, Nix for the hooks module.

**Spec:** `docs/superpowers/specs/2026-10-25-claude-sync-rewrite-design.md` (approved by the owner on 2026-10-25). Every task implements the spec section it names; when this plan and the spec disagree, stop and report.

## Global Constraints

- Python 3.9 or newer, standard library only. No `match`, no `X | Y` unions evaluated at runtime (use `from __future__ import annotations`), no pip installs. Every test passes under `/usr/bin/python3` (3.9.6) and `/etc/profiles/per-user/dannyfroberg/bin/python3` (3.14).
- No function-level imports inside `claude_sync/`: `cli.py` imports every module at startup, so the self-update (`git reset --hard` mid-run) can never load a newer module into a running process.
- The command line stays `claude-sync pull|push|status|dry-run` (`dry` alias). Exit 0 for success, skip or offline; 1 for an error; 2 for bad usage.
- Log line format, exactly: `2026-10-25T16:31:02Z pid=1234 pull WARN <message>` (UTC, `Z`). Levels DEBUG (written only with `CLAUDE_SYNC_DEBUG=1`), INFO, WARN, ERROR. Rotation above 5 MB to `last-sync.log.1`.
- stdout notice: one plain-text line, at most 200 characters, only for kept-both conflicts, local-edit-beats-delete, ERROR, or no usable python.
- Lock waits: pull 20 s, push 60 s, dry-run 20 s; `CLAUDE_SYNC_LOCK_WAIT` overrides all; status takes no lock.
- Fetch timeout 20 s, push timeout 30 s, at most 3 push attempts. Mass delete threshold: more than 20 deletions and more deletions than kept files. Empty-file restore age: 10 minutes. Retention: 30 days.
- Allowlist and canonicalisation exactly as the old script (`bin/claude-sync` at commit 1af679a4): FILES `CLAUDE.md settings.json mcp_settings.json keybindings.json`; DIRS `skills agents commands hooks`; memory `projects/<proj>/memory/**` stored as `data/memory/<canonical proj>/**`; text extensions `.md .json .jsonl .txt .sh .bash .zsh .lua .toml .yaml .yml .cfg`; byte replacements `/Users/<me>/` to `/Users/__USER__/` and `-Users-<me>-` to `-Users-__USER__-`, reversed on the way out.
- Tests are hermetic: a temporary HOME, a temporary sync repo, and a local bare remote per test. No test touches the network, `~/.claude`, `~/.claude-sync`, `~/.config/git-crypt` or the real key. The harness unsets `XDG_CONFIG_HOME` (it is set on this Mac and makes git write the real `~/.config/git/config` even with a temporary HOME) and sets `GIT_CONFIG_GLOBAL` to a temporary file and `GIT_CONFIG_NOSYSTEM=1`.
- Never run `git-crypt unlock` in this worktree. Never push. Never write to `~/.claude`, `~/.claude-sync` or `~/dotnix` outside the dotnix worktree of Task 12.
- Commits: conventional (`feat:`, `test:`, `fix:`, `docs:`, `chore:`), author Danny Froberg, no AI attribution lines. No em dashes and no arrows in docs and commit messages.

## Decisions this plan adds to the spec

- `CLAUDE_SYNC_PYTHON`: when set, the shim uses that interpreter and no other. The harness sets it to `sys.executable`, so the suite really runs the tool under each Python; setting it to a missing path tests the no-python branch.
- `CLAUDE_SYNC_TEST_HOLD=<point>`: at a named checkpoint the tool creates `$REPO/.hold-<point>` and waits (up to 60 s) until the test deletes it. Tests use it to SIGKILL or to race at an exact point. Points: `pull:before-replace`, `pull:before-reset`, `pull:before-base`, `push:before-commit`, `push:before-push`, `push:after-push`. A no-op when the variable is unset.
- Content for `B` and `R` is read from the `data/` work tree whenever `HEAD`'s blob id for that path equals the needed blob id (after the invariant restore the work tree is `HEAD`); only other blobs go through the temporary checkout. This keeps a first pull (about 1368 files, about 20 ms each through git-crypt, measured) mostly free of decryption.
- git-crypt lock state: the repo uses git-crypt when `$REPO/.gitattributes` contains `filter=git-crypt`; it is unlocked when `$REPO/.git/git-crypt/keys/default` exists. This replaces the old `data/CLAUDE.md` sniff; the ciphertext guard stays.
- Symlinks inside synced trees: a path inside `skills/ agents/ commands/ hooks/` or a memory tree that is a symlink, or passes through a symlinked directory, is outside the sync on this machine in both directions: never collected, never counted as a local delete, never read, written or trashed by pull (one INFO line per run with the count). Three such directories exist on the main Mac today (`skills/orca-cli`, `skills/orchestration`, `skills/computer-use`, symlinks into `~/.agents/skills`, not tracked in the repo); without this rule a remote file at those paths would be written into another app's directory. The spec's "writing into a symlink writes its target" is kept for the four top-level FILES only.
- HEAD must be attached to `refs/heads/<BRANCH>`. If it is detached or on another branch (a dead rebase leaves it detached), the start checks run `git checkout -q -f -B <BRANCH>` at the current HEAD commit, with a WARN, before the invariant restore.
- Remote 0-byte version over a non-empty local file: never written; the local file is kept with a WARN (`remote empty file not applied`), so the next push restores the content on the remote. Same reasoning as the spec's push-side empty-file guard.

## Review Focus

1. A remote 0-byte version of a file that is non-empty locally: the pull keeps the local bytes and logs WARN; the next push sends them back. Test in Task 4 (`test_remote_empty_never_applied`) and Task 6 (`test_pull_keeps_local_when_remote_emptied`).
2. Symlinked directories inside a synced tree (live today): pull never writes through them and push never deletes their paths on the remote. Tests in Task 2 (`test_scan_skips_symlinked_dirs`), Task 7 (`test_pull_never_writes_through_symlinked_dir`) and Task 8 (`test_push_never_deletes_symlinked_paths`).
3. File names with spaces and non-ASCII characters (`MEMORY notes.md`, `résumé.md`) through pull, push and conflicts. Tests in Task 3 (`test_diff_raw_odd_names`) and Task 6 (`test_odd_names_round_trip`).
4. A user name that is a prefix of another (`al` and `alice`) or appears twice in a project directory name: canonicalisation touches only exact `/Users/<me>/` and `-Users-<me>-`. Test in Task 2 (`test_canon_prefix_user_untouched`).
5. A session writes a file between the moment pull reads it and the moment pull replaces it: the new local bytes are re-read and decided again, never overwritten. Test in Task 6 (`test_edit_during_pull_window_survives`).

---

## File map

```
bin/claude-sync              shim (replaces the bash tool)               Task 6
claude_sync/__init__.py      empty                                       Task 1
claude_sync/__main__.py      runs cli.main                               Task 6
claude_sync/config.py        Config, load()                              Task 1
claude_sync/log.py           Log (file log, rotation, notice)            Task 1
claude_sync/paths.py         allowlist, canonicalisation, scan           Task 2
claude_sync/fsops.py         Content, atomic writes, trash, conflicts    Task 2
claude_sync/gitio.py         Git wrapper, classify, diff, temp checkout  Task 3
claude_sync/merge.py         merge-file wrapper, JSON and link checks    Task 4
claude_sync/decide.py        the 6.8 decision table                      Task 4
claude_sync/lock.py          flock lock, legacy lock dir                 Task 5
claude_sync/context.py       Ctx, Report, checkpoint                     Task 5
claude_sync/checks.py        start checks 6.6 and invariant restore      Task 5
claude_sync/pull.py          pull 6.7                                    Tasks 6, 7
claude_sync/push.py          push 6.10                                   Task 8
claude_sync/cli.py           dispatch, status, dry-run, retention        Tasks 6, 9
tests/harness.py             hermetic SyncTestCase and Machine           Task 1
tests/test_*.py              one module per task (named in each task)
.gitignore, README.md                                                    Tasks 1, 9
~/dotnix worktree: home-manager/claude-sync-hooks.nix and
  home-manager/claude-sync-hooks-ensure.py (+ _test.py)                  Task 12
```

Run commands used below (from the worktree root):
- One module: `python3 -m unittest tests.test_log -v`
- Whole suite, both interpreters:
  `/usr/bin/python3 -m unittest discover -s tests -t . && /etc/profiles/per-user/dannyfroberg/bin/python3 -m unittest discover -s tests -t .`

---

### Task 1: Package skeleton, config, log, hermetic harness

**Files:**
- Create: `claude_sync/__init__.py`, `claude_sync/config.py`, `claude_sync/log.py`, `tests/__init__.py`, `tests/harness.py`
- Modify: `.gitignore` (append the spec 6.1 entries plus `last-sync.log.1` and `.hold-*`)
- Test: `tests/test_harness.py`, `tests/test_config.py`, `tests/test_log.py`

**Interfaces:**
- Produces:
  - `config.Config` (frozen dataclass): `home: Path, claude: Path, repo: Path, data: Path, me: str, branch: str, key: Path, allow_prune: bool, debug: bool, lock_wait_override: Optional[float]`.
  - `config.load(env: Mapping[str, str]) -> Config`. `claude = home/.claude` always (ignores `CLAUDE_CONFIG_DIR`); `repo = CLAUDE_SYNC_REPO or home/.claude-sync`; `me = CLAUDE_SYNC_USER or pwd login name`; `branch = CLAUDE_SYNC_BRANCH or "main"`; `key = CLAUDE_SYNC_KEY or home/.config/git-crypt/claude-sync.key`; `allow_prune = CLAUDE_SYNC_ALLOW_PRUNE == "1"`; `debug = CLAUDE_SYNC_DEBUG == "1"`; `lock_wait_override = float(CLAUDE_SYNC_LOCK_WAIT)` or None. `home` from `HOME`.
  - `log.Log(path: Path, command: str, debug: bool, out: TextIO = sys.stdout)` with `debug/info/warn/error(msg: str) -> None`, `exception(msg: str) -> None` (one log line per traceback line), `notice(msg: str) -> None` (prints `msg` truncated to 200 chars plus newline to `out`; called at most once per run by the caller). Never raises on OSError. Newlines inside `msg` become ` | `.
  - `tests.harness.SyncTestCase`, `Machine`, `REAL_HOME`, `assert_not_real(path)` as below.

- [ ] **Step 1: Write the failing tests**

`tests/test_harness.py`:
```python
def test_refuses_real_paths(self):
    for p in [REAL_HOME / ".claude", REAL_HOME / ".claude-sync" / "x", REAL_HOME / ".config/git-crypt/k"]:
        with self.assertRaises(AssertionError):
            assert_not_real(p)

def test_env_is_hermetic(self):
    m = self.machine("a")
    env = m.env()
    self.assertNotIn("XDG_CONFIG_HOME", env)
    self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
    self.assertTrue(env["GIT_CONFIG_GLOBAL"].startswith(str(self.root)))
    self.assertTrue(env["HOME"].startswith(str(self.root)))
    out = subprocess.run(["git", "config", "--global", "--list", "--show-origin"],
                         env=env, capture_output=True, text=True).stdout
    self.assertNotIn(str(REAL_HOME / ".config"), out)

def test_machine_clone_and_remote_commit(self):
    m = self.machine("a")
    sha = self.remote_commit({"memory/-Users-__USER__-p/n.md": "hi\n"})
    self.assertEqual(len(sha), 40)
    self.assertTrue((m.repo / ".git").is_dir())
```
`tests/test_config.py`: `test_defaults` (HOME only: `claude == home/.claude`, `repo == home/.claude-sync`, `branch == "main"`), `test_overrides` (every env var above), `test_config_dir_ignored` (`CLAUDE_CONFIG_DIR=/elsewhere` leaves `claude == home/.claude`).
`tests/test_log.py`:
```python
def test_line_format(self):
    Log(p, "pull", False).warn("x")
    self.assertRegex(p.read_text(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ pid=\d+ pull WARN x\n$")
def test_debug_only_when_enabled(self): ...      # debug() writes nothing with debug=False, one line with True
def test_rotation_over_5mb(self):                # pre-fill 5*1024*1024+1 bytes; next write moves it to last-sync.log.1
    ...; self.assertTrue((p.parent / "last-sync.log.1").exists()); self.assertLess(p.stat().st_size, 200)
def test_notice_truncated(self):                 # notice("a"*300) prints exactly 200 chars plus "\n"
def test_unwritable_log_does_not_raise(self):    # path inside a read-only dir: warn() returns None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_harness tests.test_config tests.test_log -v`
Expected: errors, `ModuleNotFoundError: No module named 'claude_sync'` / `tests.harness`.

- [ ] **Step 3: Implement `tests/harness.py`**

The env is the safety boundary, so build it exactly like this:
```python
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
FORBIDDEN = [REAL_HOME / ".claude", REAL_HOME / ".claude-sync", REAL_HOME / ".config" / "git-crypt"]
WORKTREE = Path(__file__).resolve().parent.parent
TOOL = WORKTREE / "bin" / "claude-sync"

def assert_not_real(path) -> None:
    p = Path(path).resolve()
    for f in FORBIDDEN:
        assert p != f and f not in p.parents, f"test path {p} is inside the real {f}"

def base_env(root: Path) -> dict:
    keep = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
    path = ":".join(["/opt/homebrew/bin", "/run/current-system/sw/bin",
                     f"/etc/profiles/per-user/{pwd.getpwuid(os.getuid()).pw_name}/bin",
                     keep.get("PATH", "/usr/bin:/bin")])
    return {**keep, "PATH": path, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_TERMINAL_PROMPT": "0",
            "CLAUDE_SYNC_PYTHON": sys.executable}
```
`SyncTestCase.setUp`: `self.root = Path(tempfile.mkdtemp(prefix="cs-test-")).resolve()`; write `root/gitconfig` with `user.name=Test`, `user.email=test@example.invalid`, `init.defaultBranch=main`, `core.hooksPath=/dev/null`; create the bare remote `root/remote.git`; seed it from a scratch clone with `.gitignore` (copied from `WORKTREE/.gitignore`), `bin/README` (`seed\n`) and `data/CLAUDE.md` (`# shared\n`). `tearDown` removes `root`.
Methods:
- `machine(name, user="alice", clone=True) -> Machine`: home `root/name/home`, repo `home/.claude-sync`, `git clone -q file://root/remote.git repo` when `clone`. Calls `assert_not_real` on home, repo and key path.
- `remote_commit(changes: Dict[str, Optional[Union[str, bytes, Tuple[bytes, bool]]]], message="other") -> str`: applies to a scratch clone `root/_other` (paths are data-relative; `None` deletes; a tuple sets the exec bit), commits, pushes, returns the new sha.
- `make_gitcrypt_remote() -> Path`: skips the test (`self.skipTest`) when `shutil.which("git-crypt", path=env PATH)` is None; otherwise rebuilds `remote.git` from a seed with `git-crypt init`, `.gitattributes` `data/** filter=git-crypt diff=git-crypt`, exports the key to `root/test.key` and returns it.
`Machine`: `env(**extra) -> dict` (base_env plus `HOME`, `CLAUDE_SYNC_REPO`, `CLAUDE_SYNC_USER`, `CLAUDE_SYNC_KEY=root/<name>/no.key`, extras; asserts not real); `run(*args, env=None, timeout=120, expect=0) -> CompletedProcess` (text; asserts the exit code when `expect` is not None); `popen(*args, env=None)`; `write(rel, data, exec_bit=False) -> Path` and `read(rel) -> Optional[str]` relative to `home/.claude`; `git(*args) -> str`; `ref(name) -> Optional[str]` (`git rev-parse -q --verify <name>`, None when missing); `base() -> Optional[str]` (`ref("refs/claude-sync/base")`); `log_lines() -> List[str]`; `conflicts() -> List[Path]` and `trash() -> List[Path]` (files, relative to the stamp folder).
Module-level helper: `wait_for(path: Path, timeout: float = 30) -> None` (polls every 0.05 s, fails the test on timeout).

- [ ] **Step 4: Implement `claude_sync/config.py` and `claude_sync/log.py`** to the interfaces above. Rotation check happens before each write.

- [ ] **Step 5: Append to `.gitignore`**

```
.sync.lock
.fresh-machine
conflicts/
trash/
__pycache__/
*.sync-tmp.*
last-sync.log.1
.hold-*
```

- [ ] **Step 6: Run to verify they pass**, under both interpreters (command in "Run commands"). Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add claude_sync tests .gitignore
git commit -m "test: add hermetic harness, config and log for claude-sync rewrite"
```

---

### Task 2: Paths, canonicalisation, local scan, file operations

**Files:**
- Create: `claude_sync/paths.py`, `claude_sync/fsops.py`
- Test: `tests/test_paths.py`, `tests/test_fsops.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (`paths.py`):
  - constants `TOKEN = "__USER__"`, `FILES`, `DIRS`, `TEXT_EXT` (values in Global Constraints).
  - `is_text(rel: str) -> bool` (lower-cased suffix in `TEXT_EXT`); `is_tmp(name: str) -> bool` (`".sync-tmp." in name`).
  - `canon(data: bytes, me: str) -> bytes`, `decanon(data: bytes, me: str) -> bytes`.
  - `to_repo(local_rel: str, me: str) -> Optional[str]` and `to_local(repo_rel: str, me: str) -> Optional[str]`. `local_rel` is relative to `~/.claude`, `repo_rel` to `data/` (POSIX separators). `projects/-Users-alice-p/memory/a/b.md` and `memory/-Users-__USER__-p/a/b.md` map to each other; FILES and DIRS map to themselves; anything else is `None`.
  - `scan_local(claude: Path, me: str) -> Tuple[Dict[str, Path], Set[str]]`: returns (`repo_rel` to absolute local path for every syncable regular file; set of `repo_rel` prefixes that are outside the sync because of a symlink, each ending in `/` for a directory). FILES are followed if they resolve to a regular file. Inside DIRS and memory trees: `os.walk(followlinks=False)`; symlinked files and directories are reported in the second set and not descended; names matching `is_tmp` are skipped. A memory tree whose `projects/<p>` or `projects/<p>/memory` is itself a symlink counts as a symlinked directory.
  - `via_symlink(claude: Path, local_rel: str) -> bool`: True when any component below the allowlist root (`skills/`, `projects/<p>/memory/`, and so on), including the file itself, is a symlink. Always False for the four FILES.
- Produces (`fsops.py`):
  - `Content` (frozen dataclass): `data: bytes, exec: bool`.
  - `class CiphertextError(Exception)`; `is_ciphertext(data: bytes) -> bool` (`data.startswith(b"\x00GITCRYPT")`).
  - `read_content(path: Path) -> Optional[Content]` (None if missing or not a regular file after following a top-level symlink); `stat_key(path: Path) -> Optional[Tuple[int, int]]` (`(st_size, st_mtime_ns)`).
  - `atomic_write(path: Path, content: Content) -> None`: if `path` is a symlink, write its resolved target; create parents with `mkdir(parents=True, exist_ok=True)` (a regular file in the way raises `NotADirectoryError` or `FileExistsError`, re-raised as `NotADirectoryError`); write `<name>.sync-tmp.<pid>` in the same directory, `fsync`, chmod 0o755 or 0o644 according to `content.exec` (preserving other bits of an existing file is not required), `os.replace`; a directory at `path` raises `IsADirectoryError`. Raises `CiphertextError` before writing anything when `is_ciphertext(content.data)`.
  - `put_aside(root: Path, rel: str, content: Content) -> Path`: writes into `root/rel`; if that file exists, appends `.1`, `.2`, and so on. Returns the path.
  - `move_aside(src: Path, root: Path, rel: str) -> Path`: moves (`os.replace`, falling back to `shutil.move`) into `root/rel` with the same suffix rule.
  - `prune_older_than(root: Path, days: int, now: float) -> List[Path]`: removes direct subfolders of `root` whose mtime is older than `days`; returns them.

- [ ] **Step 1: Write the failing tests**

`tests/test_paths.py`:
```python
def test_canon_round_trip(self):
    raw = b"see /Users/alice/x and -Users-alice-proj- and /Users/alicex/"
    c = canon(raw, "alice")
    self.assertEqual(c, b"see /Users/__USER__/x and -Users-__USER__-proj- and /Users/alicex/")
    self.assertEqual(decanon(c, "bob"), b"see /Users/bob/x and -Users-bob-proj- and /Users/alicex/")
def test_canon_prefix_user_untouched(self):        # Review Focus 4
    self.assertEqual(canon(b"/Users/alice/ -Users-alice- -Users-al-x-Users-al-", "al"),
                     b"/Users/alice/ -Users-alice- -Users-__USER__-x-Users-__USER__-")
def test_mapping(self):
    self.assertEqual(to_repo("projects/-Users-alice-p/memory/a/b.md", "alice"), "memory/-Users-__USER__-p/a/b.md")
    self.assertEqual(to_local("memory/-Users-__USER__-p/a/b.md", "bob"), "projects/-Users-bob-p/memory/a/b.md")
    self.assertEqual(to_repo("skills/s/SKILL.md", "alice"), "skills/s/SKILL.md")
    self.assertIsNone(to_repo("projects/-Users-alice-p/transcript.jsonl", "alice"))
    self.assertIsNone(to_repo("plugins/x.json", "alice"))
def test_is_text(self): ...                        # .md .JSON .sh True; .png .bin False
def test_scan_collects_allowlist_only(self): ...   # FILES, a skill, an agent, a memory file; skips plugins/, transcripts, *.sync-tmp.* files
def test_scan_skips_symlinked_dirs(self):          # Review Focus 2
    # skills/linked is a symlink to an outside dir holding SKILL.md; skills/real/SKILL.md regular
    files, skipped = scan_local(claude, "alice")
    self.assertIn("skills/real/SKILL.md", files)
    self.assertNotIn("skills/linked/SKILL.md", files)
    self.assertIn("skills/linked/", skipped)
def test_scan_follows_top_level_file_symlink(self): ...   # CLAUDE.md as a symlink to elsewhere/real.md is collected
def test_via_symlink(self): ...                    # True under skills/linked/, False for skills/real/x, False for CLAUDE.md symlink
```
`tests/test_fsops.py`:
```python
def test_atomic_write_sets_exec_and_leaves_no_tmp(self): ...
def test_atomic_write_through_top_level_symlink(self): ...   # link stays a link, target gets bytes
def test_type_clash_directory_in_the_way(self):
    (d / "x").mkdir(); (d / "x" / "f").write_text("keep")
    with self.assertRaises(IsADirectoryError):
        atomic_write(d / "x", Content(b"new", False))
    self.assertEqual((d / "x" / "f").read_text(), "keep")
def test_type_clash_file_in_the_way(self):
    (d / "x").write_text("file")
    with self.assertRaises(NotADirectoryError):
        atomic_write(d / "x" / "y", Content(b"new", False))
    self.assertEqual((d / "x").read_text(), "file")
def test_ciphertext_refused(self):
    with self.assertRaises(CiphertextError):
        atomic_write(d / "c", Content(b"\x00GITCRYPT\x00abc", False))
    self.assertFalse((d / "c").exists())
def test_put_aside_never_overwrites(self): ...      # second call returns path ending ".1"
def test_prune_older_than(self): ...                # folders with mtime 31 days ago removed, 29 days kept (os.utime)
```

- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_paths tests.test_fsops -v`. Expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement `paths.py` and `fsops.py`** to the interfaces. `canon` and `decanon` are two `bytes.replace` calls each, in the order of the old `sed` (path form first).
- [ ] **Step 4: Run to verify they pass** under both interpreters.
- [ ] **Step 5: Commit**: `git add claude_sync tests && git commit -m "feat: add path mapping, scan and atomic file operations"`

---

### Task 3: Git wrapper

**Files:**
- Create: `claude_sync/gitio.py`
- Test: `tests/test_gitio.py`

**Interfaces:**
- Consumes: `log.Log`, `fsops.Content`.
- Produces:
  - `class GitError(Exception)` with `args_: List[str]`, `rc: int`, `stderr: str`; `str()` gives the command and the last 4 stderr lines without `hint:` lines.
  - `classify(stderr: str, timed_out: bool) -> str`: `"offline"` when `timed_out` or stderr matches (case-insensitive) any of `could not resolve host`, `network is unreachable`, `connection timed out`, `connection refused`, `operation timed out`, `ssh: connect to host`; else `"error"`.
  - `Change` (frozen dataclass): `path: str` (data-relative), `status: str` (`A M D T`), `old_mode: str`, `new_mode: str`, `old_blob: Optional[str]`, `new_blob: Optional[str]` (None for the all-zero id).
  - `Git(repo: Path, log: Log, base_env: Optional[Dict[str, str]] = None)` (`base_env` replaces `os.environ` as the starting environment; tests pass the harness env) with:
    - `run(*args: str, timeout: Optional[float] = None, input: Optional[bytes] = None, env: Optional[Dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess` (bytes). Environment adds `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, and `GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=10` unless already set. Starts git with `start_new_session=True`; on timeout kills the whole process group (`os.killpg(pid, SIGKILL)`) so no ssh child outlives it, then raises `subprocess.TimeoutExpired`. Raises `GitError` when `check` and rc != 0.
    - `rev(ref: str) -> Optional[str]`, `is_ancestor(a: str, b: str) -> bool`, `merge_base(a: str, b: str) -> Optional[str]`, `empty_tree() -> str` (`git hash-object -t tree /dev/null`), `update_ref(ref: str, sha: str) -> None`, `delete_ref(ref: str) -> None`, `head_branch() -> Optional[str]` (`symbolic-ref -q --short HEAD`).
    - `fetch(branch: str, timeout: float = 20) -> str`: runs `fetch -q origin +refs/heads/<b>:refs/remotes/origin/<b>`; returns `"ok"`, `"offline"` or `"error"` (logging WARN or ERROR with `GitError` text).
    - `push(branch: str, timeout: float = 30) -> str`: `push -q origin HEAD:<b>`; returns `"ok"`, `"rejected"` (stderr contains `[rejected]`, `non-fast-forward`, `fetch first` or `stale info`), `"offline"` or `"error"`.
    - `diff_raw(a: str, b: str) -> List[Change]`: `diff --raw -z --no-renames <a> <b> -- data/`, paths returned without the `data/` prefix.
    - `tree_blobs(rev: str) -> Dict[str, Tuple[str, str]]`: `ls-tree -r -z <rev> -- data/`, data-relative path to `(mode, blob)`.
    - `checkout_to(rev: str, paths: List[str], dest: Path) -> None`: the spec 6.7 step 7 command, verbatim: `GIT_ATTR_SOURCE=<rev> GIT_INDEX_FILE=<dest>/.index git --work-tree=<dest> checkout <rev> --pathspec-from-file=- --pathspec-file-nul`, input `data/<p>\0` per path. No-op for an empty list.
    - `reset_hard(rev: str) -> None`, `clean_data() -> None` (`clean -qfdx -- data`), `data_dirty() -> bool` (`status --porcelain -- data` non-empty), `dirty_outside_data() -> List[str]` (`status --porcelain --untracked-files=no -- . :(exclude)data`), `add_data() -> None` (`add -A -- data`), `commit(message: str) -> str` (`commit -q -m`; returns new HEAD).

- [ ] **Step 1: Write the failing tests** (`tests/test_gitio.py`, a `SyncTestCase` subclass so git runs hermetically; construct `Git(m.repo, log, base_env=m.env())`, never patch `os.environ`).
```python
def test_classify(self):
    self.assertEqual(classify("ssh: Could not resolve hostname github.com", False), "error")  # not in the list
    self.assertEqual(classify("fatal: unable to access: Could not resolve host: github.com", False), "offline")
    self.assertEqual(classify("ssh: connect to host github.com port 22: Operation timed out", False), "offline")
    self.assertEqual(classify("ERROR: Repository not found.", False), "error")
    self.assertEqual(classify("", True), "offline")
def test_diff_raw_add_modify_delete_mode(self): ...   # remote_commit an add, a modify, a delete, a chmod-only; one Change each with right status/modes
def test_diff_raw_from_empty_tree(self): ...          # every data file listed as status "A"
def test_diff_raw_odd_names(self):                    # Review Focus 3
    self.remote_commit({"memory/-Users-__USER__-p/MEMORY notes.md": "a\n", "memory/-Users-__USER__-p/résumé.md": "b\n"})
    paths = {c.path for c in g.diff_raw(g.empty_tree(), "origin/main")}
    self.assertIn("memory/-Users-__USER__-p/MEMORY notes.md", paths)
    self.assertIn("memory/-Users-__USER__-p/résumé.md", paths)
def test_checkout_to_runs_filter_from_attr_source(self):
    # repo-local filter: git config filter.up.smudge "sed s/$/XXXX/"; .gitattributes "data/** filter=up" committed
    g.checkout_to("HEAD", ["a.txt"], tmp)
    self.assertEqual((tmp / "data/a.txt").read_bytes(), b"helloXXXX\n")
    self.assertEqual(m.git("status", "--porcelain"), "")
def test_fetch_updates_stale_tracking_ref(self):      # spec test 29
    old = m.git("rev-parse", "origin/main"); new = self.remote_commit({"x.md": "1\n"})
    m.git("update-ref", "refs/remotes/origin/main", old)
    self.assertEqual(g.fetch("main"), "ok"); self.assertEqual(g.rev("origin/main"), new)
def test_fetch_error_vs_offline(self): ...            # origin file:///nonexistent gives "error"; origin ssh://example.invalid/x with GIT_SSH_COMMAND=<root>/fakessh (a script that sleeps 5 s) and timeout=1 gives "offline" in under 3 s
def test_push_rejected(self): ...                     # local commit on old tip after remote_commit gives "rejected"
```
- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_gitio -v`. Expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement `gitio.py`.** Parse `diff --raw -z`: records are `:<om> <nm> <ob> <nb> <st>\0<path>\0`. The fake ssh script never opens a socket, so the offline test touches no network.
- [ ] **Step 4: Run to verify they pass** under both interpreters.
- [ ] **Step 5: Commit**: `git commit -m "feat: add git wrapper with explicit fetch refspec and temp checkout"`

---

### Task 4: Merge rules and the decision table

**Files:**
- Create: `claude_sync/merge.py`, `claude_sync/decide.py`
- Test: `tests/test_merge.py`, `tests/test_decide.py`

**Interfaces:**
- Consumes: `fsops.Content`, `paths.is_text`.
- Produces (`merge.py`):
  - `links(data: bytes) -> Set[str]`: every `x.md` in `](x.md)` (regex `rb"\]\(([^)]+\.md)\)"`).
  - `merge_text(local: bytes, base: Optional[bytes], remote: bytes, union: bool) -> Optional[bytes]`: `git merge-file -p [--union] L B R` on files in a `tempfile.mkdtemp()` directory (removed after); `base None` means an empty file. Returns stdout when rc == 0, else None (rc < 0 is an error: raise `RuntimeError`).
  - `merge_exec(l: bool, b: Optional[bool], r: bool, fresh: bool) -> bool`: spec 6.8 bit rule.
  - `merge_content(rel: str, L: Content, B: Optional[Content], R: Content, fresh: bool) -> Optional[Content]`: None when not text (by `is_text(rel)`); `MEMORY.md` basename uses `union=True` and then requires every link in `(links(L) & links(R)) | (links(L) - links(B)) | (links(R) - links(B))` to be in the result (`links(B)` is empty when B is None); `.json` results must `json.loads` after UTF-8 decode; any failure returns None.
- Produces (`decide.py`):
  - `Act` (Enum): `NOTHING, WRITE, TRASH, KEEP_LOCAL, KEEP_BOTH, TAKE_REMOTE` (`TAKE_REMOTE`: write R, park L; only on a `.fresh-machine` first pull).
  - `Outcome` (frozen dataclass): `act: Act, content: Optional[Content] = None, warn: Optional[str] = None, merged: bool = False`.
  - `decide(rel: str, L: Optional[Content], B: Optional[Content], R: Optional[Content], fresh: bool) -> Outcome`. Order of rules, first match wins:
    1. `L == R`: NOTHING.
    2. `R` is not None and `R.data == b""` and `L` is not None and `L.data != b""`: KEEP_LOCAL, warn `remote empty file not applied` (Decisions).
    3. `B` is not None and `L == B`: `R` None gives TRASH; else WRITE R.
    4. `L` None and `B` not None: WRITE R, warn `remote edit beats local delete`.
    5. `L` None and `B` None: WRITE R.
    6. `R` None (so `L != B`): KEEP_LOCAL, warn `local edit beats remote delete`.
    7. Otherwise merge: `merge_content(rel, L, B, R, fresh)`; a result gives WRITE with `merged=True`; None gives TAKE_REMOTE when `B is None and fresh`, else KEEP_BOTH.

- [ ] **Step 1: Write the failing tests**

`tests/test_merge.py`:
```python
def test_clean_merge_different_places(self):        # spec test 3
    b = b"1\n2\n3\n4\n5\n"; l = b"1x\n2\n3\n4\n5\n"; r = b"1\n2\n3\n4\n5x\n"
    self.assertEqual(merge_text(l, b, r, False), b"1x\n2\n3\n4\n5x\n")
def test_conflict_returns_none(self): ...            # same line changed both sides: None (spec test 4)
def test_empty_base_shares_common_lines(self):       # verified behaviour, spec 6.9
    l = b"- [A](a.md) x\n- [L1](l1.md) z\n"; r = b"- [A](a.md) x\n- [R1](r1.md) w\n"
    self.assertEqual(merge_text(l, None, r, True), b"- [A](a.md) x\n- [L1](l1.md) z\n- [R1](r1.md) w\n")
    self.assertIsNone(merge_text(l, None, r, False))
def test_memory_union_keeps_both_additions(self): ...      # spec test 5: links of both survive
def test_memory_removal_respected(self):                    # spec test 28
    b = C(b"- [A](a.md)\n- [B](b.md)\n"); l = C(b"- [A](a.md)\n")             # local removed B
    r = C(b"- [A](a.md)\n- [B](b.md)\n- [N](n.md)\n")                        # remote added N
    out = merge_content("memory/p/MEMORY.md", l, b, r, False)
    self.assertEqual(links(out.data), {"a.md", "n.md"})
def test_json_invalid_merge_rejected(self): ...      # spec test 6: a clean text merge yielding invalid JSON returns None
def test_binary_never_merged(self): ...              # rel "skills/s/img.png": None
def test_merge_exec_rules(self):
    self.assertTrue(merge_exec(False, False, True, False))   # local unchanged: take remote
    self.assertTrue(merge_exec(True, False, False, False))   # local changed: keep local
    self.assertFalse(merge_exec(False, None, True, False))   # no base: local
    self.assertTrue(merge_exec(False, None, True, True))     # no base, fresh: remote
```
`tests/test_decide.py`: one test per rule above, named `test_rule1_equal`, `test_remote_empty_never_applied` (Review Focus 1), `test_unchanged_local_takes_remote`, `test_unchanged_local_trashed_on_remote_delete`, `test_remote_edit_beats_local_delete`, `test_added_remote_only`, `test_local_edit_beats_remote_delete`, `test_merge_clean`, `test_merge_conflict_keep_both`, `test_both_added_conflict_fresh_takes_remote`, `test_both_added_conflict_migration_keeps_local`, `test_exec_only_change_takes_remote_bit` (`L == B` bytes and bit, R differs only in bit: WRITE with `R.exec`).

- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_merge tests.test_decide -v`.
- [ ] **Step 3: Implement `merge.py` and `decide.py`.** A merged result's exec bit is `merge_exec(L.exec, B.exec if B else None, R.exec, fresh)`.
- [ ] **Step 4: Run to verify they pass** under both interpreters.
- [ ] **Step 5: Commit**: `git commit -m "feat: add merge rules and per-path decision table"`

---

### Task 5: Lock, run context, start checks

**Files:**
- Create: `claude_sync/lock.py`, `claude_sync/context.py`, `claude_sync/checks.py`
- Test: `tests/test_lock.py`, `tests/test_checks.py`

**Interfaces:**
- Consumes: `Config`, `Log`, `Git`, `GitError`, `fsops.is_ciphertext`.
- Produces (`lock.py`): `acquire(repo: Path, wait: float, log: Log) -> Optional[int]`: opens `repo/.sync.lock` (create) and polls `fcntl.flock(fd, LOCK_EX | LOCK_NB)` every 0.2 s until `wait` elapses; while `repo/.sync.lock.d` exists with mtime younger than 900 s the lock counts as busy (an old-code run may hold it); one older is removed with WARN `removing stale legacy lock dir`. Returns the fd (kept open for the process lifetime) or None after logging INFO `lock busy; skipping`.
- Produces (`context.py`):
  - `Report` (dataclass, all ints 0 by default): `remote_changes, written, merged, kept_both, trashed, local_wins, pushed, errors`; plus `conflicts_dir: Optional[Path] = None`, `first_error: Optional[str] = None`, `skipped_symlinks: int = 0`. Methods `summary(command: str, seconds: float) -> str` giving exactly `"{command} done in {seconds:.1f}s: {remote_changes} remote changes, {written} written, {merged} merged, {kept_both} kept-both, {trashed} trashed"` (push appends `, {pushed} pushed`), and `notice(home: Path) -> Optional[str]`: first match of: errors gives `claude-sync: ERROR {first_error} (see ~/.claude-sync/last-sync.log)`; kept_both gives `claude-sync: {n} conflicts kept both, remote copies in {conflicts_dir with home shown as ~} (see last-sync.log)`; local_wins gives `claude-sync: {n} local edits kept over remote deletes (see last-sync.log)`; else None.
  - `Ctx` (dataclass): `cfg: Config, log: Log, git: Git, report: Report, stamp: str` (UTC `%Y-%m-%dT%H-%M-%SZ` at start); `checkpoint(name: str) -> None` (the `CLAUDE_SYNC_TEST_HOLD` rule in Decisions); `conflicts_root` and `trash_root` properties (`repo/conflicts/<stamp>`, `repo/trash/<stamp>`).
  - `BASE_REF = "refs/claude-sync/base"`, `PENDING_REF = "refs/claude-sync/pending"`.
- Produces (`checks.py`):
  - `class StopRun(Exception)` (message logged as ERROR by the caller, exit 1).
  - `prepare(ctx: Ctx) -> None`: runs, in order: (1) remove `.git/index.lock`, `.git/HEAD.lock`, `.git/packed-refs.lock` and any `*.lock` under `.git/refs` older than 60 s, WARN each; (2) dead rebase: for `.git/rebase-merge` and `.git/rebase-apply`, store a non-empty `autostash` with `git stash store -m "autostash of a killed sync run" <sha>`, remove the directory, WARN; (3) attach HEAD (Decisions); (4) `dirty_outside_data()` non-empty raises `StopRun("tracked files outside data/ are modified: ...")`; (5) `ensure_unlocked(ctx)`; (6) `restore_invariant(ctx)`.
  - `ensure_unlocked(ctx: Ctx) -> None`: when `.gitattributes` contains `filter=git-crypt` and `.git/git-crypt/keys/default` is missing: if `cfg.key` exists run `git-crypt unlock <key>` (cwd repo, output captured and never logged beyond the first stderr line, 60 s timeout); still locked or no key raises `StopRun("repo is git-crypt locked and no usable key at <path>")`.
  - `restore_invariant(ctx: Ctx) -> None`: `base = git.rev(BASE_REF)`; target is `base` or `HEAD`; when `git.rev("HEAD") != target` or `git.data_dirty()`: `reset_hard(target)` then `clean_data()`, INFO `restored data/ to <short sha>`.

- [ ] **Step 1: Write the failing tests**

`tests/test_lock.py`:
```python
def test_second_acquire_waits_then_skips(self):
    fd = acquire(repo, 0, log); self.assertIsNotNone(fd)
    t = time.monotonic(); r = subprocess.run([sys.executable, "-c", HOLDER_CHECK, str(repo), "0.5"]); # child tries acquire(wait=0.5)
    self.assertEqual(r.returncode, 3); self.assertGreaterEqual(time.monotonic() - t, 0.5)   # child exits 3 on None
def test_released_on_sigkill(self): ...          # child takes lock, sleeps; os.kill(SIGKILL); acquire(wait=2) succeeds
def test_legacy_dir_young_busy_old_removed(self): ...   # spec test 19, unit half (os.utime 1000 s ago)
```
`tests/test_checks.py` (machines from the harness, `prepare` called in-process with a `Ctx` built over the machine's repo):
```python
def test_stale_index_lock_removed(self): ...      # .git/index.lock aged 120 s: removed; aged 5 s: kept
def test_dead_rebase_removed_and_autostash_kept(self): ...  # stash list gains "autostash of a killed sync run"
def test_detached_head_reattached(self): ...
def test_dirty_bin_stops(self):                    # spec test 20, second half
    (m.repo / "bin/README").write_text("hand edit\n")
    with self.assertRaises(StopRun): prepare(ctx)
def test_restore_removes_untracked_and_tmp(self):  # spec test 26, unit half
    (m.repo / "data/stray.md").write_text("x"); (m.repo / "data/CLAUDE.md.sync-tmp.99").write_text("y")
    prepare(ctx)
    self.assertFalse((m.repo / "data/stray.md").exists()); self.assertEqual(m.git("status", "--porcelain"), "")
def test_restore_resets_to_base(self): ...         # HEAD on a commit after base: HEAD back to base
def test_locked_repo_without_key_stops(self): ...  # fake: .gitattributes with filter=git-crypt committed, no keys dir, no key file: StopRun
def test_report_notice_and_summary(self): ...      # exact strings above; notice never longer than 200 (Log truncates)
```
- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_lock tests.test_checks -v`.
- [ ] **Step 3: Implement `lock.py`, `context.py`, `checks.py`.**
- [ ] **Step 4: Run to verify they pass** under both interpreters.
- [ ] **Step 5: Commit**: `git commit -m "feat: add flock lock, run context and start checks"`

---

### Task 6: Shim, CLI dispatch and pull (normal three-way case)

**Files:**
- Create: `claude_sync/__main__.py`, `claude_sync/pull.py`, `claude_sync/cli.py`
- Replace: `bin/claude-sync` (the bash tool becomes the shim)
- Test: `tests/test_shim.py`, `tests/test_pull.py`

**Interfaces:**
- Consumes: everything from Tasks 1 to 5.
- Produces (`pull.py`):
  - `fetch_origin(ctx: Ctx) -> Optional[str]`: `git.fetch`; `"offline"` returns None (WARN already logged); `"error"` raises `StopRun`; `"ok"` resolves pending (spec 6.4: if `PENDING_REF` exists and is an ancestor of `origin/<b>`, `update_ref(BASE_REF, pending)` then `restore_invariant`; delete `PENDING_REF` either way) and returns the `origin/<b>` sha.
  - `pull_steps(ctx: Ctx, origin: str) -> None`: spec 6.7 steps 4 to 9 (this task covers bases that exist and are ancestors; Task 7 adds the rest). Raises `StopRun` to stop.
  - `run_pull(ctx: Ctx) -> int`: `prepare`, `fetch_origin` (None gives 0), `origin == base` gives 0, else `pull_steps`, return 0.
- Produces (`cli.py`): `main(argv: List[str], env: Optional[Mapping[str, str]] = None) -> int`: usage error gives 2 with `usage: claude-sync {pull|push|status|dry-run}` on stderr; `CLAUDE_SYNC_RECONCILING=1` gives 0 (recursion guard kept from the old tool); builds `Config`, `Log` (`repo/last-sync.log`), lock with the command's wait, `Git`, `Ctx`; runs the command; `StopRun` or `GitError` logs ERROR with its text (no traceback), sets `report.errors` and `first_error`, and returns 1; any other exception logs `exception()` (traceback) and returns 1; for pull and push always logs the summary line, and prints `report.notice()` once. `push`, `status` and `dry-run` dispatch to stubs that Tasks 8 and 9 replace (`status` prints `not implemented` and returns 0 until then).
- `bin/claude-sync` (bash): the old PATH loop verbatim (lines 21 to 24 of the old script); `ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)`; `export PYTHONDONTWRITEBYTECODE=1`; candidates: `$CLAUDE_SYNC_PYTHON` alone when set, else `/etc/profiles/per-user/$(id -un)/bin/python3`, `$HOME/.nix-profile/bin/python3`, `/run/current-system/sw/bin/python3`, `/opt/homebrew/bin/python3`, then `/usr/bin/python3` only if `xcode-select -p >/dev/null 2>&1`; first that is executable and passes `-c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'` runs `PYTHONPATH="$ROOT" exec "$py" -s -m claude_sync "$@"`. None found: append `$(date -u +%Y-%m-%dT%H:%M:%SZ) pid=$$ ${1:-?} ERROR no usable python3 (3.9 or newer) found` to `${CLAUDE_SYNC_REPO:-$HOME/.claude-sync}/last-sync.log`, print `claude-sync: ERROR no usable python3 found (see ~/.claude-sync/last-sync.log)`, exit 0.

Pull loop details (`pull_steps`), in this order per run:
1. `M`: base if `is_ancestor(base, origin)`, else `merge_base(base, origin)` with INFO `base is not an ancestor of origin`; None raises `StopRun`.
2. `changes = git.diff_raw(M, origin)`; `report.remote_changes = len(changes)`.
3. Content reader: `head = git.tree_blobs("HEAD")`; a blob is read from `repo/data/<p>` when `head.get(p) == (mode, blob)`, else collected per rev and fetched with one `git.checkout_to(rev, paths, tmp)` into a `tempfile.mkdtemp()` (removed in `finally`). Exec bit comes from the mode (`100755`). Any `B` or `R` with `is_ciphertext` raises `StopRun("ciphertext read from git for <path>")`.
4. For each change, deletions first (then sorted by path): skip with `report.skipped_symlinks += 1` when `via_symlink`; `L` = `read_content(local)` canonicalised when `is_text`; record `stat_key`; `outcome = decide(...)`; `ctx.checkpoint("pull:before-replace")`; re-stat; if the key moved, re-read and decide again (at most 3 times, then `Act.KEEP_BOTH`); apply: WRITE `atomic_write(local, decanon content)` (`report.written`, plus `report.merged` when `merged`); TRASH `move_aside(local, ctx.trash_root, local_rel)`; KEEP_LOCAL WARN and `report.local_wins += 1`; KEEP_BOTH `put_aside(ctx.conflicts_root, local_rel, decanon R)`, WARN `kept both: <local_rel>`, `report.kept_both += 1`, `report.conflicts_dir = ctx.conflicts_root`.
5. `ctx.checkpoint("pull:before-reset")`; `git.reset_hard(origin)`; `ctx.checkpoint("pull:before-base")`; `git.update_ref(BASE_REF, origin)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_shim.py`:
```python
def test_runs_package_with_given_python(self): ...   # m.run("status") exit 0 with CLAUDE_SYNC_PYTHON=sys.executable
def test_no_python_logs_and_exits_zero(self):
    r = m.run("pull", env=m.env(CLAUDE_SYNC_PYTHON="/nonexistent/python3"))
    self.assertIn("claude-sync: ERROR no usable python3 found", r.stdout)
    self.assertRegex(m.log_lines()[-1], r"Z pid=\d+ pull ERROR no usable python3")
def test_bad_usage_exit_2(self): ...
```
Add to `tests/harness.py`: `SyncTestCase.synced(*names: str, user: str = "alice") -> List[Machine]` (makes the machines and runs `pull` once on each, so each has a base; used by Tasks 6 to 11).

`tests/test_pull.py`:
```python
def test_dod_local_edit_survives_pull_from_other_session(self):        # spec test 1
    a, = self.synced("a")
    a.write("CLAUDE.md", "# shared\nlocal edit\n")
    self.remote_commit({"memory/-Users-__USER__-p/x.md": "unrelated\n"})   # remote moves, CLAUDE.md untouched
    a.run("pull")                                                           # a separate process = another session
    self.assertEqual(a.read("CLAUDE.md"), "# shared\nlocal edit\n")
    a.run("pull")                                                           # and again with nothing new
    self.assertEqual(a.read("CLAUDE.md"), "# shared\nlocal edit\n")
def test_remote_change_arrives_edit_stays(self): ...                    # spec test 2
def test_clean_merge_both_edits_kept(self): ...                         # spec test 3, base/local/remote as in test_merge
def test_conflict_kept_both_with_notice(self):                          # spec test 4
    r = a.run("pull")
    self.assertEqual(a.read(rel), local_text)
    self.assertEqual([p.name for p in a.conflicts()], ["x.md"])
    self.assertRegex(r.stdout, r"^claude-sync: 1 conflicts kept both, remote copies in ~/\.claude-sync/conflicts/")
    self.assertTrue(any(" WARN kept both: " in l for l in a.log_lines()))
def test_memory_index_union(self): ...                                  # spec test 5
def test_settings_json_never_invalid(self): ...                         # spec test 6: result parses or is kept-both
def test_remote_delete(self): ...                                       # spec test 7: unchanged file in trash/, edited file kept + WARN
def test_two_users_rehydrated(self): ...                                # spec test 12: alice writes via remote_commit canonical; bob gets /Users/bob/ and -Users-bob- dir
def test_self_update_updates_work_tree(self): ...                       # spec test 20: remote_commit bin/README change (outside data/) reaches a.repo/bin/README
def test_claude_config_dir_ignored(self): ...                           # spec test 21
def test_base_moves_and_invariant_holds(self): ...                      # after pull: base == origin/main == HEAD, data/ clean
def test_pull_keeps_local_when_remote_emptied(self): ...                # Review Focus 1
def test_odd_names_round_trip(self): ...                                # Review Focus 3, pull half
def test_edit_during_pull_window_survives(self):                       # Review Focus 5
    p = a.popen("pull", env=a.env(CLAUDE_SYNC_TEST_HOLD="pull:before-replace"))
    wait_for(a.repo / ".hold-pull:before-replace")
    a.write(rel, "edited during pull\n"); os.utime(...)   # bump mtime by 1 s to defeat same-tick mtime
    (a.repo / ".hold-pull:before-replace").unlink(); p.wait(60)
    self.assertEqual(a.read(rel), "edited during pull\n")            # or merged containing it; never the remote bytes alone
```
- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_shim tests.test_pull -v`.
- [ ] **Step 3: Implement the shim, `__main__.py`, `cli.py` and `pull.py`.** `__main__.py` is `from claude_sync.cli import main` plus `sys.exit(main(sys.argv[1:]))`. `chmod 755 bin/claude-sync`.
- [ ] **Step 4: Run to verify they pass**, then the whole suite under both interpreters.
- [ ] **Step 5: Commit**: `git commit -m "feat: replace bash claude-sync with python shim and three-way pull"`

---

### Task 7: Pull edge cases

**Files:**
- Modify: `claude_sync/pull.py`
- Test: `tests/test_pull_edge.py`

**Interfaces:**
- Consumes: Task 6 `pull_steps`, `fetch_origin`, `run_pull`.
- Produces: the same signatures, now covering: no base (M is `git.empty_tree()`, `fresh = (repo/.fresh-machine).exists()`; after the update-ref remove the marker), the remote mass delete guard (count `D` changes against kept files in `origin`'s tree; trip before any read or write raises `StopRun("remote deletes N files and keeps K; run CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync pull if intended")` unless `cfg.allow_prune`), per-path type clash (`IsADirectoryError` or `NotADirectoryError` from the apply turns into KEEP_BOTH; an `OSError` from `put_aside` itself, or any other `OSError`, raises `StopRun("cannot write <local_rel>: <strerror>")`), TAKE_REMOTE (put L aside into `conflicts_root`, then write R; counts as kept_both for the notice), skipped symlink paths.

- [ ] **Step 1: Write the failing tests**
```python
def test_new_machine_marker_takes_remote(self):           # spec test 10, pull half
    self.remote_commit({"settings.json": '{"model": "x", "hooks": {}}\n'})
    b = self.machine("b"); b.write("settings.json", '{"hooks": {"a": 1}}\n'); b.write("projects/-Users-alice-p/memory/only.md", "local only\n")
    (b.repo / ".fresh-machine").touch()
    b.run("pull")
    self.assertEqual(json.loads(b.read("settings.json"))["model"], "x")
    self.assertIn("settings.json", [str(p) for p in b.conflicts()])
    self.assertEqual(b.read("projects/-Users-alice-p/memory/only.md"), "local only\n")
    self.assertFalse((b.repo / ".fresh-machine").exists()); self.assertIsNotNone(b.base())
def test_marker_survives_failed_first_pull(self): ...      # origin pointed at a missing path: ERROR, marker still there, base None; fix origin, next plain pull takes remote
def test_migration_keeps_local_parks_remote(self):         # spec test 11
    # machine cloned at commit c1; ~/.claude holds c1 content plus a local edit to y.md;
    # remote_commit changes x.md (c2); `git reset --hard origin/main` in b.repo after fetch (HEAD at c2, never applied); no base ref
    b.run("pull")
    self.assertEqual(b.read(y_rel), "local edit\n")
    self.assertEqual(b.read(x_rel), x_c1_text)                       # local kept in place
    self.assertIn(x_local_rel, [str(p) for p in b.conflicts()])      # remote copy parked
    self.assertEqual(b.base(), b.git("rev-parse", "origin/main"))
def test_remote_mass_delete_stops_everything(self):        # spec test 9, remote half
    # remote_commit deleting 25 of 30 files
    r = a.run("pull", expect=1)
    self.assertEqual(a.base(), base_before); self.assertTrue(all(a.read(p) for p in rels))
    self.assertIn("claude-sync: ERROR remote deletes 25 files", r.stdout)
    a.run("pull", env=a.env(CLAUDE_SYNC_ALLOW_PRUNE="1"))
    self.assertEqual(len(a.trash()), 25)
def test_type_clash_kept_both_and_run_continues(self): ... # spec test 24 first half: remote adds file skills/foo, local has dir skills/foo/; other change applied; base advanced; next pull exit 0
def test_other_write_error_stops_before_base(self):        # spec test 24 second half
    os.chmod(target_dir, 0o555); r = a.run("pull", expect=1); os.chmod(target_dir, 0o755)
    self.assertEqual(a.base(), base_before); self.assertEqual(a.conflicts(), [])
    a.run("pull"); self.assertEqual(a.read(rel), remote_text)
def test_pending_landed_moves_base(self):                  # spec test 27, refs variant
    # a pushes a commit by hand (git push) built on base, sets refs/claude-sync/pending to it, leaves base
    r = a.run("pull")
    self.assertEqual(a.base(), pushed_sha); self.assertEqual(a.conflicts(), []); self.assertEqual(r.stdout, "")
    self.assertIsNone(a.ref("refs/claude-sync/pending"))
def test_exec_bit_chmod_only_pulled(self): ...             # spec test 25, pull half
def test_both_added_bit_difference_no_base(self): ...      # keeps local bit; with marker, remote bit
def test_pull_never_writes_through_symlinked_dir(self):    # Review Focus 2
    # local skills/linked is a symlink to an outside dir; remote_commit adds skills/linked/SKILL.md
    a.run("pull")
    self.assertFalse((outside / "SKILL.md").exists())
def test_rewritten_remote_history(self): ...               # remote force-reset to an older commit plus a new one: INFO line, merge-base used, exit 0
```
- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_pull_edge -v`.
- [ ] **Step 3: Implement in `pull.py`.**
- [ ] **Step 4: Run to verify they pass**, then the whole suite under both interpreters.
- [ ] **Step 5: Commit**: `git commit -m "feat: handle first pull, mass delete, per-path errors and pending pushes"`

---

### Task 8: Push

**Files:**
- Create: `claude_sync/push.py`
- Modify: `claude_sync/cli.py` (dispatch `push`)
- Test: `tests/test_push.py`

**Interfaces:**
- Consumes: `scan_local`, `via_symlink`, `canon`, `decanon`, `fsops`, `Git`, `prepare`, `restore_invariant`, `fetch_origin`, `pull_steps`, `Ctx`.
- Produces:
  - `Scan` (dataclass): `changed: Dict[str, Content]` (repo_rel to new canonical content), `deleted: List[str]`, `local: Dict[str, Path]`.
  - `scan(ctx: Ctx) -> Scan`: `scan_local`; compare each canonical local content and exec bit with `repo/data/<rel>` (read in process, exec from `st_mode & 0o100`); a data file is deleted only when it is absent from `local`, not under a skipped symlink prefix, and `os.path.lexists(local path)` is False.
  - `run_push(ctx: Ctx) -> int`, spec 6.10 steps 1 to 9 exactly, with: no base runs `fetch_origin` plus `pull_steps` first (None or `StopRun` stops without pushing; offline is INFO and 0); empty-file guard (age from `st_mtime`, 600 s; restore writes `decanon(base bytes)` after `move_aside` of the empty file into `trash_root`); local mass delete guard message `refusing to push: would delete N files and keep only K; CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync push if intended`; data writes via `atomic_write` under `repo/data`, deletions with `unlink` and removal of empty parent dirs up to `data/`; `ctx.checkpoint("push:before-commit")`; commit message `sync: {me}@{host} {UTC %Y-%m-%dT%H:%M:%SZ}` where host is `scutil --get LocalHostName` stdout, falling back to `socket.gethostname().split(".")[0]`; `update_ref(PENDING_REF, HEAD)`; `ctx.checkpoint("push:before-push")`; push; `ctx.checkpoint("push:after-push")`; on `"rejected"`: `restore_invariant`, `delete_ref(PENDING_REF)`, back to the fetch, at most 3 attempts then `StopRun("push rejected 3 times")`; on `"ok"`: `update_ref(BASE_REF, HEAD)`, `delete_ref(PENDING_REF)`, `report.pushed = len(changed) + len(deleted)`.

- [ ] **Step 1: Write the failing tests**
```python
def test_dod_two_machines(self):                         # spec test 1 end to end
    a, b = self.synced("a", "b")
    a.write(rel, "edit on a\n"); a.run("push")
    b.write(other_rel, "edit on b\n"); b.run("pull")
    self.assertEqual(b.read(rel), "edit on a\n"); self.assertEqual(b.read(other_rel), "edit on b\n")
def test_noop_push_touches_no_network(self):
    os.rename(self.root / "remote.git", self.root / "gone.git")
    r = a.run("push"); self.assertEqual(r.returncode, 0)
    self.assertFalse(any(" WARN " in l or " ERROR " in l for l in a.log_lines()))
def test_local_delete_trashed_on_other_machine(self): ...   # spec test 8
def test_local_mass_delete_refused_then_allowed(self): ...  # spec test 9, local half; exit 1 then override
def test_push_without_base_pulls_first(self): ...           # spec test 10, push half: no base, offline remote: nothing pushed, exit 0
def test_empty_file_guard(self): ...                         # spec test 17: young 0-byte left alone and not pushed; old (os.utime 11 min) restored, empty in trash
def test_exec_bit_pushed(self): ...                          # spec test 25, push half
def test_leftovers_never_committed(self): ...                # spec test 26: stray data/ file and *.sync-tmp.* absent from origin tree after push
def test_rejected_push_keeps_local_edit(self):               # spec test 30
    p = a.popen("push", env=a.env(CLAUDE_SYNC_TEST_HOLD="push:before-push"))
    wait_for(hold); b.write(rel, "edit on b\n"); b.run("push"); hold.unlink(); self.assertEqual(p.wait(60), 0)
    out = a.read(rel)
    self.assertTrue(out == merged_expected or a.conflicts())   # merge or keep-both; never the remote bytes alone
    self.assertNotEqual(out, "edit on b\n")
def test_rejected_push_different_files_both_land(self): ...   # spec test 30 variant
def test_push_never_deletes_symlinked_paths(self): ...       # Review Focus 2: remote has skills/linked/SKILL.md; local skills/linked is a symlink: still in origin tree after push
def test_odd_names_round_trip_push(self): ...                # Review Focus 3, push half
```
- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_push -v`.
- [ ] **Step 3: Implement `push.py` and wire `cli.py`.**
- [ ] **Step 4: Run to verify they pass**, then the whole suite under both interpreters.
- [ ] **Step 5: Commit**: `git commit -m "feat: add push with guards, pending ref and safe retry"`

---

### Task 9: status, dry-run, retention, error reporting, README

**Files:**
- Modify: `claude_sync/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `scan`, `Report`, `prune_older_than`, `Git`.
- Produces:
  - `status`: no lock, no network. Prints lines, in this order: `base: <sha or none>`, `head: <sha>`, `origin: <sha of origin/<b> or none>`, `pending: yes|no`, `fresh-machine marker: yes|no`, `local changes: <n changed> changed, <n deleted> deleted`, `conflict folders: <n>`, `last: <last summary line from the log or none>`.
  - `dry-run` / `dry`: lock (20 s), no `prepare` writes: if `HEAD != base` or `data_dirty()` print `note: repo needs a restore; the next pull or push will do it`; then `A <rel>`, `M <rel>`, `D <rel>` lines sorted by path; exit 0.
  - Retention: after every locked pull or push (success or not), `prune_older_than(repo/"conflicts", 30, now)` and the same for `trash`; INFO per removed folder.
  - README: replace the Usage, New machine and Automation sections to match the spec (three-way sync, `conflicts/`, `trash/`, the notice, `touch ~/.claude-sync/.fresh-machine` before the first pull on a new Mac, hooks as in spec section 7). No em dashes or arrows; keep the allowlist section.

- [ ] **Step 1: Write the failing tests**
```python
def test_status_fields(self): ...
def test_dry_run_lists_and_writes_nothing(self): ...     # A/M/D lines; git rev-parse HEAD and data/ unchanged afterwards
def test_broken_remote_is_error_not_offline(self):       # spec test 16
    a.git("remote", "set-url", "origin", str(self.root / "missing.git"))
    r = a.run("pull", expect=1)
    self.assertTrue(any(" ERROR " in l and "missing.git" in l for l in a.log_lines()))
    self.assertFalse(any("offline" in l for l in a.log_lines()))
def test_every_log_line_timestamped(self): ...           # after a pull, a push and a failure: all lines match the Global format
def test_uncaught_exception_logged_with_traceback(self): ...  # ~/.claude/CLAUDE.md chmod 000, then push: exit 1, a log line containing "Traceback", one stdout notice line; chmod back in tearDown
def test_git_error_logged_without_traceback(self): ...       # the broken-remote case above: ERROR line holds the git command and stderr, no "Traceback"
def test_retention(self): ...                             # spec test 23: aged folders removed by the next pull, fresh kept
def test_legacy_lock_dir(self): ...                       # spec test 19 integration: young .sync.lock.d makes pull exit 0 with "lock busy" after CLAUDE_SYNC_LOCK_WAIT=1
```
- [ ] **Step 2: Run to verify they fail**: `python3 -m unittest tests.test_cli -v`.
- [ ] **Step 3: Implement in `cli.py`; rewrite the README sections.**
- [ ] **Step 4: Run to verify they pass**, then the whole suite under both interpreters, then `python3 -c "import re,sys; sys.exit(bool(re.search(r'\u2014|\u2013|\u2192|\u2190|-[>]|=[>]', open('README.md').read())))"` (expected: exit 0).
- [ ] **Step 5: Commit**: `git commit -m "feat: add status, dry-run, retention and README for the rewrite"`

---

### Task 10: Concurrency and kill safety

**Files:**
- Test: `tests/test_stress.py`
- Modify: whatever the failures point at (each fix gets its own `fix:` commit)

**Interfaces:**
- Consumes: the built tool through the harness only.

- [ ] **Step 1: Write the tests**
```python
def test_concurrent_sessions(self):                       # spec test 13
    a, b = self.synced("a", "b")
    editor = a.popen_editor(rels, rounds=40)               # add to harness: Machine.popen_editor(rels, rounds) starts a python child that appends "round N" to each rel with 50 ms sleeps
    procs = [a.popen(cmd, env=a.env(CLAUDE_SYNC_LOCK_WAIT="60")) for cmd in ["pull", "push"] * 4]
    b_procs = [b.popen("push") for _ in range(2)]          # other machine keeps the remote moving
    for p in procs + b_procs: self.assertEqual(p.wait(300), 0)
    editor.wait(120); a.run("push"); b.run("pull")
    for rel in rels: self.assertEqual(b.read(rel), a.read(rel)); self.assertIn("round 39", a.read(rel))
    self.assertEqual(a.git("rev-parse", "HEAD"), a.base()); self.assertEqual(a.git("status", "--porcelain", "--", "data"), "")
    self.assertFalse((a.repo / ".sync.lock.d").exists())
def test_sigkill_at_each_point(self):                     # spec test 14 and 27 (kill variant)
    for point in ["pull:before-replace", "pull:before-reset", "pull:before-base",
                  "push:before-commit", "push:before-push", "push:after-push"]:
        with self.subTest(point=point):
            # set up a pending remote change and a pending local change, start with CLAUDE_SYNC_TEST_HOLD=point,
            # wait for the hold file, os.kill(p.pid, SIGKILL), remove the hold file, then run pull and push normally
            self.assertEqual(a.run("pull").returncode, 0); self.assertEqual(a.run("push").returncode, 0)
            self.assertEqual([p for p in (a.home / ".claude").rglob("*") if p.is_file() and p.stat().st_size == 0], [])
            b.run("pull"); self.assertEqual(b.read(rel_local), local_text)   # no edit lost
            self.assertEqual(a.read(rel_remote), remote_text)                  # remote change arrived
            if point == "push:after-push": self.assertEqual(a.conflicts(), [])   # landed push not seen as remote change
def test_pull_skips_when_lock_held(self): ...             # a run holding the lock via hold point; second pull with CLAUDE_SYNC_LOCK_WAIT=2 exits 0 in under 5 s with INFO "lock busy"
```
- [ ] **Step 2: Run**: `python3 -m unittest tests.test_stress -v`. Expected: PASS if Tasks 1 to 9 are right. Any failure: use superpowers:systematic-debugging, fix in the owning module with its own unit test first, commit as `fix: ...`.
- [ ] **Step 3: Run the whole suite** under both interpreters.
- [ ] **Step 4: Commit**: `git commit -m "test: add concurrency and kill-point tests"`

---

### Task 11: git-crypt round trip, locked repo, time budgets

**Files:**
- Test: `tests/test_gitcrypt.py`, `tests/test_timing.py`
- Modify: `tests/harness.py` (fixture generator)

**Interfaces:**
- Produces (`harness.py`): `make_fixture(self, memory_files: int = 750, skill_files: int = 600, total_bytes: int = 17 * 2**20, gitcrypt: bool = True) -> Optional[Path]`: rebuilds `remote.git` with deterministic content (`random.Random(772)`, text lines, a few `MEMORY.md` indexes, some `hooks/*.sh` with exec bits) in one commit; returns the key path when `gitcrypt`. `remote_commit_n(n: int) -> str` (one commit changing `n` fixture memory files); `Machine.edit_n(n: int) -> None` (appends a line to `n` local memory files).

- [ ] **Step 1: Write the tests**
```python
# test_gitcrypt.py
def test_round_trip_two_machines(self):                    # spec test 18
    key = self.make_gitcrypt_remote()
    a = self.machine("a"); b = self.machine("b")           # clones stay locked; the tool unlocks with CLAUDE_SYNC_KEY
    env_a = a.env(CLAUDE_SYNC_KEY=str(key)); env_b = b.env(CLAUDE_SYNC_KEY=str(key))
    a.run("pull", env=env_a); b.run("pull", env=env_b)
    a.write(rel, "secret on a\n"); a.run("push", env=env_a); b.run("pull", env=env_b)
    self.assertEqual(b.read(rel), "secret on a\n")
    blob = subprocess.run(["git", "-C", str(self.root / "remote.git"), "cat-file", "blob", f"main:data/{repo_rel}"], ...).stdout
    self.assertTrue(blob.startswith(b"\x00GITCRYPT"))
def test_merge_reads_plaintext(self): ...                  # spec test 18: both sides edit one file in different places under git-crypt: clean merge, both edits present
def test_filter_missing_stops_before_write(self):          # spec test 18 last sentence
    # after setup, remove filter.git-crypt.* from a.repo/.git/config; remote change arrives
    r = a.run("pull", env=env_a, expect=1)
    self.assertIn("ciphertext", r.stdout + "".join(a.log_lines())); self.assertEqual(a.read(rel), before)
def test_locked_repo_without_key(self):                    # spec test 22
    a = self.machine("a"); r = a.run("pull", expect=1)     # CLAUDE_SYNC_KEY points at a missing file
    self.assertFalse((a.home / ".claude").exists() and any((a.home / ".claude").rglob("*")))
# test_timing.py: one fixture per class (setUpClass), git-crypt on; skipped when git-crypt is missing (reported as skipped)
def test_budgets(self):                                     # spec test 15 and 6.14
    t = timed(lambda: a.run("pull", env=env)); self.assertLess(t, 60)       # first pull, no base
    t = timed(lambda: a.run("push", env=env)); self.assertLess(t, 1)        # nothing changed
    t = timed(lambda: a.run("pull", env=env)); self.assertLess(t, 2)        # nothing new
    self.remote_commit_n(50); t = timed(lambda: a.run("pull", env=env)); self.assertLess(t, 5)
    a.edit_n(50); t = timed(lambda: a.run("push", env=env)); self.assertLess(t, 10)
    print(f"timings: {timings}")                                           # evidence for the report
```
- [ ] **Step 2: Run**: `python3 -m unittest tests.test_gitcrypt tests.test_timing -v`. Expected: PASS. A budget miss: profile (`python3 -X importtime`, `cProfile`), then consider the stat cache the spec allows (section 10), with its own unit test and `fix:` or `perf:` commit.
- [ ] **Step 3: Run the whole suite** under both interpreters and keep the output for the report.
- [ ] **Step 4: Commit**: `git commit -m "test: add git-crypt round trip and hook time budget tests"`

---

### Task 12: dotnix hooks module (separate worktree, never switched)

**Files (in a new worktree of `~/dotnix`, branch `dfroberg/TO-772-claude-sync-hooks`, path `/Users/dannyfroberg/dotnix-TO-772`):**
- Create: `home-manager/claude-sync-hooks-ensure.py` (the ensure script, moved out of the Nix string so it can be tested), `home-manager/claude-sync-hooks-ensure_test.py`
- Modify: `home-manager/claude-sync-hooks.nix` (lines 36 to 52 `pushIfDirty`, 106 to 151 `ensureScript`, 180 to 218 bootstrap)

**Interfaces:**
- `claude-sync-hooks-ensure.py`: `WANT` per event: SessionStart matcher `""`, command `GIT_TERMINAL_PROMPT=0 "$HOME/.claude-sync/bin/claude-sync" pull 2>>"$HOME/.claude-sync/last-sync.log" || true`, timeout 60; SessionEnd matcher `""`, command `: > "$HOME/.claude-sync/.dirty"; launchctl kickstart "gui/$(id -u)/org.nix-community.home.claude-sync-push" >/dev/null 2>&1 || true`, timeout 10; PostToolUse matcher `Write|Edit`, command `$HOME/.claude/hooks/claude-sync-mark-dirty.sh`, timeout 45. Targets: `~/.claude/settings.json` plus every `~/Library/Application Support/orca/claude-profiles/*/home/settings.json` that is a regular file (symlinks skipped). Per event: the hooks whose command contains `claude-sync` must be exactly one, equal to WANT in command, timeout and group matcher (missing matcher counts as `""`); otherwise remove them all (dropping groups left without hooks) and append one WANT group. Other hooks and keys are untouched. Unparseable JSON: print a message and leave that file alone (exit 1 at the end). Atomic write via `<file>.tmp` and `os.replace`, as today.

- [ ] **Step 1: Create the worktree**: `git -C ~/dotnix worktree add /Users/dannyfroberg/dotnix-TO-772 -b dfroberg/TO-772-claude-sync-hooks`. Expected: new branch from `main`, `~/dotnix` itself unchanged (`git -C ~/dotnix status --short` empty).
- [ ] **Step 2: Write the failing test** `claude-sync-hooks-ensure_test.py` (unittest; runs the script as a subprocess with `HOME` set to a temp dir):
```python
def test_live_shape_replaced(self): ...          # live ~/.claude shape (SessionStart pull >> log 180 s, SessionEnd kick 10 s, PostToolUse 45 s): SessionStart becomes WANT with timeout 60; others unchanged; non-claude-sync hooks kept
def test_old_nix_shape_replaced(self): ...       # SessionEnd "claude-sync push" 45 s replaced by the kick, timeout 10
def test_orca_profile_updated_symlink_skipped(self): ...   # two profile homes: regular file updated, symlinked settings.json untouched
def test_idempotent(self): ...                   # second run prints "already present" and leaves mtime unchanged
def test_bad_json_left_alone(self): ...
```
- [ ] **Step 3: Run it to fail**: `python3 -m unittest claude-sync-hooks-ensure_test -v` from `home-manager/` (module name via `python3 home-manager/claude-sync-hooks-ensure_test.py -v`). Expected: FAIL (script missing).
- [ ] **Step 4: Implement** the script; in the Nix file set `ensureScript = ./claude-sync-hooks-ensure.py;`; replace `pushIfDirty`'s body so it always runs `claude-sync push` (keep the PATH line and the `.dirty` restore on failure, drop the `ahead` gate); in the bootstrap, add `: > "${syncRepo}/.fresh-machine"` right after `git -C "${syncRepo}" reset --hard -q origin/main`. Update the comments that describe the old behaviour.
- [ ] **Step 5: Run the test to pass.**
- [ ] **Step 6: Commit in the dotnix worktree**: `git add home-manager && git commit -m "feat(claude-sync): keep hooks current in all profiles, push every tick, mark fresh machines"`
- [ ] **Step 7: Build, never switch**: find the host attribute with `nix eval --raw /Users/dannyfroberg/dotnix-TO-772#darwinConfigurations --apply 'c: builtins.concatStringsSep " " (builtins.attrNames c)'`, then `nix build --no-link /Users/dannyfroberg/dotnix-TO-772#darwinConfigurations.<host>.system`. Expected: exit 0. Do not run `darwin-rebuild switch`, `home-manager switch` or any activation script.

---

## Execution notes for the lead

- Builder runs (Sonnet, high effort, at most four tasks each, this plan's literal text in the brief): A = Tasks 1 to 3; B = Tasks 4 and 5; C = Tasks 6 and 7; D = Tasks 8 and 9; E = Tasks 10 and 11; F = Task 12.
- After each run: a checker (Haiku 5.5; Sonnet for C, D and E) on the run's commit range: diff scope against the tasks, both-interpreter suite output, commit authorship and messages, no writes outside the worktree.
- Final gate: one Fable review of the whole branch against the spec before the report to the owner.
