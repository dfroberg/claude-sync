# claude-sync rewrite: design (TO-772)

Status: draft for owner approval. Date: 2026-10-25. Jira: TO-772.

## 1. Intent

claude-sync keeps `~/.claude` (settings, CLAUDE.md, skills, agents, commands, hooks and
per-project memory) in step between two Macs through the git-crypt repo
`dfroberg/claude-sync`. It runs from Claude Code hooks, so every bug lands in the middle of
a work session. The worst one is silent loss of local edits. The rewrite replaces the
last-writer-wins file copy with a three-way sync that knows what each side changed since
the last time they agreed, so nothing local is overwritten without a merge or a kept copy.

## 2. What is wrong today (verified in code and in the live repo, read only)

1. Pull reverts local edits even when the remote did not change. `cmd_pull` runs `apply()`
   (bin/claude-sync lines 202 to 225), which copies every file in `data/` over `~/.claude`.
   `data/` holds this machine's own last push, so any edit made since that push is put
   back by the next SessionStart in any session. 3590 of the 3596 sync commits come from
   EZ-MBP-GD4RPHKWL4 and only 5 from Dannys-MacBook-Pro, so in practice this is many
   parallel sessions on one machine, not two machines fighting.
2. Push has the mirror bug. `cmd_push` runs `git pull` without applying, then `collect()`
   writes this machine's files over whatever the other machine changed in `data/`, and
   prunes files the other machine added. The tripwire only stops more than 20 deletions.
3. The git layer can wedge. `pull --rebase --autostash` killed by a hook timeout left a
   `rebase-merge` directory that blocked every pull for weeks, logged as "offline".
4. The lock is a directory plus an age check; a killed run leaves it behind.
5. `collect()` forks `sed` and `cmp` per file (1368 files, about 39 s per push).
6. memory-reconcile runs for every project on every pull and push and may call `claude -p`.
7. `last-sync.log` (21 MB) has no timestamps.
8. Orca sessions do not run the hooks in `~/.claude/settings.json`. See section 6.13.
9. The nix module re-adds a 45 s SessionEnd push hook when it finds none, while the live
   hooks are a 180 s SessionStart pull and a 10 s SessionEnd launchd kick.

## 3. Goals, non-goals, anti-goals

Goals (from TO-772):
- Never overwrite a local file that changed since the last sync. Merge, or keep both.
- Pull and push finish well inside their hook timeouts: incremental, no full rewrite or
  full re-encrypt.
- Fail loudly: real errors in a timestamped log, never reported as offline.
- Safe under many concurrent sessions on one machine: one writer at a time, a lock that
  cannot go stale, waits with a timeout.
- Each MEMORY.md keeps every entry from both sides.

Non-goals:
- Changing what is synced (the allowlist stays as it is).
- Syncing Orca profile `settings.json` or profile `CLAUDE.md` files (section 6.13).
- Changing memory-lint or memory-index-orphans.
- Real-time sync. The 5 minute timer and session hooks stay.

Anti-goals (a failure even if it technically works):
- Any path that drops a local edit silently, including in a race or after a kill.
- A wedge that needs manual git surgery to clear.
- A sync step that blocks session start for more than a few seconds in the normal case.
- Writing invalid JSON into a settings file.

## 4. Constraints

- Public repo. `data/**` is git-crypt encrypted. The tool and tests never read or print the
  key, and tests never use it.
- Code delivery stays as today (owner's choice): the tool runs from `~/.claude-sync/bin`
  and updates itself whenever a pull moves the repo. Every version must therefore keep the
  command line the existing hooks call: `claude-sync pull`, `claude-sync push`,
  `claude-sync status`, `claude-sync dry-run`.
- Python 3.9 or newer, standard library only. 3.9 is `/usr/bin/python3` on these Macs;
  nix provides 3.14. No pip installs.
- Hooks and launchd give a thin PATH. The current PATH fix (nix profiles before system,
  so git finds git-lfs for the global pre-push hook) must be kept.
- macOS only. GNU and BSD tool differences do not matter any more because the work moves
  into Python.

## 5. Approaches

Considered:
- A. Three-way sync rewritten in Python (standard library). Per-machine base ref, merges
  through `git merge-file`, `flock` lock, no rebase. Fixes every goal and is easy to test.
- B. The same design in bash. One language, but macOS has no `flock` binary, per-file
  forks keep it slow, and it is hard to test.
- C. Minimal bash patch: a hash manifest so `apply()` skips files the remote did not
  change. Fixes the headline bug only; wedges, speed and locking stay.

Picked by the owner on 2026-10-25: A, with these choices:
- Conflicts: merge first; if hunks still conflict, keep both (local stays in place, remote
  copy parked). MEMORY.md takes the union of both sides.
- Delivery: self-update through the sync repo, as today.
- Deletes: propagate, but into a trash folder, never a hard delete.
- Transport: keep git plus git-crypt (my call, not objected to). Its history is what
  recovered the 0-byte files on 2026-10-08 and 2026-10-13.

## 6. Design

### 6.1 Repo layout

```
bin/claude-sync          bash shim: PATH fix, pick python, exec the package
claude_sync/             the Python package (module split is the builder's call)
tests/                   unittest suite, hermetic (section 8)
bin/memory-reconcile     unchanged, manual tool only
bin/memory-lint          unchanged
bin/memory-index-orphans unchanged
```

The shim keeps the existing PATH loop, sets `PYTHONDONTWRITEBYTECODE=1`, and picks the
first working interpreter of: `/etc/profiles/per-user/$USER/bin/python3`,
`$HOME/.nix-profile/bin/python3`, `/run/current-system/sw/bin/python3`,
`/opt/homebrew/bin/python3`, then `/usr/bin/python3` only if `xcode-select -p` succeeds
(otherwise that stub opens an install dialog). It requires 3.9 or newer. With none found it
writes a timestamped ERROR to the log, prints the one-line notice from 6.11 to stdout, and
exits 0, so a hook never breaks a session but the session still sees the problem.

`.gitignore` gains: `.sync.lock`, `conflicts/`, `trash/`, `__pycache__/`.

### 6.2 Paths and environment

- `CLAUDE` = `$HOME/.claude`. `CLAUDE_CONFIG_DIR` is ignored on purpose: Orca and the work
  profile set it, but the synced tree is always `~/.claude` (their `projects/`, `skills/`
  and so on are symlinks into it).
- `REPO` = `$CLAUDE_SYNC_REPO` or `$HOME/.claude-sync`. `DATA` = `$REPO/data`.
- `ME` = `$CLAUDE_SYNC_USER` or the login name. The override exists for tests.
- `BRANCH` = `$CLAUDE_SYNC_BRANCH` or `main`.

### 6.3 What syncs and how paths are rewritten

Unchanged from today:
- Files `CLAUDE.md`, `settings.json`, `mcp_settings.json`, `keybindings.json`; trees
  `skills/`, `agents/`, `commands/`, `hooks/`; and `projects/<proj>/memory/**` stored as
  `data/memory/<canonical proj>/**`.
- Regular files only; symlinked files are not collected. Writing into a local path that
  is a symlink writes its target. Files named `*.sync-tmp.*` are ignored.
- Text files (same extension list as `is_text` today) are canonicalised on the way in:
  `/Users/<ME>/` becomes `/Users/__USER__/` and `-Users-<ME>-` becomes
  `-Users-__USER__-`, and the reverse on the way out. Project directory names use the same
  rule. Other files are copied byte for byte.
- The exec bit travels (git records it). Other mode bits do not.

All comparisons below are on canonical bytes: local content is canonicalised before it is
compared with repo content. The rewrite is lossy in the same rare cases as today (a file
that already contains the literal `__USER__` token); that is accepted, not fixed.

### 6.4 State: the base

Each machine keeps a local ref `refs/claude-sync/base`. It is never pushed (push only
sends `HEAD:main`). Meaning: the last remote commit whose content this machine has fully
taken into `~/.claude`. For any path, the base version is what both sides last agreed on,
which makes it the common ancestor for a three-way decision.

Invariant when no run holds the lock: `HEAD == base`, and `data/` in the work tree equals
the base tree (clean). Every run starts by restoring it if a killed run broke it:
if `HEAD != base` or `git status --porcelain -- data` is not empty, run
`git reset -q --hard base`. This is safe because `data/` is derived and any unpushed local
content still lives in `~/.claude`, where the next scan finds it again.

When there is no base ref (first run of the new code on a machine):
- Migration (the normal case): `~/.claude` has synced files. Base becomes the current
  `HEAD`, which is the last commit the old code pulled and applied, or pushed. If the old
  code left unpushed commits, `HEAD` is not an ancestor of `origin/main`; pull then uses
  their merge base as the common ancestor (6.7 step 4), which is correct because those
  commits were collected from `~/.claude` and their content is still there.
- Fresh machine: `~/.claude` has none of the synced files. Base becomes the empty tree, so
  the first pull writes everything and nothing local counts as deleted.

### 6.5 Lock

- `fcntl.flock` on `$REPO/.sync.lock`. The kernel releases it when the process dies,
  including on SIGKILL, so it cannot go stale.
- Waits with a budget, then skips with an INFO log line and exit 0: pull 20 s, push 60 s,
  status none (it does not take the lock), dry-run 20 s. Overridable through
  `CLAUDE_SYNC_LOCK_WAIT`.
- Transition guard: while the old `.sync.lock.d` directory exists and is younger than
  900 s, an old-code run may be active, so the new code treats the lock as busy. An
  older one is removed with a WARN line.

### 6.6 Repo checks at the start of every locked run

1. Remove `.git/index.lock`, `.git/HEAD.lock`, ref `*.lock` files and
   `.git/packed-refs.lock` older than 60 s (we hold the lock, so they belong to a killed
   run), with a WARN line each.
2. Remove `.git/rebase-merge` and `.git/rebase-apply` left by the old code, keeping an
   autostash as a stash entry as today, with a WARN line.
3. If tracked files outside `data/` are modified
   (`git status --porcelain --untracked-files=no -- . ':(exclude)data'`), stop with an
   ERROR naming them. A reset would destroy hand edits to `bin/`.
4. git-crypt state, kept from today's `ensure_unlocked`: if `data/CLAUDE.md` starts with
   the `\0GITCRYPT` header the repo is locked. With a key at `$CLAUDE_SYNC_KEY` (default
   `~/.config/git-crypt/claude-sync.key`) run `git-crypt unlock <key>`; never print the
   key or its contents. Still locked afterwards, or no key: ERROR and stop before reading
   or writing anything under `~/.claude`.
5. Restore the invariant in 6.4.

Ciphertext guard, independent of the check above: any content about to be written into
`~/.claude` or into `data/` that starts with the `\0GITCRYPT` header is refused with an
ERROR and the run stops.

### 6.7 Pull

1. Lock, checks.
2. `git fetch -q origin <BRANCH>` with a 20 s timeout, `GIT_TERMINAL_PROMPT=0`, and ssh
   `BatchMode=yes`, `ConnectTimeout=10`. Failure: classify (6.11); offline gives WARN and
   exit 0, anything else gives ERROR and exit 1. Stop either way.
3. `O` = `origin/<BRANCH>`. If `O == base`, stop: nothing remote to take.
4. Common ancestor `M`: `base` if it is an ancestor of `O`, otherwise
   `git merge-base base O` with an INFO line ("base is not an ancestor of origin": either
   unpushed old-code commits or rewritten remote history). No common ancestor: ERROR,
   stop.
5. Remote changes: `git diff --raw -z --no-renames M O -- data/`. git-crypt encryption is
   deterministic, so blob ids compare correctly without decrypting anything.
6. Remote mass delete guard: if the remote deletes more than 20 files and more than it
   keeps, do not apply its deletions (apply the rest), log ERROR, and tell the user to run
   `CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync pull` if it is intended.
7. For each changed path, with `B` = decrypted content at `M`, `R` = decrypted content at
   `O`, `L` = canonical local content (absent if no file), decide by the table in 6.8.
   Decrypted content comes from `git cat-file --batch --filters` (`<rev>:<path>`), which
   runs the git-crypt smudge filter. Only changed paths are read.
8. `git reset -q --hard O`. This also brings in new code under `bin/` and `claude_sync/`;
   that is the self-update.
9. `git update-ref refs/claude-sync/base O`.
10. Log a summary line. If there were conflicts, kept-over-delete cases or errors, also
    print one short line to stdout (a SessionStart hook's stdout reaches the session).

A run killed anywhere in steps 7 to 9 is safe to repeat: paths already written now have
`L == R` and are skipped, and base has not moved yet.

### 6.8 Decision table for one path

| Local vs base | Remote | Action |
|---|---|---|
| `L == R` | any | nothing |
| `L == B` (unchanged here) | changed | write `R` locally (atomic) |
| `L == B` | deleted | move local file to trash |
| `L` absent, was in base (deleted here) | changed | write `R` locally; WARN "remote edit beats local delete" |
| `L` absent, not in base | added | write `R` locally |
| `L != B` (changed here) | deleted | keep `L`; WARN "local edit beats remote delete" (the next push re-adds it) |
| `L != B` | changed | merge (6.9) |

Race guard: just before replacing a local file, re-stat it; if size or mtime moved since
`L` was read, re-read and decide again (at most 3 times, then treat as a conflict). This
narrows the window where a session's write lands between the read and the rename.

### 6.9 Merge rules

- Text files: `git merge-file -p <L> <B> <R>` (temporary files outside `~/.claude`).
  Exit 0 is a clean merge: write the result locally. The next push publishes it.
- `MEMORY.md` (any project): the same with `--union`, so conflicting hunks keep both
  sides' lines. Check after merging: every `](file.md)` link present in `L` or `R` must be
  in the result; if not, fall back to keep-both.
- `*.json`: the merged result must parse with `json.loads`, otherwise keep-both. Invalid
  JSON is never written.
- Binary files (not in the text list), or text with conflicting hunks: keep-both.
- Keep-both: the local file stays as it is. The remote version, decanonicalised, goes to
  `$REPO/conflicts/<UTC timestamp>/<path relative to ~/.claude>`. Logged as WARN and
  reported on stdout. Because the base then advances, the local version wins on the next
  push, and the remote version is preserved in git history as well as in `conflicts/`.
- No base available (path added on both sides): merge against an empty base by the same
  rules.

### 6.10 Push

1. Lock, checks (the invariant restore makes `data/` equal base).
2. Scan: for every allowlisted local file, compare canonical local bytes with the file in
   the `data/` work tree, which the invariant guarantees is the decrypted base. Also list
   files in `data/` with no local counterpart (local deletes). In-process reads, no forks,
   no `cat-file` calls for unchanged files. If nothing differs: log "no changes" at DEBUG
   level and exit 0 without touching the network. This makes the 5 minute timer cheap.
3. Fetch (as 6.7 step 2). If `O != base`, run pull steps 4 to 9 first, then scan again.
4. Empty-file guard: a 0-byte local file whose base version is not empty is never pushed.
   If its mtime is older than 10 minutes it is restored from base with a WARN (the empty
   file is moved to trash first); a younger one may be a write in progress and is left
   alone.
5. Local mass delete guard (as today): more than 20 deletions and more deletions than
   kept files: refuse with ERROR unless `CLAUDE_SYNC_ALLOW_PRUNE=1`, restore the
   invariant, exit 1.
6. Write changed files into `data/` (atomic), delete removed ones, `git add -A -- data`,
   commit `sync: <ME>@<LocalHostName> <UTC time>` on top of `HEAD` (which is `O`).
7. `git push -q origin HEAD:<BRANCH>` with a 30 s timeout. Rejected because the remote
   moved: go back to step 3, at most 3 attempts in total, then ERROR. Offline: WARN,
   exit 0; the commit is dropped by the next run's invariant restore and rebuilt from
   `~/.claude`.
8. On success, `git update-ref refs/claude-sync/base HEAD`.

History stays linear: every commit's parent is the remote tip it was built on. No rebase,
no autostash, no merge commits.

### 6.11 Errors and logging

- The tool appends to `$REPO/last-sync.log` itself, one line per event:
  `2026-10-25T16:31:02Z pid=1234 pull WARN <message>`. Levels DEBUG (not written unless
  `CLAUDE_SYNC_DEBUG=1`), INFO, WARN, ERROR.
- Each run writes one summary line, for example
  `pull done in 0.8s: 3 remote changes, 2 written, 1 merged, 0 kept-both, 0 trashed`.
- Rotation: above 5 MB the log is renamed to `last-sync.log.1` (one generation).
- Git failures log the command and the last 4 stderr lines (hint lines dropped).
- Offline means the stderr matches a network pattern (could not resolve host, network is
  unreachable, connection timed out or refused, operation timed out, `ssh: connect to
  host`) or the subprocess timed out. Everything else is an ERROR.
- Exit codes: 0 for success, skip or offline; 1 for an error. Hooks keep `|| true`.
- An uncaught exception is logged with its traceback and exits 1.
- stdout notice: plain text, one line, at most 200 characters, only when there is
  something to act on (kept-both conflicts, local-edit-beats-delete, ERROR, or the shim
  finding no usable python). Example:
  `claude-sync: 2 conflicts kept both, remote copies in ~/.claude-sync/conflicts/2026-10-25T16-31-02Z (see last-sync.log)`.
- Retention: at the end of each locked run, folders in `conflicts/` and `trash/` older
  than 30 days are removed.

### 6.12 Commands

- `pull`, `push`: as above.
- `status`: no lock, no network. Prints base, HEAD, `origin/<BRANCH>` from the last fetch,
  how many local changes the scan finds, how many `conflicts/` folders exist, and the last
  summary line from the log.
- `dry-run` (alias `dry`): the push scan only; prints what would be added, changed and
  deleted. No git writes.

memory-reconcile is no longer called from pull or push. The union merge in 6.9 covers the
case it was built for (two machines overwriting each other's index). It stays in `bin/`
for manual use.

### 6.13 Orca and other Claude profiles

- An Orca session runs Claude Code with `CLAUDE_CONFIG_DIR` set to
  `~/Library/Application Support/orca/claude-profiles/<id>/home`. Its hooks therefore
  come from `<id>/home/settings.json`, a separate real file of about 54 KB: the content of
  `~/.claude/settings.json` plus Orca's own hooks. Both existing profiles carry the same
  three claude-sync hooks as `~/.claude/settings.json` today (SessionStart pull 180 s,
  SessionEnd kick 10 s, PostToolUse mark-dirty 45 s).
- `projects`, `skills`, `agents`, `commands` and `hooks` in a profile home are symlinks
  into `~/.claude`, so their content is synced. The profile `settings.json` and
  `CLAUDE.md` are not, and stay out of scope: the settings carry Orca-specific hooks, and
  the profile `CLAUDE.md` is a one-line `@~/.claude/CLAUDE.md` import.
- Consequences handled here: the tool always works on `~/.claude` (6.2). The push timer
  no longer depends on the `.dirty` flag (section 7), because mark-dirty only matches
  `/.claude/projects/` and misses memory writes made through a profile path. The nix
  module keeps the claude-sync hooks correct in every profile `settings.json` as well as
  in `~/.claude/settings.json` (section 7).

### 6.14 Time budgets

Measured on a fixture shaped like the live data (about 750 memory files and 600 skill
files, 17 MB) against a local bare remote:
- push with nothing changed: under 1 s, no network.
- pull with nothing new: under 2 s.
- pull with 50 remote changes: under 5 s.
- push with 50 local changes: under 10 s.
Hook timeouts after section 7: SessionStart 60 s, SessionEnd 10 s (kick only). Lock waits
(20 s pull) plus fetch (20 s) still fit inside 60 s.

## 7. Nix module changes (dotnix, separate branch and worktree)

In `home-manager/claude-sync-hooks.nix`:
1. `ensureScript` keeps the claude-sync hooks correct instead of only adding missing ones.
   It replaces any claude-sync entry that differs from the wanted one, and runs over
   `~/.claude/settings.json` and every regular file matching
   `~/Library/Application Support/orca/claude-profiles/*/home/settings.json`. Symlinks
   (such as `~/.claude-work/settings.json`) are skipped. Writes stay atomic.
2. Wanted hooks:
   - SessionStart: `GIT_TERMINAL_PROMPT=0 "$HOME/.claude-sync/bin/claude-sync" pull 2>>"$HOME/.claude-sync/last-sync.log" || true`,
     timeout 60. stdout is no longer redirected, so conflict notices reach the session.
   - SessionEnd: the existing `.dirty` touch plus `launchctl kickstart` of the push agent,
     timeout 10.
   - PostToolUse mark-dirty: unchanged. Harmless, and removing it is a later cleanup.
3. `claude-sync-push-if-dirty` runs `claude-sync push` on every tick, not only when
   `.dirty` exists or the branch is ahead. The new push exits in under a second with no
   network when nothing changed.
4. Bootstrap activation: unchanged.

These changes work with both the old and the new code, so they can be switched in either
order.

This work is in scope: a branch in a separate worktree of `~/dotnix`, committed, never
switched. Verification without a switch: run the ensure script against temporary copies
of a `~/.claude/settings.json` shape and an Orca profile shape and check the result, and
build (not switch) the home-manager configuration so the module evaluates.

## 8. Testing

All tests use the standard `unittest` module and run under both `/usr/bin/python3` (3.9)
and the nix `python3`: `python3 -m unittest discover -s tests`.

Hermetic harness, enforced in a shared base class:
- A temporary directory per test with its own `HOME`, a bare remote `remote.git` (a
  `file://` path), and one clone per simulated machine. `CLAUDE_SYNC_REPO`,
  `CLAUDE_SYNC_USER` and `HOME` are set for each run, and the tool runs as a subprocess
  through `bin/claude-sync`, as a hook would.
- `GIT_CONFIG_NOSYSTEM=1` and a temporary global gitconfig (user name and email, no hooks
  path), so the real git configuration and the git-lfs pre-push hook are not used.
- The base class refuses to start if any of these resolve under the real home's
  `.claude`, `.claude-sync` or `.config/git-crypt`.
- No network. No test touches `~/.claude`, `~/.claude-sync` or the real key.

Required cases:
1. TO-772 Definition of Done: a local edit made between two syncs survives a pull started
   by another session (a separate process) when the remote did not change that file.
2. Same, while the remote changed another file: that file arrives, the edit stays.
3. Both sides change one memory file in different places: clean merge, both edits kept.
4. Both sides change the same lines: local kept, remote copy in `conflicts/`, stdout
   notice, WARN in the log.
5. MEMORY.md: both sides add entries at the same spot: union, no link lost.
6. settings.json: both sides change it; the result is valid JSON or a kept-both, never
   invalid JSON.
7. Remote delete of an unchanged file goes to trash; of a locally edited file, kept.
8. Local delete is pushed; the other machine moves its copy to trash.
9. Mass delete guard in both directions.
10. Fresh machine (empty `~/.claude`, no base): everything arrives, nothing is deleted.
11. Migration from the old code: no base ref, repo at HEAD, a local edit pending: the edit
    survives and base is set.
12. Two users (`alice`, `bob`): paths and project directory names are rehydrated.
13. Concurrent sessions: 8 processes mixing pull and push on one HOME while another
    process keeps editing files. All exit 0, no edit lost, no lock left, and afterwards
    `HEAD == base` with a clean `data/`.
14. SIGKILL in the middle of a pull and of a push (several kill points): the next run
    succeeds, no 0-byte files, no wedge.
15. Timing: the budgets in 6.14 on the fixture described there.
16. Errors: a broken remote is logged as ERROR with the git message, not as offline;
    unit tests feed the offline classifier sample stderr strings; every log line starts
    with a UTC timestamp.
17. Empty-file guard (young and old 0-byte files).
18. git-crypt round trip with a throwaway key made by `git-crypt init` inside the test
    directory: two machines pull and push, and the blobs in the bare remote are
    ciphertext. Skipped if git-crypt is not installed.
19. Old lock directory: a fresh `.sync.lock.d` makes a run skip; an old one is removed.
20. Self-update: a pull that brings a new `bin/` updates the work tree; a modified
    tracked file in `bin/` stops the run with an ERROR.
21. `CLAUDE_CONFIG_DIR` pointing elsewhere does not change which tree is synced.
22. Locked repo: with no key, a run stops with an ERROR and writes nothing under
    `~/.claude`; content with the git-crypt header is never written locally.
23. Retention: `conflicts/` and `trash/` folders older than 30 days are removed, newer
    ones kept.

## 9. Rollout (each step needs the owner's yes)

1. Review the branch `dfroberg/TO-772-claude-sync-rewrite`.
2. On each Mac, run the old `claude-sync push` once so no local edit is pending. The last
   old-code pull, which brings in the new code, still runs the old `apply()` once.
3. Merge the branch into `main` of `dfroberg/claude-sync` and push. This touches only
   `bin/`, `claude_sync/`, `tests/`, `docs/` and `.gitignore`; `data/` is not changed.
4. The next pull on each Mac (any session start, or `claude-sync pull` by hand) updates
   the code. The first new-code run sets the base (6.4). Check `claude-sync status` and
   the log.
5. Merge the dotnix branch and switch on both Macs.
6. Watch for a week: `find ~/.claude/projects/*/memory -type f -size 0` stays empty, no
   unexpected `conflicts/` folders, no ERROR lines.

Rollback: revert the merge on `main` and push; each Mac returns to the old code on its
next pull. If the new code cannot pull at all:
`git -C ~/.claude-sync fetch origin && git -C ~/.claude-sync reset --hard origin/main`.
The old code ignores `refs/claude-sync/base` and `.sync.lock`.

## 10. Left to the builder

- Module split inside `claude_sync/`, function names, and the exact log wording.
- Whether to add a stat cache to the scan. Only if test 15 misses its budget.
- How the fixture for test 15 is generated.
- Exact kill points for test 14.

## 11. Calls made by the lead, for the owner to check

- Keep git plus git-crypt as the transport.
- memory-reconcile leaves the sync path; it stays as a manual tool.
- Keep-both makes local win at the path; the remote copy goes to `conflicts/` and stays
  in git history.
- Remote edit beats local delete, and local edit beats remote delete.
- Empty local files are never pushed, and are restored from base after 10 minutes.
- Mark-dirty stays for now; the timer stops depending on it.
- SessionStart hook timeout becomes 60 s (live is 180 s), and its stdout reaches the
  session so conflicts are visible.
- The nix ensure step now rewrites outdated claude-sync hook entries, including in Orca
  profile settings files.
- `conflicts/` and `trash/` keep 30 days.
- The SessionStart notice is one plain-text line of at most 200 characters.
