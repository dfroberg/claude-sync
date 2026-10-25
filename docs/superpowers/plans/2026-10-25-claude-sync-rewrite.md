# claude-sync rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bash `bin/claude-sync` with a three-way sync in Python that never overwrites a local edit without a merge or a kept copy, plus the matching dotnix hook changes.

**Architecture:** A bash shim picks a Python 3.9+ interpreter and runs the `claude_sync` package from the repo it lives in. Each machine keeps a local base ref; pull diffs base to `origin/main` and decides per path with a pure decision function and `git merge-file`; push scans `~/.claude` against the `data/` work tree and builds a linear commit on the remote tip. An `fcntl.flock` lock serialises runs.

**Tech Stack:** Python 3.9+ standard library only (`unittest`, `subprocess`, `fcntl`, `json`, `dataclasses`), git 2.54, git-crypt 0.8, bash for the shim, Nix for the hooks module.

**Spec:** `docs/superpowers/specs/2026-10-25-claude-sync-rewrite-design.md` (approved by the owner on 2026-10-25, revised after the grok-4.7 round at commit 1dd0d746). Every task implements the spec section it names. The section "Decisions this plan adds to the spec" overrides the spec only for the points it names; for any other disagreement between this plan and the spec, stop and report.

## Global Constraints

- Python 3.9 or newer, standard library only. No `match`, no `X | Y` unions evaluated at runtime (use `from __future__ import annotations`), no pip installs. Every test passes under `/usr/bin/python3` (3.9.6) and `/etc/profiles/per-user/dannyfroberg/bin/python3` (3.14).
- No function-level imports inside `claude_sync/`: `cli.py` imports every module at startup, so the self-update (`git reset --hard` mid-run) can never load a newer module into a running process.
- The command line stays `claude-sync pull|push|status|dry-run` (`dry` alias). Exit 0 for success, skip or offline; 1 for an error; 2 for bad usage.
- Log line format, exactly: `2026-10-25T16:31:02Z pid=1234 pull WARN <message>` (UTC, `Z`). Levels DEBUG (written only with `CLAUDE_SYNC_DEBUG=1`), INFO, WARN, ERROR. Rotation above 5 MB to `last-sync.log.1`.
- stdout notice: one plain-text line, at most 200 characters, only for kept-both conflicts, local-edit-beats-delete, ERROR, or no usable python.
- Lock waits: pull 20 s, push 60 s, dry-run 20 s; `CLAUDE_SYNC_LOCK_WAIT` overrides all; status takes no lock.
- Fetch timeout 20 s, push timeout 30 s, at most 3 push attempts. Mass delete threshold, one predicate for pull and push: more than 20 deletions AND more deletions than files left under `data/` in the destination tree (`origin`'s tree for pull, the tree about to be committed for push). Empty-file restore age: 10 minutes. Retention: 30 days.
- Every git command runs as `git -C <repo>` with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_PREFIX`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR` and `GIT_NAMESPACE` removed from its environment (spec 6.2), as an argument list, never a shell string.
- Offline is WARN everywhere and exits 0. Every ERROR stop prints the stdout notice before exit 1 (spec 6.7 step 10).
- Stamp for `conflicts/` and `trash/`: one per run, the run's start time in UTC as `%Y-%m-%dT%H-%M-%SZ`; parked paths are `<repo>/conflicts/<stamp>/<path relative to ~/.claude>` and `<repo>/trash/<stamp>/<path relative to ~/.claude>`.
- Allowlist and canonicalisation exactly as the old script (`bin/claude-sync` at commit 1af679a4): FILES `CLAUDE.md settings.json mcp_settings.json keybindings.json`; DIRS `skills agents commands hooks`; memory `projects/<proj>/memory/**` stored as `data/memory/<canonical proj>/**`; text extensions `.md .json .jsonl .txt .sh .bash .zsh .lua .toml .yaml .yml .cfg`; byte replacements `/Users/<me>/` to `/Users/__USER__/` and `-Users-<me>-` to `-Users-__USER__-`, reversed on the way out.
- Tests are hermetic: a temporary HOME, a temporary sync repo, and a local bare remote per test. No test touches the network, `~/.claude`, `~/.claude-sync`, `~/.config/git-crypt` or the real key. The harness unsets `XDG_CONFIG_HOME` (it is set on this Mac and makes git write the real `~/.config/git/config` even with a temporary HOME) and sets `GIT_CONFIG_GLOBAL` to a temporary file and `GIT_CONFIG_NOSYSTEM=1`.
- Never run `git-crypt unlock` in this worktree. Never push. Never write to `~/.claude`, `~/.claude-sync` or `~/dotnix` outside the dotnix worktree of Task 12.
- Commits: conventional (`feat:`, `test:`, `fix:`, `docs:`, `chore:`), author Danny Froberg, no AI attribution lines. No em dashes and no arrows in docs and commit messages.

## Decisions this plan adds to the spec

- `CLAUDE_SYNC_PYTHON`: when set, the shim uses that interpreter and no other. The harness sets it to `sys.executable`, so the suite really runs the tool under each Python; setting it to a missing path tests the no-python branch.
- `CLAUDE_SYNC_TEST_HOLD=<point>`: at a named checkpoint the tool creates `$REPO/.hold-<point>` and waits (up to 60 s) until the test deletes it. Tests use it to SIGKILL or to race at an exact point. Points: `pull:before-replace`, `pull:before-reset`, `pull:before-base`, `push:before-commit`, `push:before-push`, `push:after-push`. Each point holds at most once per run (a per-run set of fired points), so a loop over several paths waits only at the first. A no-op when the variable is unset.
- Content reads: `B` is the `data/` work tree (spec 6.7 step 7: `M` is always the base, and the invariant makes `data/` the base); with no base, `B` is absent. `R` is read from the `data/` work tree whenever `HEAD`'s `(mode, blob)` for that path equals `R`'s; only other blobs go through the temporary checkout. This keeps a first pull (about 1368 files, about 20 ms each through git-crypt, measured) mostly free of decryption, because the old code's `HEAD` usually already holds most remote blobs.
- git-crypt lock state: the repo uses git-crypt when `$REPO/.gitattributes` contains `filter=git-crypt`; it is unlocked only when `$REPO/.git/git-crypt/keys/default` exists AND `git config --get filter.git-crypt.smudge` and `filter.git-crypt.clean` are both set. Either missing counts as locked. This replaces the old `data/CLAUDE.md` sniff; the ciphertext guard stays.
- Plaintext guard (public repo): in a repo that uses git-crypt, push checks every staged added or modified blob under `data/` after `git add` and before the commit, reading the index blobs raw (`git cat-file --batch` without `--filters`, input `:data/<path>`, sizes are exact without filters). Any blob that does not start with `\0GITCRYPT` raises `StopRun("refusing to commit unencrypted data/<path>")` after `git reset -q` of the index. Without this, a missing clean filter commits plaintext to the public remote.
- Paths the tool cannot place: a repo path that is not one of the allowlisted shapes (FILES, DIRS, `memory/<p>/...`) is skipped by pull with one WARN line per run giving the count, and is never treated as a local delete by push. A repo path that fails `safe_rel` (spec 6.3) stops the run.
- HEAD must be attached to `refs/heads/<BRANCH>`. If it is detached or on another branch (a dead rebase leaves it detached), the start checks run `git update-ref refs/heads/<BRANCH> HEAD` and `git symbolic-ref HEAD refs/heads/<BRANCH>`, with a WARN, before the invariant restore. Neither touches the work tree, so hand edits under `bin/` still reach the dirty-outside-data check.
- The shim runs Python with `cd "$ROOT" && exec "$py" -E -s -B -m claude_sync "$@"`: `-m` puts the current directory first on `sys.path`, so from a hook's project directory a file such as `json.py` would shadow the standard library. `-E` ignores `PYTHONPATH`, `-B` replaces `PYTHONDONTWRITEBYTECODE` (which `-E` would ignore).
- Remote 0-byte version over a non-empty local file: never written; the local file is kept with a WARN (`remote empty file not applied`), so the next push restores the content on the remote. Same reasoning as the spec's push-side empty-file guard. This WARN does not count as a local win and produces no stdout notice.
## Review Focus

1. A remote 0-byte version of a file that is non-empty locally: the pull keeps the local bytes and logs WARN; the next push sends them back. Test in Task 4 (`test_remote_empty_never_applied`) and Task 6 (`test_pull_keeps_local_when_remote_emptied`).
2. Symlinked paths inside `~/.claude` (three skill folders are symlinks live today): pull never reads, writes or trashes through them and push never collects them or deletes their paths on the remote (spec 6.3, test 34). Tests in Task 2 (`test_scan_skips_symlinked_dirs`, `test_scan_skips_top_level_file_symlink`), Task 7 (`test_pull_never_writes_through_symlinked_dir`) and Task 8 (`test_push_never_deletes_symlinked_paths`).
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
claude_sync/paths.py         Content, allowlist, canonicalisation, scan  Task 2
claude_sync/fsops.py         atomic writes, trash, conflicts             Task 2
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
  - `config.load(env: Mapping[str, str]) -> Config`. `claude = home/.claude` always (ignores `CLAUDE_CONFIG_DIR`); `repo = CLAUDE_SYNC_REPO or home/.claude-sync`; `data = repo/"data"`; `me = CLAUDE_SYNC_USER or pwd login name`; `branch = CLAUDE_SYNC_BRANCH or "main"`; `key = CLAUDE_SYNC_KEY or home/.config/git-crypt/claude-sync.key`; `allow_prune = CLAUDE_SYNC_ALLOW_PRUNE == "1"`; `debug = CLAUDE_SYNC_DEBUG == "1"`; `lock_wait_override = float(CLAUDE_SYNC_LOCK_WAIT)` or None. `home` from `HOME`.
  - `log.Log(path: Path, command: str, debug: bool, out: TextIO = sys.stdout)` with `debug/info/warn/error(msg: str) -> None`, `exception(msg: str) -> None` (one log line per traceback line), `notice(msg: str) -> None` (folds newlines in `msg` to ` | `, then prints it truncated to 200 chars plus one newline to `out`; the only way anything reaches stdout, called at most once per run by the caller). Never raises on OSError. Newlines inside any log `msg` become ` | ` too. Timestamps come from `datetime.now(timezone.utc)`, never local time.
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
def test_notice_one_line(self):                  # notice("x\ny\nz") prints "x | y | z\n" (one line)
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
- `remote_commit(changes: Dict[str, Optional[Union[str, bytes, Tuple[bytes, bool]]]], message="other", root_relative: bool = False) -> str`: applies to a scratch clone `root/_other` (paths are data-relative, or relative to the repo root when `root_relative`, for `bin/README` or `.gitattributes`; `None` deletes; a tuple sets the exec bit), commits, pushes, returns the new sha.
- `remote_tip() -> str` (`git -C remote.git rev-parse main`) and `remote_paths() -> List[str]` (data-relative paths at the remote tip, from `ls-tree -r -z --name-only main -- data/`).
- Every git command the harness runs (`remote_commit`, `Machine.git`, seeding, cloning) uses `base_env(root)` plus the machine's `HOME`, never the parent environment.
- `make_gitcrypt_remote() -> Path`: skips the test (`self.skipTest`) when `shutil.which("git-crypt", path=env PATH)` is None; otherwise rebuilds `remote.git` from a seed with `git-crypt init`, `.gitattributes` `data/** filter=git-crypt diff=git-crypt`, exports the key to `root/test.key` and returns it.
`Machine`: `env(**extra) -> dict` (base_env plus `HOME`, `CLAUDE_SYNC_REPO`, `CLAUDE_SYNC_USER`, `CLAUDE_SYNC_KEY=root/<name>/no.key`, then `extra` last so it overrides any of them; asserts not real); `run(*args, env=None, timeout=120, expect=0, cwd=None) -> CompletedProcess` runs `[str(TOOL), *args]` with `env or self.env()` (text; cwd defaults to `root`; asserts the exit code when `expect` is not None); `popen(*args, env=None)` likewise; `write(rel, data, exec_bit=False) -> Path` and `read(rel) -> Optional[str]` relative to `home/.claude`; `git(*args) -> str` runs `git -C <repo> *args` with `self.env()`; `ref(name) -> Optional[str]` (`git rev-parse -q --verify <name>`, None when missing); `base() -> Optional[str]` (`ref("refs/claude-sync/base")`); `log_lines() -> List[str]`; `conflicts() -> List[str]` and `trash() -> List[str]` (every file under every stamp folder, as POSIX paths relative to its stamp folder, sorted; tests compare against paths like `"settings.json"` or `"skills/s/SKILL.md"`).
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
- Produces (`paths.py`, which imports nothing from the package):
  - `Content` (frozen dataclass): `data: bytes, exec: bool`. Defined here so `paths` and `fsops` do not import each other in a cycle; `fsops` imports it, and later tasks may import it from either module (`fsops.Content is paths.Content`).
  - constants `TOKEN = "__USER__"`, `FILES`, `DIRS`, `TEXT_EXT` (values in Global Constraints).
  - `is_text(rel: str) -> bool` (lower-cased suffix in `TEXT_EXT`); `is_tmp(name: str) -> bool` (`".sync-tmp." in name`).
  - `canon(data: bytes, me: str) -> bytes`, `decanon(data: bytes, me: str) -> bytes`.
  - `safe_rel(rel: str) -> bool`: False when `rel` is empty, absolute, or has an empty, `.` or `..` component (spec 6.3).
  - `under(root: Path, rel: str) -> Path`: `root/rel` after checking `safe_rel(rel)` and that `os.path.realpath` of the result's parent is `realpath(root)` or below it; raises `ValueError("unsafe path <rel>")` otherwise. Every write, park and trash target in `~/.claude`, `conflicts/`, `trash/` and `data/` is built with it.
  - `to_repo(local_rel: str, me: str) -> Optional[str]` and `to_local(repo_rel: str, me: str) -> Optional[str]`. `local_rel` is relative to `~/.claude`, `repo_rel` to `data/` (POSIX separators). For every project directory name `<p>`, `projects/<p>/memory/<rest>` maps to `memory/<canon(p)>/<rest>` and back with `decanon`; FILES and DIRS (`<dir>/<rest>`) map to themselves; anything else, and anything failing `safe_rel` on either side, is `None`.
  - `canon_content(rel: str, c: Content, me: str) -> Content` and `decanon_content(rel: str, c: Content, me: str) -> Content`: `Content(canon(c.data, me) if is_text(rel) else c.data, c.exec)`, and the reverse. Every read from `~/.claude` goes through the first and every write into `~/.claude` or `conflicts/` through the second; non-text bytes and the exec bit pass through untouched.
  - `scan_local(claude: Path, me: str) -> Tuple[Dict[str, Path], Set[str]]`: returns (`repo_rel` to absolute local path for every syncable regular file; set of `repo_rel` prefixes that are outside the sync because of a symlink, each ending in `/` for a directory, or the bare path for a file). FILES that are symlinks are reported in the second set, not followed (spec 6.3). Inside DIRS and memory trees: `os.walk(followlinks=False, onerror=<raise>)`, so a folder that cannot be listed raises its `OSError` (spec 6.10 step 3: never read as deletes); symlinked files and directories are reported in the second set and not descended; names matching `is_tmp` are skipped. A memory tree whose `projects/<p>` or `projects/<p>/memory` is itself a symlink counts as a symlinked directory.
  - `via_symlink(claude: Path, local_rel: str) -> bool`: True when any component from `~/.claude` down, including the file itself and including the four FILES, is a symlink (checked with `os.lstat` per component; a missing component ends the walk with False).
- Produces (`fsops.py`, imports `Content` and `under` from `paths`):
  - `class CiphertextError(Exception)`; `is_ciphertext(data: bytes) -> bool` (`data.startswith(b"\x00GITCRYPT")`).
  - `read_content(path: Path) -> Optional[Content]`: None when the path does not exist (`FileNotFoundError`, or `NotADirectoryError` because a parent is a file) or is a directory; any other `OSError` (permission denied, I/O error) propagates, because only a confirmed absent path may count as absent (spec 6.7 step 7). Callers never pass a symlinked path (they check `via_symlink` first). `stat_key(path: Path) -> Optional[Tuple[int, int]]` (`(st_size, st_mtime_ns)` from `os.lstat`, None when missing).
  - `atomic_write(path: Path, content: Content) -> None`: raises `ValueError("refusing to write through symlink")` if `path` is a symlink (defence in depth; spec 6.3); create parents with `mkdir(parents=True, exist_ok=True)` (a regular file in the way raises `NotADirectoryError` or `FileExistsError`, re-raised as `NotADirectoryError`); write `<name>.sync-tmp.<pid>` in the same directory, `fsync`, chmod 0o755 or 0o644 according to `content.exec` (preserving other bits of an existing file is not required), `os.replace`; a directory at `path` raises `IsADirectoryError`. Raises `CiphertextError` before writing anything when `is_ciphertext(content.data)`.
  - `put_aside(root: Path, rel: str, content: Content) -> Path`: writes into `under(root, rel)`, creating its parent directories; if that file exists, appends `.1`, `.2`, and so on. Returns the path.
  - `copy_aside(src: Path, root: Path, rel: str) -> Path`: copies `src` (bytes and mode, `shutil.copy2`) to `under(root, rel)` with the same parents and suffix rule; `src` stays in place. Used to save a local file before it is replaced (spec 6.9 fresh machine).
  - `move_aside(src: Path, root: Path, rel: str) -> Path`: moves (`os.replace`, falling back to `shutil.move`) into `under(root, rel)` with the same parents and suffix rule.
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
    self.assertEqual(to_repo("projects/other-proj/memory/a.md", "alice"), "memory/other-proj/a.md")
    self.assertIsNone(to_local("skills/../../.ssh/authorized_keys", "bob"))
    self.assertIsNone(to_local("memory/../x.md", "bob"))
def test_safe_rel_and_under(self):
    for bad in ["", "/etc/x", "a//b", "a/./b", "a/../b", ".."]:
        self.assertFalse(safe_rel(bad))
    with self.assertRaises(ValueError): under(root, "skills/../../x")
    self.assertEqual(under(root, "skills/s/a.md"), root / "skills/s/a.md")
def test_content_helpers_leave_binary_alone(self):
    raw = Content(b"/Users/alice/x", True)
    self.assertEqual(canon_content("a.png", raw, "alice"), raw)
    self.assertEqual(canon_content("a.md", raw, "alice"), Content(b"/Users/__USER__/x", True))
def test_is_text(self): ...                        # .md .JSON .sh True; .png .bin False
def test_scan_collects_allowlist_only(self): ...   # FILES, a skill, an agent, a memory file; skips plugins/, transcripts, *.sync-tmp.* files
def test_scan_skips_symlinked_dirs(self):          # Review Focus 2
    # skills/linked is a symlink to an outside dir holding SKILL.md; skills/real/SKILL.md regular
    files, skipped = scan_local(claude, "alice")
    self.assertIn("skills/real/SKILL.md", files)
    self.assertNotIn("skills/linked/SKILL.md", files)
    self.assertIn("skills/linked/", skipped)
def test_scan_skips_top_level_file_symlink(self): ...     # CLAUDE.md as a symlink to elsewhere/real.md: not in files, "CLAUDE.md" in skipped
def test_scan_unreadable_dir_raises(self): ...     # skills/locked chmod 000: scan_local raises PermissionError (chmod back in finally)
def test_via_symlink(self): ...                    # True under skills/linked/, False for skills/real/x, True for a CLAUDE.md symlink
```
`tests/test_fsops.py`:
```python
def test_atomic_write_sets_exec_and_leaves_no_tmp(self): ...
def test_atomic_write_refuses_symlink(self): ...   # path is a symlink to an outside file: ValueError, target unchanged
def test_read_content_missing_vs_unreadable(self): ...   # missing: None; a directory: None; chmod 000 file: PermissionError
def test_copy_aside_keeps_source(self): ...        # source still in place, copy under root/rel with parents created, mode kept
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
def test_put_aside_never_overwrites(self): ...      # nested rel "skills/s/SKILL.md" with no parents yet; second call returns path ending ".1"
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
    - `run(*args: str, timeout: Optional[float] = None, input: Optional[bytes] = None, env: Optional[Dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess` (bytes). Argv is `["git", "-C", str(repo), *args]`. Environment: start from `base_env` (or `os.environ`), remove `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_PREFIX`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`, then add `GIT_TERMINAL_PROMPT=0`, `LC_ALL=C`, `GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=10` (unless already set), then the per-call `env` (which is how `checkout_to` sets its own `GIT_INDEX_FILE` and `GIT_ATTR_SOURCE`). Starts git with `start_new_session=True`; on timeout kills the whole process group (`os.killpg(pid, SIGKILL)`, ignoring `ProcessLookupError`) so no ssh child outlives it, calls `clear_locks(0)` (spec 6.6 item 1: the killed child was the only git process allowed to hold them), then raises `subprocess.TimeoutExpired`. Raises `GitError` when `check` and rc != 0.
    - `clear_locks(min_age: float) -> List[str]`: removes `.git/index.lock`, `.git/HEAD.lock`, `.git/packed-refs.lock` and every `*.lock` under `.git/refs` whose mtime is at least `min_age` seconds old; returns the removed paths (callers log WARN per path).
    - Optional queries run with `check=False` and map a non-zero exit to None or False: `rev(ref: str) -> Optional[str]` (`rev-parse -q --verify <ref>^{commit}`), `is_ancestor(a: str, b: str) -> bool` (`merge-base --is-ancestor`, rc 0 True, rc 1 False, anything else raises `GitError`), `head_branch() -> Optional[str]` (`symbolic-ref -q --short HEAD`). Also `empty_tree() -> str` (`git hash-object -t tree /dev/null`), `update_ref(ref: str, sha: str) -> None`, `delete_ref(ref: str) -> None` (no error when missing). No `merge_base`: spec 6.7 step 4 no longer uses one.
    - `fetch(branch: str, timeout: float = 20) -> str`: runs `fetch -q origin +refs/heads/<b>:refs/remotes/origin/<b>`; returns `"ok"`, `"offline"` or `"error"` (logging WARN or ERROR with `GitError` text).
    - `push(branch: str, timeout: float = 30) -> str`: `push -q origin HEAD:<b>`; returns `"ok"`, `"rejected"` (stderr contains `[rejected]`, `non-fast-forward`, `fetch first` or `stale info`), `"offline"` or `"error"`.
    - `diff_raw(a: str, b: str) -> List[Change]`: `diff --raw -z --no-renames --no-abbrev <a> <b> -- data/` (without `--no-abbrev` the blob ids are abbreviated and cannot be compared with `tree_blobs`; found by the run A builder), paths returned without the `data/` prefix.
    - `tree_blobs(rev: str) -> Dict[str, Tuple[str, str]]`: `ls-tree -r -z <rev> -- data/`, data-relative path to `(mode, blob)`. Records are `<mode> SP <type> SP <object> TAB <path> NUL`.
    - `checkout_to(rev: str, paths: List[str], dest: Path) -> None`: the spec 6.7 step 7 command, verbatim: `GIT_ATTR_SOURCE=<rev> GIT_INDEX_FILE=<dest>/.index git -C <repo> --work-tree=<dest> checkout <rev> --pathspec-from-file=- --pathspec-file-nul`, input `:(literal)data/<p>\0` per path (literal, so a name starting with `:` is not pathspec magic). No-op for an empty list.
    - `reset_hard(rev: str) -> None`, `clean_data() -> None` (`clean -qfdx -- data`), `data_dirty() -> bool` (`status --porcelain -- data` non-empty), `dirty_outside_data() -> List[str]` (`status --porcelain --untracked-files=no -- . :(exclude)data`), `add_data() -> None` (`add -A -- data`), `staged_data() -> bool` (`diff --cached --quiet -- data`: rc 1 True, rc 0 False), `commit(message: str) -> str` (`commit -q -m <message>` as one argv element; returns new HEAD).

- [ ] **Step 1: Write the failing tests** (`tests/test_gitio.py`, a `SyncTestCase` subclass so git runs hermetically; construct `Git(m.repo, log, base_env=m.env())`, never patch `os.environ`).
```python
def test_classify(self):
    self.assertEqual(classify("ssh: Could not resolve hostname github.com: nodename nor servname", False), "offline")  # substring match
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
def test_inherited_git_env_ignored(self):             # spec 6.2, unit half of spec test 31
    other = self.root / "other"; subprocess.run(["git", "init", "-q", str(other)], env=m.env(), check=True)
    g2 = Git(m.repo, log, base_env=m.env(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE=str(other / ".git/index")))
    self.assertEqual(g2.rev("HEAD"), m.git("rev-parse", "HEAD"))
def test_optional_queries_do_not_raise(self): ...     # rev("refs/nope") is None; is_ancestor(newer, older) is False; head_branch() None when detached
def test_clear_locks_by_age(self): ...                # index.lock aged 120 s removed by clear_locks(60); aged 5 s kept; clear_locks(0) removes it
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
  - `class MergeError(Exception)`.
  - `merge_text(local: bytes, base: Optional[bytes], remote: bytes, union: bool) -> Optional[bytes]`: `git merge-file -p [--union] L B R` (argv list, run with the same cleaned environment as `Git.run`) on files in a `tempfile.mkdtemp()` directory (removed after); `base None` means an empty file. rc 0 returns stdout; rc 1 to 127 (a conflict count) returns None; any other rc, including 255 (git's own errors, verified for a missing input and a binary input) and a negative rc (signal), raises `MergeError` (spec 6.9: an ERROR that stops the run, never a keep-both).
  - `strict_json(data: bytes) -> bool`: True when `json.loads(data.decode("utf-8"), object_pairs_hook=<raise ValueError on a repeated key>)` succeeds; False on any decode or parse error.
  - `merge_exec(l: bool, b: Optional[bool], r: bool, fresh: bool) -> bool`: spec 6.8 bit rule.
  - `merge_content(rel: str, L: Content, B: Optional[Content], R: Content, fresh: bool) -> Optional[Content]`: None when not text (by `is_text(rel)`); `MEMORY.md` basename uses `union=True` and then requires every link in `(links(L) & links(R)) | (links(L) - links(B)) | (links(R) - links(B))` to be in the result (`links(B)` is empty when B is None); `.json` results must pass `strict_json`; any such failure returns None. `MergeError` propagates.
- Produces (`decide.py`):
  - `Act` (Enum): `NOTHING, WRITE, TRASH, KEEP_LOCAL, KEEP_BOTH, TAKE_REMOTE` (`TAKE_REMOTE`: write R, park L; only on a `.fresh-machine` first pull).
  - `Outcome` (frozen dataclass): `act: Act, content: Optional[Content] = None, warn: Optional[str] = None, merged: bool = False, local_win: bool = False`.
  - `decide(rel: str, L: Optional[Content], B: Optional[Content], R: Optional[Content], fresh: bool) -> Outcome`. Order of rules, first match wins:
    1. `L == R`: NOTHING.
    2. `R` is not None and `R.data == b""` and `L` is not None and `L.data != b""`: KEEP_LOCAL, warn `remote empty file not applied` (Decisions; not a local win).
    3. `B` is not None and `L == B`: `R` None gives TRASH; else WRITE R.
    4. `L` None and `B` not None: WRITE R, warn `remote edit beats local delete`.
    5. `L` None and `B` None: WRITE R.
    6. `R` None (so `L != B`): KEEP_LOCAL, warn `local edit beats remote delete` (the only local win: `Outcome.local_win = True`).
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
def test_memory_removal_respected(self):                    # spec test 28: separate hunks (verified with git merge-file --union)
    b = C(b"- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n- [D](d.md)\n")
    l = C(b"- [A](a.md)\n- [C](c.md)\n- [D](d.md)\n")                         # local removed B
    r = C(b"- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n- [D](d.md)\n- [N](n.md)\n")  # remote added N, two lines away
    out = merge_content("memory/p/MEMORY.md", l, b, r, False)
    self.assertEqual(links(out.data), {"a.md", "c.md", "d.md", "n.md"})
def test_json_invalid_merge_rejected(self): ...      # spec test 6: a clean text merge yielding invalid JSON returns None
def test_json_both_sides_keys_kept(self): ...        # spec test 6: B {"a":1,"b":2,"c":3} one key per line; L changes "a", R changes "c": result parses, a and c both changed
def test_json_duplicate_key_rejected(self):          # spec test 6: both add "x" with different values at different places
    self.assertFalse(strict_json(b'{"x": 1, "y": 2, "x": 3}'))
    b = C(b'{\n "a": 1,\n "m": 2,\n "z": 3\n}\n')
    l = C(b'{\n "x": 1,\n "a": 1,\n "m": 2,\n "z": 3\n}\n')     # x added at the top
    r = C(b'{\n "a": 1,\n "m": 2,\n "x": 9,\n "z": 3\n}\n')     # x added before z: a clean line merge with x twice
    self.assertIsNone(merge_content("settings.json", l, b, r, False))
def test_merge_error_raises(self): ...               # merge_text with a NUL byte in L (git treats it as binary, rc 255): MergeError
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
- Produces (`lock.py`): `acquire(repo: Path, wait: float, log: Log) -> Optional[int]`: opens `repo/.sync.lock` (create) and tries `fcntl.flock(fd, LOCK_EX | LOCK_NB)` once, then every 0.2 s until `wait` elapses (so `wait=0` still makes one attempt); while `repo/.sync.lock.d` exists with mtime younger than 900 s the lock counts as busy (an old-code run may hold it); one older is removed with WARN `removing stale legacy lock dir`. Returns the fd (kept open for the process lifetime) or None after logging INFO `lock busy; skipping`.
- Produces (`context.py`):
  - `Report` (dataclass, all ints 0 by default): `remote_changes, written, merged, kept_both, trashed, local_wins, pushed, errors`; plus `conflicts_dir: Optional[Path] = None`, `first_error: Optional[str] = None`, `skipped_symlinks: int = 0`, `noop: bool = False` (set by push when it ends with no changes; the summary line is then logged at DEBUG). Methods `fail(message: str) -> None` (increments `errors`, sets `first_error` if unset), `summary(command: str, seconds: float) -> str` giving exactly `"{command} done in {seconds:.1f}s: {remote_changes} remote changes, {written} written, {merged} merged, {kept_both} kept-both, {trashed} trashed"` (push appends `, {pushed} pushed`), and `notice(home: Path) -> Optional[str]` (the only signature; callers pass `cfg.home`): first match of: errors gives `claude-sync: ERROR {first_error} (see ~/.claude-sync/last-sync.log)`; kept_both gives `claude-sync: {n} conflicts kept both, remote copies in {conflicts_dir with home shown as ~} (see last-sync.log)`; local_wins gives `claude-sync: {n} local edits kept over remote deletes (see last-sync.log)`; else None.
  - `Ctx` (dataclass): `cfg: Config, log: Log, git: Git, report: Report, stamp: str` (UTC `%Y-%m-%dT%H-%M-%SZ` at start), `lock_fd: Optional[int] = None` (the open lock file descriptor, held on the context so it lives as long as the run), `fired: Set[str]` (checkpoints already held this run); `checkpoint(name: str) -> None` (the `CLAUDE_SYNC_TEST_HOLD` rule in Decisions, once per name per run); `conflicts_root` and `trash_root` properties (`repo/conflicts/<stamp>`, `repo/trash/<stamp>`).
  - `BASE_REF = "refs/claude-sync/base"`, `PENDING_REF = "refs/claude-sync/pending"`.
- Produces (`checks.py`):
  - `class StopRun(Exception)` (message logged as ERROR by the caller, exit 1).
  - `prepare(ctx: Ctx) -> None`: runs, in order: (1) `git.clear_locks(60)`, WARN per removed path; (2) dead rebase: for `.git/rebase-merge` and `.git/rebase-apply`, store a non-empty `autostash` with `git stash store -m "autostash of a killed sync run" <sha>` (a non-zero exit is logged as WARN and ignored, as in the old tool), remove the directory, WARN; (3) attach HEAD without touching the work tree (Decisions); (4) `dirty_outside_data()` non-empty raises `StopRun("tracked files outside data/ are modified: ...")`; (5) `ensure_unlocked(ctx)`; (6) `restore_invariant(ctx)`.
  - `ensure_unlocked(ctx: Ctx) -> None`: when `.gitattributes` contains `filter=git-crypt` and the repo is locked by the Decisions rule (keys file or either filter setting missing): if `cfg.key` exists run `git-crypt unlock <key>` (cwd repo, output captured and never logged beyond the first stderr line, 60 s timeout); still locked or no key raises `StopRun("repo is git-crypt locked and no usable key at <path>")`.
  - `restore_invariant(ctx: Ctx) -> None`: `base = git.rev(BASE_REF)`; target is `base` or `HEAD`; when `git.rev("HEAD") != target` or `git.data_dirty()`: `reset_hard(target)`, INFO `restored data/ to <short sha>`; then always `clean_data()` (spec 6.4: the porcelain check does not see ignored `*.sync-tmp.*` leftovers).

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
def test_dead_rebase_removed_and_autostash_kept(self): ...  # autostash sha made with `git stash create` after a data/ edit; stash list gains "autostash of a killed sync run"
def test_dead_rebase_with_bad_autostash_still_cleared(self): ...  # autostash holds a plain commit sha: WARN, directory removed, no exception
def test_detached_head_keeps_bin_edit_and_stops(self): ...  # detached HEAD plus a bin/README hand edit: HEAD reattached, the edit still on disk, StopRun raised
def test_detached_head_reattached(self): ...
def test_dirty_bin_stops(self):                    # spec test 20, second half
    (m.repo / "bin/README").write_text("hand edit\n")
    with self.assertRaises(StopRun): prepare(ctx)
def test_restore_removes_untracked_and_tmp(self):  # spec test 26, unit half
    (m.repo / "data/stray.md").write_text("x"); (m.repo / "data/CLAUDE.md.sync-tmp.99").write_text("y")
    prepare(ctx)
    self.assertFalse((m.repo / "data/stray.md").exists()); self.assertEqual(m.git("status", "--porcelain"), "")
def test_restore_removes_ignored_tmp_only(self):   # spec test 26: HEAD == base and the ignored file is the only leftover
    (m.repo / "data/x.md.sync-tmp.7").write_text("y"); prepare(ctx)
    self.assertFalse((m.repo / "data/x.md.sync-tmp.7").exists())
def test_restore_resets_to_base(self): ...         # HEAD on a commit after base: HEAD back to base
def test_locked_repo_without_key_stops(self): ...  # fake: .gitattributes with filter=git-crypt committed (remote_commit root_relative), no keys dir, no key file: StopRun
def test_keys_without_filter_config_counts_as_locked(self): ...  # .git/git-crypt/keys/default present, filter.git-crypt.* unset, no key file: StopRun
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
  - `fetch_origin(ctx: Ctx) -> Optional[str]`: `git.fetch`; `"offline"` returns None (WARN already logged); `"error"` raises `StopRun`; `"ok"` resolves pending exactly as spec 6.4 lists it (`P = git.rev(PENDING_REF)`; if `P` and `is_ancestor(P, origin)`: `reset_hard(P)`, `update_ref(BASE_REF, P)`; then `delete_ref(PENDING_REF)`, last) and returns the `origin/<b>` sha. Callers read the base again after it returns.
  - `pull_steps(ctx: Ctx, origin: str) -> None`: spec 6.7 steps 4 to 9, run under the lock the caller already holds (it never takes the lock itself, so push can call it in-process, spec 6.4). Raises `StopRun` to stop; any `StopRun` leaves the base where it was. This task covers both a base and no base (`M = git.empty_tree()`, `fresh = False`; Task 7 adds the `.fresh-machine` marker, TAKE_REMOTE and the remaining edge cases), because `synced()` below runs a first pull.
  - `run_pull(ctx: Ctx) -> int`: `prepare`, `fetch_origin` (None gives 0), read `base = git.rev(BASE_REF)` again (a resolved pending may have moved it), `base is not None and origin == base` gives 0, else `pull_steps`, return 0.
- Produces (`cli.py`): `main(argv: List[str], env: Optional[Mapping[str, str]] = None) -> int`: usage error gives 2 with `usage: claude-sync {pull|push|status|dry-run}` on stderr; `CLAUDE_SYNC_RECONCILING=1` gives 0 (recursion guard kept from the old tool); builds `Config`, `Log` (`repo/last-sync.log`), `Git`, `Ctx`; for a locking command, `wait = cfg.lock_wait_override if cfg.lock_wait_override is not None else <command default>`, `fd = acquire(cfg.repo, wait, log)`, `None` returns 0 without running anything (INFO already logged), otherwise `ctx.lock_fd = fd`; runs the command; `StopRun`, `GitError`, `MergeError` and `CiphertextError` log ERROR with their text (no traceback), call `report.fail(str(e))`, and return 1; any other exception logs `exception()` (traceback), calls `report.fail(f"{type(e).__name__}: {e}")` and returns 1; for every locked command, in a `finally`: the summary line for pull and push (DEBUG instead of INFO when `report.noop`), and `msg = report.notice(cfg.home)`; `if msg: log.notice(msg)` (never `print`). `push`, `status` and `dry-run` dispatch to stubs that Tasks 8 and 9 replace (`status` prints `not implemented` and returns 0 until then).
- `bin/claude-sync` (bash, no `set -e`): the old PATH loop verbatim (lines 21 to 24 of the old script, verified); `ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)`; candidates: `$CLAUDE_SYNC_PYTHON` alone when set, else `/etc/profiles/per-user/$(id -un)/bin/python3`, `$HOME/.nix-profile/bin/python3`, `/run/current-system/sw/bin/python3`, `/opt/homebrew/bin/python3`, then `/usr/bin/python3` only if `xcode-select -p >/dev/null 2>&1`; the first that is executable and passes `-c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'` (a too-old one is skipped, not fatal) runs `cd "$ROOT" && exec "$py" -E -s -B -m claude_sync "$@"` (Decisions). None found: append `$(date -u +%Y-%m-%dT%H:%M:%SZ) pid=$$ ${1:-?} ERROR no usable python3 (3.9 or newer) found` to `${CLAUDE_SYNC_REPO:-$HOME/.claude-sync}/last-sync.log` with `2>/dev/null || true` (a failed append never stops the notice), print `claude-sync: ERROR no usable python3 found (see ~/.claude-sync/last-sync.log)`, exit 0.

Pull loop details (`pull_steps`), in this order per run:
1. `base = git.rev(BASE_REF)`. `M = base`, or `git.empty_tree()` when `base` is None. When `base` exists and `not is_ancestor(base, origin)`: one INFO `remote history was rewritten` and carry on with `M = base` (spec 6.7 step 4; no merge-base).
2. `changes = git.diff_raw(M, origin)`; `report.remote_changes = len(changes)`. Any change whose new mode is not `100644` or `100755` (and is not a delete), or whose status is `T`, raises `StopRun("unsupported entry in data/: <path> mode <mode>")` (spec 6.3). Any path failing `safe_rel` raises `StopRun("unsafe path in data/: <path>")`. Paths with `to_local(...) is None` are dropped with a count for one WARN line (Decisions).
3. Content reader: `B` from `repo/data/<p>` (spec 6.7 step 7; absent when there is no base or the path is not in it). `R`: `head = git.tree_blobs("HEAD")`; read from `repo/data/<p>` when `head.get(p) == (new_mode, new_blob)`, else collected and fetched with one `git.checkout_to(origin, paths, tmp)` into a `tempfile.mkdtemp()` (removed in `finally`). Exec bit comes from the mode (`100755`). The ciphertext check runs on the raw bytes, before anything else: any `B` or `R` with `is_ciphertext` raises `StopRun("ciphertext read from git for <path>")`.
4. For each change, deletions first (then sorted by path): skip with `report.skipped_symlinks += 1` when `via_symlink(cfg.claude, local_rel)`; `local = under(cfg.claude, local_rel)`; `L = canon_content(local_rel, read_content(local), me)` (None stays None; an `OSError` from the read raises `StopRun("cannot read <local_rel>: <strerror>")`); record `key = stat_key(local)`; `outcome = decide(...)` (a merged `.json` result has already passed `strict_json` inside `merge_content`; a remote version taken whole is written as is, spec 6.9); `ctx.checkpoint("pull:before-replace")`; before WRITE, TRASH or TAKE_REMOTE, `stat_key(local)` again: if it differs from `key` (None versus present counts as different), re-read `L` through the same `canon_content` path, record the new key and decide again, at most 3 times, then `Act.KEEP_BOTH` (spec 6.8). Apply:
   - WRITE: `atomic_write(local, decanon_content(local_rel, outcome.content, me))`; `report.written += 1`, plus `report.merged += 1` when `outcome.merged`.
   - TRASH: `move_aside(local, ctx.trash_root, local_rel)`; `report.trashed += 1`.
   - KEEP_LOCAL: WARN `outcome.warn`; `report.local_wins += 1` only when `outcome.local_win`.
   - KEEP_BOTH: `put_aside(ctx.conflicts_root, local_rel, decanon_content(local_rel, R, me))`, WARN `kept both: <local_rel>`, `report.kept_both += 1`, `report.conflicts_dir = ctx.conflicts_root`.
   After the loop: one INFO `skipped <n> paths under symlinks` when `report.skipped_symlinks`, and one WARN `skipped <n> paths outside the allowlist` when that count is non-zero.
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
def test_project_dir_cannot_shadow_stdlib(self): ...   # run the tool with cwd set to a temp dir holding json.py that raises on import: exit 0
def test_hook_env_inside_other_repo(self):            # spec test 31
    other = self.root / "proj"; subprocess.run(["git", "init", "-q", str(other)], env=m.env(), check=True)
    (other / "f.txt").write_text("mine\n"); subprocess.run(["git", "-C", str(other), "add", "f.txt"], env=m.env(), check=True)
    m.run("pull", cwd=other, env=m.env(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other), GIT_INDEX_FILE=str(other / ".git/index")))
    self.assertIsNotNone(m.base())
    self.assertEqual((other / "f.txt").read_text(), "mine\n")
    self.assertIn("A  f.txt", subprocess.run(["git", "-C", str(other), "status", "--porcelain"], env=m.env(), capture_output=True, text=True).stdout)
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
    self.assertEqual(a.conflicts(), [rel])
    self.assertRegex(r.stdout, r"^claude-sync: 1 conflicts kept both, remote copies in ~/\.claude-sync/conflicts/")
    self.assertTrue(any(" WARN kept both: " in l for l in a.log_lines()))
def test_memory_index_union(self): ...                                  # spec test 5
def test_settings_json_both_sides_kept(self): ...                       # spec test 6: different keys changed on each side: both changes in the result, which passes strict_json
def test_settings_json_same_key_added_kept_both(self): ...              # spec test 6: the duplicate-key fixture from test_merge: local unchanged, remote parked
def test_binary_and_exec_round_trip(self): ...                          # remote adds skills/s/img.png holding b"/Users/__USER__/\x00\xff" and an exec hooks/x.sh: bytes arrive unchanged, x.sh is 0o755
def test_jsonc_remote_update_applied(self): ...                         # skills/s/tsconfig.json with // comments, unchanged locally, changed remotely: written as is, no keep-both
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
- Produces: the same signatures, now covering: the `.fresh-machine` marker with no base (`fresh = (repo/.fresh-machine).exists()` only when there is no base; after the update-ref remove the marker), the remote mass delete guard (`D` changes counted against the files left under `data/` in `origin`'s tree, `K = len(git.tree_blobs(origin))`; trips when `D > 20 and D > K`, before any read or write, raising `StopRun("remote deletes D files and keeps K; run CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync pull if intended")` unless `cfg.allow_prune`), per-path type clash (`IsADirectoryError` or `NotADirectoryError` from the apply turns into KEEP_BOTH; an `OSError` from `put_aside` itself, or any other `OSError`, raises `StopRun("cannot write <local_rel>: <strerror>")`), TAKE_REMOTE (spec 6.9: first `copy_aside(local, ctx.conflicts_root, local_rel)`, which leaves the local file in place; only after it returns, `atomic_write(local, decanon_content(local_rel, R, me))`; an `OSError` from either raises `StopRun`, so the local file is never replaced without a saved copy; counts as kept_both and sets `report.conflicts_dir`), skipped symlink paths. `MergeError` from a merge raises `StopRun("merge failed for <local_rel>: ...")`.

- [ ] **Step 1: Write the failing tests**
```python
def test_new_machine_marker_takes_remote(self):           # spec test 10, pull half
    self.remote_commit({"settings.json": '{"model": "x", "hooks": {}}\n'})
    b = self.machine("b"); b.write("settings.json", '{"hooks": {"a": 1}}\n'); b.write("projects/-Users-alice-p/memory/only.md", "local only\n")
    (b.repo / ".fresh-machine").touch()
    b.run("pull")
    self.assertEqual(json.loads(b.read("settings.json"))["model"], "x")
    self.assertIn("settings.json", b.conflicts())
    self.assertEqual(b.read("projects/-Users-alice-p/memory/only.md"), "local only\n")
    self.assertFalse((b.repo / ".fresh-machine").exists()); self.assertIsNotNone(b.base())
def test_marker_survives_failed_first_pull(self): ...      # origin pointed at a missing path: ERROR, marker still there, base None; fix origin, next plain pull takes remote
def test_fresh_copy_failure_keeps_local(self):             # spec test 36
    # as test_new_machine_marker_takes_remote, but b.repo/"conflicts" is a regular file, so copy_aside cannot create its folder
    r = b.run("pull", expect=1)
    self.assertEqual(b.read("settings.json"), '{"hooks": {"a": 1}}\n')
    self.assertIsNone(b.base()); self.assertTrue((b.repo / ".fresh-machine").exists())
def test_migration_keeps_local_parks_remote(self):         # spec test 11
    # machine cloned at commit c1; ~/.claude holds c1 content plus a local edit to y.md;
    # remote_commit changes line 1 of x.md (c2); b changes the same line 1 locally, so the empty-base merge conflicts;
    # `git reset --hard origin/main` in b.repo after fetch (HEAD at c2, never applied); no base ref
    b.run("pull")
    self.assertEqual(b.read(y_rel), "local edit\n")
    self.assertEqual(b.read(x_rel), x_local_text)                    # local kept in place
    self.assertIn(x_rel, b.conflicts())                              # remote copy parked
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
def test_pull_never_writes_through_symlinked_dir(self):    # Review Focus 2, spec test 34
    # local skills/linked is a symlink to an outside dir; remote_commit adds skills/linked/SKILL.md;
    # local CLAUDE.md is a symlink to outside/real.md; remote_commit changes CLAUDE.md
    a.run("pull")
    self.assertFalse((outside / "SKILL.md").exists()); self.assertEqual((outside / "real.md").read_text(), real_before)
    self.assertTrue(any("skipped 2 paths under symlinks" in l for l in a.log_lines()))
def test_rewritten_remote_history(self):                   # spec 6.7 step 4: M is the base
    # base at c2 (x.md v2 applied locally); remote force-reset to c1 (x.md v1) then c3 adds y.md
    a.run("pull")
    self.assertEqual(a.read(x_rel), "v1\n")                # differs between base and the new tip only; merge-base would miss it
    self.assertEqual(a.read(y_rel), "new\n")
    self.assertTrue(any(" INFO remote history was rewritten" in l for l in a.log_lines()))
def test_remote_symlink_entry_stops(self): ...             # remote commits data/skills/s/link as a symlink (mode 120000): exit 1, base unchanged, nothing written
def test_unreadable_local_file_stops(self): ...            # remote changes x.md, local x.md chmod 000: exit 1, base unchanged (chmod back in tearDown)
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
- Consumes: `scan_local`, `via_symlink`, `to_local`, `under`, `canon_content`, `decanon_content`, `fsops` (`read_content`, `atomic_write`, `move_aside`, `is_ciphertext`), `Git` (including `staged_data`), `prepare`, `restore_invariant`, `fetch_origin`, `pull_steps`, `Ctx`.
- Produces:
  - `Scan` (dataclass): `changed: Dict[str, Content]` (repo_rel to new canonical content), `deleted: List[str]`, `local: Dict[str, Path]`.
  - `scan(ctx: Ctx) -> Scan`: `scan_local` (an `OSError` from it, such as an unlistable folder, raises `StopRun("cannot scan <path>: <strerror>")`, spec 6.10 step 3); read each local file with `read_content` (an `OSError` raises the same `StopRun`) and `canon_content`; compare canonical bytes and exec bit with `repo/data/<rel>` (read in process, exec from `st_mode & 0o100`); any `data/` file read for the comparison that `is_ciphertext` raises `StopRun("data/ holds ciphertext; repo is not unlocked")`. A tracked `data/` path is a delete when: `to_local` maps it (unmappable paths are never deletes, Decisions), it is not in the regular-file map, and it is neither `via_symlink` nor under a skipped symlink prefix. A directory at the local path is therefore a delete, which is how a type clash (spec 6.9) replaces the remote file with the directory's files.
  - `run_push(ctx: Ctx) -> int`, spec 6.10 steps 1 to 9 exactly. Structure, because the retry must repeat everything from step 4:
    1. `prepare(ctx)`.
    2. No base: `fetch_origin` (None: WARN already logged, return 0) then `pull_steps(ctx, origin)` in this process (it never locks); a `StopRun` from it is re-raised after `restore_invariant`, with nothing pushed. Still no base afterwards: return 0.
    3. `s = scan(ctx)`; nothing changed: DEBUG `no changes`, return 0 (no network).
    4. Loop, `attempt` 1 to 3: `base0 = git.rev(BASE_REF)`; `origin = fetch_origin(ctx)` (None returns 0); `base = git.rev(BASE_REF)`; if `origin != base`: `pull_steps(ctx, origin)` (a `StopRun` is re-raised after `restore_invariant`; nothing is committed or pushed); if the base moved in this step (`git.rev(BASE_REF) != base0`): `s = scan(ctx)`, and an empty rescan returns 0 (a landed-but-unseen push ends silently, spec test 27).
    5. Empty-file guard (age from `st_mtime`, 600 s): a young 0-byte local file whose data version is non-empty is dropped from `s.changed`; an old one is `move_aside`d into `trash_root`, then restored with `atomic_write(local, decanon_content(rel, <data content with its exec bit>, me))`, WARN, and dropped from `s.changed`.
    6. Local mass delete guard: `D = len(s.deleted)`, `K` = tracked `data/` files minus `D` plus new paths in `s.changed`; trips when `D > 20 and D > K`, raising `StopRun("refusing to push: would delete D files and keep only K; CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync push if intended")` after `restore_invariant`, unless `cfg.allow_prune`.
    7. Data writes via `atomic_write(under(cfg.data, rel), content)`; deletions with `unlink`, then remove empty parent folders while `parent != cfg.data` (never `data/` itself); `git.add_data()`; if `not git.staged_data()`: DEBUG `no changes`, `restore_invariant`, return 0 (no commit, no pending, spec 6.10 step 7). The plaintext guard (Decisions) runs here, before the commit. `ctx.checkpoint("push:before-commit")`; commit message `sync: {me}@{host} {UTC %Y-%m-%dT%H:%M:%SZ}`, where `host = <scutil --get LocalHostName stdout>.strip()`, falling back to `socket.gethostname().split(".")[0]` when that is empty or fails.
    8. `update_ref(PENDING_REF, HEAD)`; `ctx.checkpoint("push:before-push")`; `git.push`; `ctx.checkpoint("push:after-push")`. `"ok"`: `update_ref(BASE_REF, HEAD)`, `delete_ref(PENDING_REF)`, `report.pushed = len(s.changed) + len(s.deleted)`, return 0. `"offline"`: WARN with the git text, return 0 (pending stays for the next fetching run). `"rejected"`: `restore_invariant`, `delete_ref(PENDING_REF)`, `s = scan(ctx)`, and continue the loop at step 4 (fetch, pull steps, a rescan, steps 5 to 8 again). `"error"`: `StopRun`.
    After 3 rejected attempts: `StopRun("push rejected 3 times")`.

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
def test_push_without_base_pulls_first(self): ...           # spec test 10, push half: no base, offline remote: nothing pushed, exit 0, one WARN line
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
def test_push_after_unseen_landed_push_is_silent(self):     # spec test 27, push variant
    # a's commit pushed by hand and pending set (as in test_pending_landed_moves_base); base left behind
    r = a.run("push")
    self.assertEqual(r.stdout, ""); self.assertEqual(a.base(), pushed_sha)
    self.assertFalse(any(" ERROR " in l for l in a.log_lines()))
def test_odd_names_round_trip_push(self): ...                # Review Focus 3, push half
def test_push_stops_when_inner_pull_fails(self):             # spec test 32
    # a has a local edit to rel; remote changes other_rel; a's parent folder of other_rel is chmod 555, so the write fails
    tip = self.remote_tip(); r = a.run("push", expect=1)
    self.assertEqual(self.remote_tip(), tip); self.assertEqual(a.base(), base_before)
    self.assertEqual(a.git("status", "--porcelain", "--", "data"), ""); self.assertIn("claude-sync: ERROR", r.stdout)
def test_push_completes_killed_first_pull(self):             # spec test 33
    # new machine with a local-only memory file; pull held at pull:before-base and SIGKILLed (base still None)
    r = a.run("push", env=a.env(CLAUDE_SYNC_LOCK_WAIT="5"))   # must not wait on its own lock
    self.assertIsNotNone(a.base()); self.assertIn(only_rel_repo, self.remote_paths())
def test_unreadable_dir_stops_push(self):                    # spec test 35
    # memory folder of a synced project chmod 000: exit 1, remote tip unchanged, no file deleted on the remote
def test_nothing_staged_no_commit(self):                     # spec test 37
    # the only local difference is a young 0-byte file whose data version is non-empty
    tip = self.remote_tip(); a.run("push")
    self.assertEqual(self.remote_tip(), tip); self.assertIsNone(a.ref("refs/claude-sync/pending"))
def test_type_clash_directory_replaces_remote_file(self): ... # spec test 24, push half: after the pull keep-both, push sends skills/foo/* and deletes skills/foo; b's pull trashes its file
def test_top_level_symlink_not_pushed(self): ...             # spec test 34: CLAUDE.md is a symlink: not pushed and not deleted on the remote
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
  - `status`: no lock, no network, no writes. Prints lines, in this order: `base: <sha or none>`, `head: <sha>`, `origin: <sha of origin/<b> or none>`, `pending: yes|no`, `fresh-machine marker: yes|no`, `local changes: <n changed> changed, <n deleted> deleted`, `conflict folders: <n>`, `last: <last summary line from the log or none>`. The `local changes` value is `locked` when the repo is git-crypt locked by the Decisions rule or `data/CLAUDE.md` starts with the git-crypt header, and `unknown (a run is in progress or was interrupted)` when `HEAD != base` or `data_dirty()` (spec 6.12); `scan` runs only when neither holds, and a `StopRun` from it prints `unknown` too.
  - `dry-run` / `dry`: lock (20 s), `prepare(ctx)` (the spec 6.6 checks, including the unlock and the invariant restore), then `scan`; prints `A <rel>`, `M <rel>`, `D <rel>` lines sorted by path; no commit, no fetch, no push; exit 0.
  - Retention: in `cli.main`'s `finally`, only when the lock was acquired, after every locked pull, push or dry-run (success or not): `prune_older_than(repo/"conflicts", 30, now)` and the same for `trash`; INFO per removed folder.
  - README: replace the Usage, New machine and Automation sections to match the spec (three-way sync, `conflicts/`, `trash/`, the notice, `touch ~/.claude-sync/.fresh-machine` before the first pull on a new Mac, hooks as in spec section 7). No em dashes or arrows; keep the allowlist section.

- [ ] **Step 1: Write the failing tests**
```python
def test_status_fields(self): ...
def test_status_locked_and_unknown(self): ...            # spec test 38: gitcrypt remote, no key: "local changes: locked"; after a push SIGKILLed at push:before-push: "local changes: unknown ..."
def test_dry_run_lists_and_writes_nothing(self): ...     # A/M/D lines; HEAD, base and the remote tip unchanged afterwards
def test_dry_run_after_killed_push(self): ...            # push SIGKILLed at push:before-commit leaves data/ dirty; dry-run restores it and lists exactly what the next push then sends
def test_retention_runs_after_error(self): ...           # aged conflicts/ folder removed by a pull that ends in ERROR (broken remote)
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
    b_procs = []                                           # other machine keeps the remote moving: a push only does work after a local change
    for i in range(2):
        b.write(f"projects/-Users-alice-p/memory/b{i}.md", f"from b {i}\n"); b_procs.append(b.popen("push"))
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
- Produces (`harness.py`): `make_fixture(self, memory_files: int = 750, skill_files: int = 600, total_bytes: int = 17 * 2**20, gitcrypt: bool = True) -> Optional[Path]`: rebuilds `remote.git` with deterministic content (`random.Random(772)`, text lines, a few `MEMORY.md` indexes, some `hooks/*.sh` with exec bits) in one commit; returns the key path when `gitcrypt`. `remote_commit_n(n: int) -> str` (one commit changing `n` fixture memory files); `Machine.edit_n(n: int) -> None` (appends a line to `n` local memory files); `remote_data_blobs() -> List[bytes]` (raw bytes of every blob under `data/` at the remote tip, read with `git cat-file blob`, no filters).

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
def test_filter_missing_stops_before_write(self):          # filter config gone and no key: counts as locked (Decisions)
    # after setup, remove filter.git-crypt.* from a.repo/.git/config and delete the key file; remote change arrives
    r = a.run("pull", env=a.env(CLAUDE_SYNC_KEY=str(self.root / "gone.key")), expect=1)
    self.assertEqual(a.read(rel), before)                     # stopped; the message need not say "ciphertext"
def test_filter_attribute_off_ciphertext_guard(self):      # spec test 18 last sentence: the filter silently does not run
    # repo unlocked; append "data/** !filter" to a.repo/.git/info/attributes; remote change to rel arrives
    r = a.run("pull", env=env_a, expect=1)
    self.assertIn("ciphertext", r.stdout); self.assertEqual(a.read(rel), before)
def test_filter_missing_never_pushes_plaintext(self):      # plaintext guard (Decisions)
    # same broken state, plus a local edit; push must fail and the remote must not change
    tip = self.remote_tip(); a.write(rel, "edit\n"); a.run("push", env=a.env(CLAUDE_SYNC_KEY=str(self.root / "gone.key")), expect=1)
    self.assertEqual(self.remote_tip(), tip)
    for blob in self.remote_data_blobs(): self.assertTrue(blob.startswith(b"\x00GITCRYPT"))
def test_plaintext_guard_unit(self): ...                   # unlocked repo, clean filter replaced by `cat` in .git/config: push raises StopRun "refusing to commit unencrypted", index reset, remote unchanged
def test_locked_repo_without_key(self):                    # spec test 22
    self.make_gitcrypt_remote()                            # without it the remote is plaintext and the pull would succeed
    a = self.machine("a"); r = a.run("pull", expect=1)     # CLAUDE_SYNC_KEY points at a missing file
    self.assertFalse((a.home / ".claude").exists() and any((a.home / ".claude").rglob("*")))
# test_timing.py: one fixture per class (setUpClass), git-crypt on; skipped when git-crypt is missing (reported as skipped).
# The class overrides setUp and tearDown (no per-test seed remote), so the class fixture is not wiped between tests.
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
- Create: `home-manager/claude-sync-hooks-ensure.py` (the ensure script, moved out of the Nix string so it can be tested), `home-manager/claude_sync_hooks_ensure_test.py` (underscores, so `unittest` can import it)
- Modify: `home-manager/claude-sync-hooks.nix` (lines 36 to 52 `pushIfDirty`, 106 to 151 `ensureScript`, 180 to 218 bootstrap)

**Interfaces:**
- `claude-sync-hooks-ensure.py`: `WANT` per event: SessionStart matcher `""`, command `GIT_TERMINAL_PROMPT=0 "$HOME/.claude-sync/bin/claude-sync" pull 2>>"$HOME/.claude-sync/last-sync.log" || true`, timeout 60; SessionEnd matcher `""`, command `: > "$HOME/.claude-sync/.dirty"; launchctl kickstart "gui/$(id -u)/org.nix-community.home.claude-sync-push" >/dev/null 2>&1 || true`, timeout 10; PostToolUse matcher `Write|Edit`, command `$HOME/.claude/hooks/claude-sync-mark-dirty.sh`, timeout 45. Targets: `~/.claude/settings.json` plus every `~/Library/Application Support/orca/claude-profiles/*/home/settings.json` that is a regular file (symlinks skipped). Per event: the hooks whose command contains `claude-sync` must be exactly one, equal to WANT in command, timeout and group matcher (missing matcher counts as `""`); otherwise remove them all (dropping groups left without hooks) and append one WANT group. Other hooks and keys are untouched, Orca's own hooks included (spec section 7). A file whose content would not change is not written; when no target changes, print `already present`. The new content is parsed again with `json.loads` before an atomic write via `<file>.tmp` and `os.replace`, as today. Unparseable JSON: print a message and leave that file alone (exit 1 at the end).

- [ ] **Step 1: Create the worktree**: `git -C ~/dotnix worktree add /Users/dannyfroberg/dotnix-TO-772 -b dfroberg/TO-772-claude-sync-hooks main`. Expected: new branch from `main`, `~/dotnix` itself unchanged (`git -C ~/dotnix status --short` empty).
- [ ] **Step 2: Write the failing test** `claude_sync_hooks_ensure_test.py` (unittest; runs the script as a subprocess with `HOME` set to a temp dir):
```python
def test_live_shape_replaced(self): ...          # live ~/.claude shape (SessionStart pull >> log 180 s, SessionEnd kick 10 s, PostToolUse 45 s): SessionStart becomes WANT with timeout 60; others unchanged; non-claude-sync hooks kept
def test_old_nix_shape_replaced(self): ...       # SessionEnd "claude-sync push" 45 s replaced by the kick, timeout 10
def test_orca_profile_updated_symlink_skipped(self): ...   # two profile homes: regular file updated, symlinked settings.json untouched
def test_orca_own_hooks_kept(self): ...          # profile settings with Orca hooks in SessionStart and PostToolUse plus other top-level keys: all kept byte-equal after json round trip
def test_idempotent(self): ...                   # second run prints "already present" and leaves mtime unchanged
def test_bad_json_left_alone(self): ...
```
- [ ] **Step 3: Run it to fail**: `python3 home-manager/claude_sync_hooks_ensure_test.py -v` from the worktree root. Expected: FAIL (script missing).
- [ ] **Step 4: Implement** the script; in the Nix file set `ensureScript = ./claude-sync-hooks-ensure.py;` (its use site, line 240, is `/usr/bin/python3 ${ensureScript}`, so a path literal interpolates to the store path of the file; checked); replace `pushIfDirty`'s body so it always runs `claude-sync push` (keep the PATH line and the `.dirty` restore on failure, drop the `ahead` gate); in the bootstrap, add `: > "${syncRepo}/.fresh-machine"` right after `git -C "${syncRepo}" reset --hard -q origin/main`. Update the comments that describe the old behaviour.
- [ ] **Step 5: Run the test to pass.**
- [ ] **Step 6: Commit in the dotnix worktree**: `git add home-manager && git commit -m "feat(claude-sync): keep hooks current in all profiles, push every tick, mark fresh machines"`
- [ ] **Step 7: Build, never switch**: find the host attribute with `nix eval --raw /Users/dannyfroberg/dotnix-TO-772#darwinConfigurations --apply 'c: builtins.concatStringsSep " " (builtins.attrNames c)'`, then `nix build --no-link /Users/dannyfroberg/dotnix-TO-772#darwinConfigurations.<host>.system`. Expected: exit 0. Do not run `darwin-rebuild switch`, `home-manager switch` or any activation script.

---

## Execution notes for the lead

- Builder runs (Sonnet, high effort, at most four tasks each, this plan's literal text in the brief): A = Tasks 1 to 3; B = Tasks 4 and 5; C = Tasks 6 and 7; D = Tasks 8 and 9; E = Tasks 10 and 11; F = Task 12.
- After each run: a checker (Haiku 5.5; Sonnet for C, D and E) on the run's commit range: diff scope against the tasks, both-interpreter suite output, commit authorship and messages, no writes outside the worktree.
- Final gate: one Fable review of the whole branch against the spec before the report to the owner.

---

## Adversarial review

Two rounds ran on this plan on 2026-10-25. Round 1 ran on Fable because the lead wrongly
concluded that xAI was unreachable (it checked only the environment, not
`~/.config/xai/env`); round 2 is the required grok-4.7 round. The plan was also revised
for the spec changes from the grok-4.7 design round (spec section 12, commit 1dd0d746).

### Round 1: Fable

Against commit 55fc9e2a. Verdict: NEEDS FIXES (2 major, 8 minor), all accepted.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | Major | A push after an unseen landed push scanned against the old base, then committed nothing and failed with an ERROR notice. | Task 8: rescan whenever the base moved after the fetch; an empty rescan exits 0. `test_push_after_unseen_landed_push_is_silent`. |
| 2 | Major | "keys present" counted as unlocked with the filter config missing; `reset --hard` then wrote ciphertext into `data/`, and push could commit plaintext to the public remote. | Decisions: unlocked needs the key file and both filter settings; plaintext guard before every commit; `scan` stops on ciphertext. `test_keys_without_filter_config_counts_as_locked`, `test_filter_missing_never_pushes_plaintext`, `test_plaintext_guard_unit`. |
| 3 | Minor | `git stash store` refuses a plain commit, so the autostash test as described would fail. | Task 5: the test builds the autostash with `git stash create`; a failed store is a WARN. `test_dead_rebase_with_bad_autostash_still_cleared`. |
| 4 | Minor | `remote_commit` could not change files outside `data/`, and harness git did not say which environment it used. | Task 1: `root_relative`; all harness git uses `base_env`. |
| 5 | Minor | The generic exception branch set no error, so no notice printed; two `notice` signatures. | Task 5 `Report.fail`, one `notice(home)`; Task 6 calls `report.fail` in both branches. |
| 6 | Minor | `checkout -f -B` to reattach HEAD discarded hand edits under `bin/`. | Decisions: `update-ref` plus `symbolic-ref`, no work-tree write. `test_detached_head_keeps_bin_edit_and_stops`. |
| 7 | Minor | `acquire(wait=0)` never tried. | Task 5: always one attempt. |
| 8 | Minor | Machine b's pushes in the concurrency test had nothing to push. | Task 10: b writes before each push. |
| 9 | Minor | `-m` from the hook's cwd lets a project `json.py` shadow the standard library. | Decisions and Task 6: `cd "$ROOT"`, `-E -s -B`. `test_project_dir_cannot_shadow_stdlib`. |
| 10 | Minor | `worktree add -b` without a start point. | Task 12: start point `main`. |

### Round 2: grok-4.7

`grok_review.py`, model grok-4.7, exit 0. Verdict: DO NOT EXECUTE (8 critical, 28 major,
8 minor). The review read the plan before the round 1 edits were saved, so several
findings were already fixed. The lead checked each claim; where it named git behaviour, it
was tested in a scratch repo (git 2.54.0).

| # | Severity | Finding | Resolution |
|---|---|---|---|
| C1 | Critical | `checkout -f -B` destroys `bin/` edits. | Already fixed in round 1 (finding 6). |
| C2 | Critical | Task 6 pull could not set a first base, which its own `synced()` needs. | Task 6 `pull_steps` handles no base (`M` = empty tree, `fresh` False); Task 7 adds the marker. |
| C3 | Critical | `test_classify` expected "error" for a string that contains the offline pattern. | The test expects "offline". |
| C4 | Critical | Push never deleted a remote file when a local directory sat at that path. | Task 8 `scan`: delete when not a regular file and not via a symlink; a directory counts. `test_type_clash_directory_replaces_remote_file`. |
| C5 | Critical | `atomic_write(local, decanon content)` did not type-check, and a naive fix rewrites binaries. | Task 2 `canon_content` / `decanon_content` (text only, exec bit kept); every write uses them. `test_content_helpers_leave_binary_alone`, `test_binary_and_exec_round_trip`. |
| C6 | Critical | The lock fd and the None result were unspecified at the call site. | Task 6 `cli.main`: None returns 0; `ctx.lock_fd` holds the fd. |
| C7 | Critical | The rejected-push retry went back to the fetch only and never merged. | Task 8 loop: restore, delete pending, rescan, then fetch, pull steps and steps 5 to 8 again. |
| C8 | Critical | `Machine.run` and `Machine.git` did not name their program or environment. | Task 1: `run` executes the shim with `env or self.env()`; `git` is `git -C repo` with `self.env()`; extras override. |
| M1 | Major | `acquire(wait=0)` never tried. | Already fixed in round 1 (finding 7). |
| M2 | Major | A remote-emptied file produced the "local edits kept over remote deletes" notice. | Task 4 `Outcome.local_win`, set only by rule 6; Task 6 counts only that. |
| M3 | Major | The race re-read skipped canonicalisation, and a file created in the window was not "moved". | Task 6 step 4: same `canon_content` path; None versus present differs. |
| M4 | Major | A hold point inside the per-path loop waited again for every path. | Decisions: each point holds once per run (`Ctx.fired`). |
| M5 | Major | TAKE_REMOTE parked L before writing R. | Task 7 per spec 6.9: `copy_aside` first (L stays in place), then write R; sets `conflicts_dir`. `test_fresh_copy_failure_keeps_local`. |
| M6 | Major | Empty-folder cleanup could remove `data/` itself. | Task 8 step 7: stop at `cfg.data`. |
| M7 | Major | The empty-file restore dropped the exec bit and decanonicalised binaries. | Task 8 step 5: `decanon_content` with the data file's exec bit. |
| M8 | Major | `scutil` output kept its newline. | Task 8 step 7: `.strip()`, fallback when empty. |
| M9 | Major | Push offline had no log line and a different level from fetch. | Global Constraints and Task 8: WARN, exit 0. |
| M10 | Major | dry-run listed against a broken invariant. | Task 9 per spec 6.12: dry-run takes the lock and runs `prepare` first. `test_dry_run_after_killed_push`. |
| M11 | Major | Retention could be skipped on errors; `CLAUDE_SYNC_LOCK_WAIT=0` could read as unset. | Task 9: retention in `finally` once the lock is held; Task 6: `is not None`. `test_retention_runs_after_error`. |
| M12 | Major | The notice could be multi-line and over 200 characters. | Task 1 `Log.notice` folds then truncates; Task 6 never prints directly. `test_notice_one_line`. |
| M13 | Major | `origin == base` compared against a base read before pending was resolved. | Task 6 `run_pull` and Task 8 step 4 read the base again after `fetch_origin`. |
| M14 | Major | The symlink skip count was never logged. | Task 6 step 4: one INFO line after the loop. |
| M15 | Major | `put_aside` / `move_aside` did not create parents. | Task 2: all three create parents through `under`. |
| M16 | Major | Writing through a top-level symlink was specified two ways. | Moot: spec 6.3 now puts every symlinked path outside the sync; `atomic_write` refuses a symlink. |
| M17 | Major | The memory mapping read as the example only. | Task 2: general for every `<p>`; test with a non-user project name. |
| M18 | Major | `check=True` broke the Optional queries; `killpg` could raise. | Task 3: `check=False` for them, `ProcessLookupError` ignored. `test_optional_queries_do_not_raise`. |
| M19 | Major | `remote_commit` could not change `bin/`. | Already fixed in round 1 (finding 4). |
| M20 | Major | The per-test `setUp` would wipe the timing fixture. | Task 11: the timing class overrides `setUp` and `tearDown`. |
| M21 | Major | The locked-repo test never locked the repo. | Task 11: `make_gitcrypt_remote()` first. |
| M22 | Major | Removing the filter config is not the silent-ciphertext case. | Task 11: new `test_filter_attribute_off_ciphertext_guard` (`data/** !filter`); the old test no longer requires the word "ciphertext". |
| M23 | Major | A hyphenated test module cannot be imported; "already present" was unspecified. | Task 12: `claude_sync_hooks_ensure_test.py`; no write when unchanged. |
| M24 | Major | `ensureScript` as a path may not fit the use site. | Rejected after reading the module: line 240 is `/usr/bin/python3 ${ensureScript}`, so a path literal interpolates to its store path. Noted in Task 12. |
| M25 | Major | Named Decisions contradicted "stop and report". | Spec line: Decisions override only the points they name. |
| M26 | Major | Repo paths were joined under `~/.claude` with no `..` check. | Task 2 `safe_rel`, `under`; Task 6 stops on an unsafe path. `test_safe_rel_and_under`. |
| M27 | Major | `Config.data` was never defined. | Task 1: `data = repo/"data"`. |
| M28 | Major | The migration fixture might merge cleanly. | Task 7: both sides change the same line. |
| m1 | Minor | `ls-tree -z` layout unspecified. | Task 3 states it. |
| m2 | Minor | Pathspecs were not literal. | Task 3: `:(literal)data/<p>`; verified with a `:`-prefixed name and a test filter. |
| m3 | Minor | `Machine.env` precedence unspecified. | Task 1: extras last. |
| m4 | Minor | `conflicts()` / `trash()` relativity ambiguous. | Task 1: POSIX strings relative to the stamp folder; tests updated. |
| m5 | Minor | A failed log append in the shim could stop the notice. | Task 6: no `set -e`; the append is `|| true`. |
| m6 | Minor | `push -q` might not print the rejection text. | Rejected: verified on git 2.54.0, `push -q` prints `! [rejected] ... (fetch first)`, rc 1. |
| m7 | Minor | The PATH loop line numbers might be wrong. | Rejected: lines 21 to 24 checked. |
| m8 | Minor | The log test does not prove UTC. | Task 1: `datetime.now(timezone.utc)` stated. |

Also found while applying these: the test 28 fixture in Task 4 put the removal and the
addition in adjacent hunks, where `--union` brings the removed line back (verified); the
fixture now keeps them apart. And a spec sentence added from design finding M13, which was
about the nix step, would have made every remote update to a `.json` file with comments a
keep-both; spec 6.9 now applies the strict check to merged results only
(`test_jsonc_remote_update_applied`).
