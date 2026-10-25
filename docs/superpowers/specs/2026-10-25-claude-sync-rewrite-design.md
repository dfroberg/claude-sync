# claude-sync rewrite: design (TO-772)

Status: draft for owner approval, revised after the adversarial round (section 12).
Date: 2026-10-25. Jira: TO-772.

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

`.gitignore` gains: `.sync.lock`, `.fresh-machine`, `conflicts/`, `trash/`, `__pycache__/`,
`*.sync-tmp.*`.

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

All comparisons below are on canonical bytes plus the exec bit: local content is
canonicalised before it is compared with repo content, and a file whose bytes match but
whose exec bit differs counts as changed. A chmod-only change is therefore pulled (chmod
locally) and pushed (chmod in `data/`) like any other change. The rewrite is lossy in
the same rare cases as today (a file that already contains the literal `__USER__`
token); that is accepted, not fixed.

### 6.4 State: the base

Each machine keeps a local ref `refs/claude-sync/base`. It is never pushed (push only
sends `HEAD:main`). It always points at a commit, or is absent; it never points at a tree
(`reset`, `merge-base` and `merge-base --is-ancestor` all fail on a tree). Meaning: the
last remote commit whose content this machine has fully taken into `~/.claude`. For any
path, the base version is what both sides last agreed on, which makes it the common
ancestor for a three-way decision.

Invariant when no run holds the lock: `HEAD == base`, and `data/` in the work tree equals
the base tree, with no untracked files. Every run starts by restoring it if a killed run
broke it: if `HEAD != base` or `git status --porcelain -- data` (which lists untracked
files too) is not empty, run `git reset -q --hard base` and then
`git clean -qfdx -- data`. With no base, the same restore runs against `HEAD` (HEAD does
not move). The clean matters: `reset --hard` leaves untracked files from a killed run in
place, and the next `git add -A -- data` would commit them. This is safe because `data/` is
derived and any unpushed local content still lives in `~/.claude`, where the next scan
finds it again.

Pending push. A second local ref, `refs/claude-sync/pending`, is set by push to the commit
it is about to push, just before `git push` (6.10 step 8). On success push moves base to
it and deletes it. When a later run finds it, right after its fetch: if pending is an
ancestor of `O`, the push landed although the pushing run never saw the reply (timeout or
kill), so base becomes pending and the work tree is reset to it. Pending is deleted either
way. Without this, this machine's own landed commit would come back as remote changes and
produce spurious keep-both conflicts.

No base ref (the first run of the new code on a machine, for migration or a new machine):
- The base is never derived from `HEAD`. The old code moves `HEAD` without applying it
  (its push pulls without applying, and a killed or tripwired push stops there), and a new
  machine's bootstrap resets `HEAD` before anything is applied. `HEAD` says nothing
  reliable about `~/.claude`.
- The first pull uses the empty tree as the common ancestor `M`, in the diff only
  (`git diff --raw` from the empty tree id to `O`). Every remote path counts as added, and
  each is compared with the local file by the table in 6.8.
- A path that exists on both sides with different content has no base to merge against,
  so it goes through 6.9's "added on both sides" rule. If that does not merge cleanly,
  which side keeps the place depends on a marker file:
  - No marker (migration of a Mac that already synced with the old code): the local file
    stays in place and the remote copy goes to `conflicts/`. Local edits made since the old
    code last ran are what this tool exists to protect.
  - `$REPO/.fresh-machine` present (a new Mac): the remote file goes in place and the local
    copy goes to `conflicts/`. A new Mac's local files are defaults, for example a
    `settings.json` holding only the hooks the nix ensure step wrote, and pushing them
    would overwrite the real ones on the other Mac. The nix bootstrap creates the marker
    when it clones (section 7); a by-hand setup creates it with `touch` (README). The
    marker is removed when the base is first set, so it survives a first pull that fails
    (for example before the key is installed).
- Files that exist only locally are not in the diff and are not touched; the first push
  publishes them. Files deleted locally since the old code last ran come back from the
  remote, because there is no base to show they were deleted. Both are the safe direction.
- Push with no base runs a full pull (6.7) first. If that pull does not set the base
  (offline, an error, the lock busy), push stops without pushing anything. Nothing is
  pushed until this machine has taken the remote in once.
- The first pull reads every remote file (about 1368 today), so it is slower than a
  normal pull. If a SessionStart hook kills it, nothing is lost (the base has not moved)
  and the timer push, which has no timeout, completes it.

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

Ciphertext guard, independent of the check above: any content read from git as `B` or
`R`, and any content about to be written into `~/.claude` or into `data/`, that starts
with the `\0GITCRYPT` header is refused with an ERROR and the run stops. This guard is
load-bearing, not a backstop: git returns undecrypted content without any warning whenever
the filter does not run (verified, see 6.7 step 7).

### 6.7 Pull

1. Lock, checks.
2. `git fetch -q origin +refs/heads/<BRANCH>:refs/remotes/origin/<BRANCH>` with a 20 s
   timeout, `GIT_TERMINAL_PROMPT=0`, and ssh `BatchMode=yes`, `ConnectTimeout=10`. The
   explicit refspec makes sure `origin/<BRANCH>` is updated; `git fetch origin <BRANCH>`
   alone can leave it stale without an error, depending on the clone's fetch config.
   Failure: classify (6.11); offline gives WARN and exit 0, anything else gives ERROR and
   exit 1. Stop either way.
3. `O` = `origin/<BRANCH>`. Resolve a pending push (6.4). If there is a base and
   `O == base`, stop: nothing remote to take.
4. Common ancestor `M`: with no base, the empty tree (6.4). Otherwise `base` if it is an
   ancestor of `O`, else `git merge-base base O` with an INFO line ("base is not an
   ancestor of origin", which means rewritten remote history). No common ancestor: ERROR,
   stop.
5. Remote changes: `git diff --raw -z --no-renames M O -- data/`. This lists content and
   exec-bit changes. git-crypt encryption is deterministic, so blob ids compare correctly
   without decrypting anything.
6. Remote mass delete guard: if the remote deletes more than 20 files and more than it
   keeps, stop with an ERROR before writing anything, without resetting and without moving
   the base. Every pull and push on this machine then stops the same way, with the stdout
   notice, until the remote is fixed or the user runs
   `CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync pull`, which applies the deletions (into trash).
   Applying only the other changes and moving the base, as an earlier draft did, would
   leave the skipped deletions unreachable and let the next push undo them on the remote.
7. Read contents for the changed paths only. `L` = canonical local content (absent if no
   file). `B` = decrypted content at `M`: read from the `data/` work tree when `M` is the
   base, because the invariant guarantees it holds the decrypted base tree. Every path
   into these steps must therefore start from a restored invariant, including a push
   retry (6.10 step 8). `R`, and `B`
   when `M` is not the base, come from a temporary checkout of those paths into a private
   temporary directory with a temporary index:
   `GIT_ATTR_SOURCE=<rev> GIT_INDEX_FILE=<tmp index> git --work-tree=<tmp dir> checkout <rev> --pathspec-from-file=- --pathspec-file-nul`.
   This runs the git-crypt smudge filter exactly as a normal checkout does, once per file,
   in one git process. The temporary directory is removed when the run ends.
   Verified on git 2.54.0 with a length-changing test filter:
   - `git cat-file --batch --filters` rejects `<rev>:<path>` input
     (`fatal: missing path`), and with `<blob> <path>` input it reports the size before
     filtering while printing the filtered bytes, so the stream cannot be split when the
     filter changes the length, as git-crypt decryption does. Do not use it.
   - Without `GIT_ATTR_SOURCE`, a temporary work tree has no `.gitattributes`, and the
     filter silently does not run.
8. For each changed path, decide by the table in 6.8. Deletions are applied before
   writes, so a file replaced by a directory of the same name, or the reverse, lands
   cleanly. A type clash on one path (`EISDIR` or `ENOTDIR`: a local directory where the
   remote has a file, or a local file where the remote needs a directory) turns that path
   into a keep-both (the remote copy goes to `conflicts/`), and the run continues with the
   next path. If writing that copy fails, the run stops like any other error below, so the
   base never advances past a remote version that was not saved. Any other filesystem
   error (disk full, permission denied, read-only file) stops the run with an ERROR naming
   the path, before the reset and the update-ref, so the base does not move and the next
   run retries; paths already written are then skipped as `L == R`. Parking such a path
   and advancing the base would let the next push send the stale local copy over the other
   Mac's edit. Git failures and the ciphertext guard also stop the whole run.
9. `git reset -q --hard O`. This also brings in new code under `bin/` and `claude_sync/`;
   that is the self-update. Then `git update-ref refs/claude-sync/base O`, and remove
   `.fresh-machine` if present.
10. Log a summary line. If there were conflicts, kept-over-delete cases or errors, also
    print one short line to stdout (a SessionStart hook's stdout reaches the session).

A run killed anywhere before the update-ref in step 9 is safe to repeat: the base has not
moved, paths already written now have `L == R` and are skipped, and the next run's
invariant restore undoes a reset that ran without its update-ref.

### 6.8 Decision table for one path

Equality means the same canonical bytes and the same exec bit.

| Local vs base | Remote | Action |
|---|---|---|
| `L == R` | any | nothing |
| `L == B` (unchanged here) | changed | write `R` locally (atomic) |
| `L == B` | deleted | move local file to trash |
| `L` absent, was in base (deleted here) | changed | write `R` locally; WARN "remote edit beats local delete" |
| `L` absent, not in base | added | write `R` locally |
| `L` present, not in base (added on both sides, and every differing path of a first pull) | added | 6.9 "added on both sides" |
| `L != B` (changed here) | deleted | keep `L`; WARN "local edit beats remote delete" (the next push re-adds it) |
| `L != B` | changed | merge (6.9) |

When only the exec bit differs between `L` and `R`, the bit follows its own three-way
rule: if `L`'s bit equals `B`'s, take `R`'s (chmod); otherwise keep `L`'s. A merged
result gets its exec bit by the same rule. With no `B` (added on both sides, including a
first pull), keep `L`'s bit, except on a machine marked `.fresh-machine`, where `R`'s bit
is taken, matching which side keeps the place in 6.4.

Race guard: just before replacing a local file, re-stat it; if size or mtime moved since
`L` was read, re-read and decide again (at most 3 times, then treat as a conflict). This
narrows the window where a session's write lands between the read and the rename.

### 6.9 Merge rules

- Text files: `git merge-file -p <L> <B> <R>` (temporary files outside `~/.claude`).
  Exit 0 is a clean merge: write the result locally. The next push publishes it.
- `MEMORY.md` (any project): the same with `--union`, so conflicting hunks keep both
  sides' lines. `--union` exits 0 even when hunks conflicted, so its exit code says
  nothing about conflicts. Check after merging: every `](file.md)` link that is in both
  `L` and `R`, or that one side added (absent from `B`), must be in the result. A link
  one side removed on purpose may be gone. If the check fails, fall back to keep-both.
- `*.json`: the merged result must parse with `json.loads`, otherwise keep-both. Invalid
  JSON is never written.
- Binary files (not in the text list), or text with conflicting hunks: keep-both.
- Keep-both: the local file stays as it is. The remote version, decanonicalised, goes to
  `$REPO/conflicts/<UTC timestamp>/<path relative to ~/.claude>`. Logged as WARN and
  reported on stdout. Because the base then advances, the local version wins on the next
  push, and the remote version is preserved in git history as well as in `conflicts/`.
  For a type clash (6.7 step 8) the local side is a directory, so the next push sends the
  directory's files and a delete of the remote file, which the other Mac moves to trash
  by the 6.8 table.
- Added on both sides (no `B` for the path, which includes every differing path in a
  first pull): merge against an empty base by the same rules. Verified with
  `git merge-file`: git narrows the conflict, so lines both sides share are kept once and
  only the differing lines conflict; with `--union` the result is a line union in order.
  If it does not merge cleanly: keep-both, except in a first pull on a machine marked
  `.fresh-machine` (6.4), where the remote file goes in place and the local copy goes to
  `conflicts/` instead.

### 6.10 Push

1. Lock, checks (the invariant restore makes `data/` equal base).
2. No base: run a full pull (6.7) first. If it does not set the base, stop without
   pushing (INFO when offline or the lock is busy, ERROR otherwise).
3. Scan: for every allowlisted local file, compare canonical local bytes and exec bit with
   the file in the `data/` work tree, which the invariant guarantees is the decrypted base.
   Also list files in `data/` with no local counterpart (local deletes). In-process reads,
   no forks, no git calls for unchanged files. If nothing differs: log "no changes" at
   DEBUG level and exit 0 without touching the network. This makes the 5 minute timer
   cheap.
4. Fetch (as 6.7 step 2) and resolve a pending push (6.4). If `O != base`, run pull steps
   4 to 9 first, then scan again. If the remote mass delete guard stops that pull, push
   stops too.
5. Empty-file guard: a 0-byte local file whose base version is not empty is never pushed.
   If its mtime is older than 10 minutes it is restored from base with a WARN (the empty
   file is moved to trash first); a younger one may be a write in progress and is left
   alone.
6. Local mass delete guard (as today): more than 20 deletions and more deletions than
   kept files: refuse with ERROR unless `CLAUDE_SYNC_ALLOW_PRUNE=1`, restore the
   invariant, exit 1.
7. Write changed files into `data/` (atomic, through temporary names matching
   `*.sync-tmp.*`), set their exec bit, delete removed ones, `git add -A -- data`, commit
   `sync: <ME>@<LocalHostName> <UTC time>` on top of `HEAD` (which is `O`).
8. `git update-ref refs/claude-sync/pending HEAD`, then `git push -q origin HEAD:<BRANCH>`
   with a 30 s timeout. Rejected because the remote moved: first restore the invariant
   (`git reset -q --hard base`, `git clean -qfdx -- data`) and delete pending, then go
   back to step 4, at most 3 attempts in total, then ERROR. The restore is required, not
   tidying: step 7 wrote the local versions into `data/`, and the pull steps read `B` from
   `data/` (6.7 step 7), so without it a local edit would read as unchanged and be
   overwritten by the remote version with no merge and no copy. Offline or timed out:
   WARN, exit 0. The next run either finds the push landed (pending check in 6.4, base
   moves to it) or drops the commit in its invariant restore and rebuilds it from
   `~/.claude`.
9. On success, `git update-ref refs/claude-sync/base HEAD` and delete pending.

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
- `status`: no lock, no network. Prints base (or "none"), HEAD, `origin/<BRANCH>` from the
  last fetch, whether a pending push or `.fresh-machine` exists, how many local changes the
  scan finds, how many `conflicts/` folders exist, and the last summary line from the log.
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
files, 17 MB) against a local bare remote, with git-crypt active (a throwaway key made
inside the test directory), because the git-crypt filter, one process per file read or
written, is the slow part:
- push with nothing changed: under 1 s, no network.
- pull with nothing new: under 2 s.
- pull with 50 remote changes: under 5 s.
- push with 50 local changes: under 10 s.
- first pull with no base (every file read): under 60 s. A kill there is harmless (6.4).
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
4. Bootstrap activation (`bootstrapClaudeSync`, runs only when the repo has no commit
   yet): create `${syncRepo}/.fresh-machine` right after its `reset --hard`, before its
   first `claude-sync pull` (6.4). Otherwise unchanged.

Order matters: switch this on a Mac only after the new code is running there (rollout
step 5). With the old code, a push on every timer tick runs the old 39 s collect and
memory-reconcile every 5 minutes, and a 60 s SessionStart timeout can kill an old pull in
the middle of its rebase, which is the wedge in section 2 item 3.

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
9. Mass delete guard in both directions. Remote: the pull stops before writing anything,
   the base does not move, push stops too, and `CLAUDE_SYNC_ALLOW_PRUNE=1` applies the
   deletions into trash. Local: push refuses; the override pushes.
10. New machine: `.fresh-machine` present, no base, a local `settings.json` holding only
    hooks and a local-only memory file. The remote `settings.json` goes in place, the
    local one is in `conflicts/`, everything else arrives, nothing is deleted, the
    local-only file is pushed later, and the marker is gone once the base is set. A first
    pull that fails (no key) leaves the marker, and the next plain pull still treats the
    machine as new. A push before the first successful pull pushes nothing.
11. Migration from the old code: no base ref, no marker, `HEAD` at a commit that was
    fetched but never applied (as the old push leaves it), a local edit pending, and a
    remote change to another file that this machine never applied. The local edit
    survives in place. The other file differs on both sides with no base: the local copy
    stays, the remote copy is in `conflicts/`, and the stdout notice says so. The base
    ends at `O`.
12. Two users (`alice`, `bob`): paths and project directory names are rehydrated.
13. Concurrent sessions: 8 processes mixing pull and push on one HOME while another
    process keeps editing files. All exit 0, no edit lost, no lock left, and afterwards
    `HEAD == base` with a clean `data/`.
14. SIGKILL in the middle of a pull and of a push (several kill points): the next run
    succeeds, no 0-byte files, no wedge.
15. Timing: the budgets in 6.14 on the git-crypt fixture described there. Without
    git-crypt installed the test is skipped and reported as skipped, never as passed.
16. Errors: a broken remote is logged as ERROR with the git message, not as offline;
    unit tests feed the offline classifier sample stderr strings; every log line starts
    with a UTC timestamp.
17. Empty-file guard (young and old 0-byte files).
18. git-crypt round trip with a throwaway key made by `git-crypt init` inside the test
    directory: two machines pull and push, the blobs in the bare remote are ciphertext,
    and a merge reads `B` and `R` as plaintext. With the filter attribute removed, the
    ciphertext guard stops the run before anything is written. Skipped if git-crypt is
    not installed.
19. Old lock directory: a fresh `.sync.lock.d` makes a run skip; an old one is removed.
20. Self-update: a pull that brings a new `bin/` updates the work tree; a modified
    tracked file in `bin/` stops the run with an ERROR.
21. `CLAUDE_CONFIG_DIR` pointing elsewhere does not change which tree is synced.
22. Locked repo: with no key, a run stops with an ERROR and writes nothing under
    `~/.claude`; content with the git-crypt header is never written locally.
23. Retention: `conflicts/` and `trash/` folders older than 30 days are removed, newer
    ones kept.
24. One bad path. Type clash: the remote adds a file where a local directory of that name
    sits. That path becomes a keep-both, the other changed paths are applied, the base
    advances, and the next pull runs clean; the next push sends the directory, and the
    other machine moves its file to trash. Other error: a write fails for another reason
    (for example a read-only target directory). The run stops with an ERROR naming the
    path, the base does not move, nothing is parked, and once the cause is removed the
    next pull completes with no edit lost on either machine.
25. Exec bit: a chmod-only change on either machine reaches the other; a merge keeps the
    bit by the rule in 6.8; with no base, a both-sides bit difference keeps the local bit,
    or the remote bit on a `.fresh-machine` Mac.
26. Leftovers: an untracked file and a `*.sync-tmp.*` file left in `data/` by a killed run
    are removed by the next run and never committed.
27. Push landed but unseen: the remote has this machine's commit, pending is set, and the
    base was not moved (as after a timeout or a kill). The next run moves the base to it,
    with no keep-both and no stdout notice.
28. MEMORY.md removals: one side removes an entry while the other adds one. The removal
    stays removed, the addition is kept, and there is no keep-both.
29. Stale tracking ref: `origin/<BRANCH>` is rewound by hand in the clone; the next pull
    still sees the remote tip.
30. Rejected push: machine A has a local edit to a file and is pushing; machine B pushes a
    change to the same file after A's fetch and before A's push lands, so A's push is
    rejected. A's edit is not overwritten: the result is a clean merge, or a keep-both
    with the stdout notice. A second variant where B changed a different file ends with
    both changes on the remote and on both machines.

## 9. Rollout (each step needs the owner's yes)

Code first, nix second, never the other way round (section 7).

1. Review the branch `dfroberg/TO-772-claude-sync-rewrite`.
2. Run the old `claude-sync push` once on the main Mac (EZ-MBP-GD4RPHKWL4) only, right
   before step 3, so its local files and the remote agree. Anything that still differs
   on both sides at its first new-code run has no base to merge against: the local copy
   stays and the remote copy goes to `conflicts/` (6.4). The last old-code pull, which
   brings in the new code, still runs the old `apply()` once.
   Do not run the old push on the second Mac (Dannys-MacBook-Pro, 5 of the 3596 sync
   commits): the old push never applies, so it would send that Mac's stale copies over
   the main Mac's (section 2 item 2). Its 5 minute timer runs the old push only when
   `~/.claude-sync/.dirty` exists, so before step 3 remove that file there, or keep that
   Mac asleep with no Claude session, until its step 4. Its next session start then runs
   the old pull one last time, which applies the remote over its local files as the old
   code always has, and brings in the new code; its first new-code run finds local equal
   to the remote. If it holds local work that must survive, copy that work aside before
   that session start.
3. Merge the branch into `main` of `dfroberg/claude-sync` and push. This touches only
   `bin/`, `claude_sync/`, `tests/`, `docs/`, `README.md` (new-machine steps gain the
   `touch .fresh-machine` line) and `.gitignore`; `data/` is not changed.
4. On each Mac, once a pull has brought in the new code (any session start), run
   `claude-sync pull` by hand so the first new-code run happens at once and sets the
   base. Check `claude-sync status`, the log and any notice.
5. Only on a Mac where step 4 is done: merge the dotnix branch and switch there.
6. Watch for a week: `find ~/.claude/projects/*/memory -type f -size 0` stays empty, no
   unexpected `conflicts/` folders, no ERROR lines.

Rollback: if the nix change was switched, switch back to the previous home-manager
generation first (the old code with the new hooks is the bad pairing in section 7). Then
revert the merge on `main` and push; each Mac returns to the old code on its next pull. If
the new code cannot pull at all:
`git -C ~/.claude-sync fetch origin && git -C ~/.claude-sync reset --hard origin/main`.
The old code ignores `refs/claude-sync/base`, `refs/claude-sync/pending`, `.sync.lock`
and `.fresh-machine`.

## 10. Left to the builder

- Module split inside `claude_sync/`, function names, and the exact log wording.
- Whether to add a stat cache to the scan. Only if test 15 misses its budget.
- How the fixture for test 15 is generated.
- Exact kill points for test 14.
- How tests 27 and 30 force their race (for example setting the refs directly for 27, and
  a `pre-push` hook in A's test clone that first pushes from B for 30).

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
- First run with no base (added after the adversarial round): on a Mac that already
  synced with the old code, the local copy keeps the place and the remote copy is parked;
  on a new Mac marked `.fresh-machine` by the nix bootstrap, the remote copy keeps the
  place and the local one is parked. Either way both copies survive and the notice says
  so. The alternative, one rule for both cases, either pushes a new Mac's default
  `settings.json` over the real one or parks a migrating Mac's pending edits.
- A remote mass delete stops all syncing on that Mac, loudly, until the remote is fixed or
  the override is run.
- A path where a directory sits in the way of a file, or the reverse, becomes a keep-both
  and the run continues. Any other write failure stops the run before the base moves, so
  it is retried and stays loud instead of being parked.
- Rollout: the old push runs on the main Mac only. The second Mac takes the new code
  through one last old pull, which overwrites its local files with the remote as the old
  code always has.

## 12. Adversarial review

grok-4.7 could not be used: `XAI_API_KEY` is not set in this session or in a login shell,
so xAI was unreachable. Per the owner's rule the round ran on Fable instead, on
2026-10-25, against the draft committed as e8f137e1. The reviewer tested its claims with
small git experiments in a scratch directory. Verdict: NEEDS FIXES (1 critical, 6 major,
5 minor). All twelve findings were accepted; none changed the chosen approach.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | Critical | Setting the base to `HEAD` at migration is unsafe: the old push moves `HEAD` without applying it, so local stale files would beat unapplied remote changes. The fresh-machine check by "no synced files present" never fires, because files such as `settings.json` exist before the first pull. | 6.4: the base is never derived from `HEAD`. A first pull diffs from the empty tree; both-sides differences are kept both, with the place decided by the `.fresh-machine` marker that the nix bootstrap writes. Push does nothing until a pull has set the base. Tests 10 and 11. |
| 2 | Major | The base cannot be the empty tree: `reset`, `merge-base` and `--is-ancestor` fail on a tree. | 6.4: the base is a commit or absent; the empty tree is used only as a diff side. |
| 3 | Major | `git cat-file --batch --filters` rejects `<rev>:<path>`, and without the filter it returns ciphertext silently. | 6.7 step 7: temporary checkout with `GIT_ATTR_SOURCE`. While verifying, a further problem was found: with `<blob> <path>` input the batch header gives the size before filtering, so the stream cannot be split once git-crypt changes the length. `cat-file --batch --filters` is not used at all. The ciphertext guard now also covers content read from git (6.6). Test 18. |
| 4 | Major | The remote mass delete guard skipped deletions but still moved the base, so the deletions became unreachable and the next push undid them. | 6.7 step 6: stop before any write, reset or base move; push stops too. Test 9. |
| 5 | Major | One path that cannot be written (a file where a directory sits) would fail every pull from then on. | 6.7 step 8: a type clash becomes a keep-both and the run continues (narrowed by the re-check, new defect 2: any other error stops the run). Test 24. |
| 6 | Major | The exec bit no longer travelled, because comparisons looked at bytes only. | 6.3, 6.8, 6.10: comparisons include the exec bit, with a three-way rule for the bit. Test 25. |
| 7 | Major | `reset --hard` leaves untracked files in `data/`, which the next `git add -A` would commit. | 6.4: the restore also runs `git clean -qfdx -- data`; temporary files use `*.sync-tmp.*`, which is ignored. Test 26. |
| 8 | Minor | An own push that landed but timed out would come back as remote changes and produce spurious keep-both conflicts. | 6.4 and 6.10: `refs/claude-sync/pending`, resolved after the next fetch. Test 27. |
| 9 | Minor | The MEMORY.md link check required every link from either side, so a deliberate removal forced a keep-both. `--union` also exits 0 on conflicts. | 6.9: only links both sides still have, or that one side added, must survive; the exit code of `--union` is not used. Test 28. |
| 10 | Minor | `git fetch origin main` can leave `origin/main` stale. | 6.7 step 2: explicit refspec. Test 29. |
| 11 | Minor | Switching nix before the new code makes the old code worse (a push every 5 minutes, and a 60 s timeout that can kill an old rebase). | Section 7 and section 9: code first, then nix per Mac; rollback reverses nix first. |
| 12 | Minor | The time budgets did not include git-crypt, the slow part. | 6.14 and test 15: budgets measured with git-crypt on, plus a first-pull budget. |

Verification commands run by the lead for findings 1 and 3 (scratch repo, test filter
`sed s/$/XXXX/`, git 2.54.0):
- `echo "HEAD:data/a.txt" | git cat-file --batch --filters` printed
  `fatal: missing path for '<sha>'`.
- `printf '<sha> data/a.txt\n' | git cat-file --batch --filters` printed the header
  `<sha> blob 6` followed by the 10 filtered bytes `helloXXXX` and a newline.
- A temporary checkout without `GIT_ATTR_SOURCE` wrote `hello` (no filter). With
  `GIT_ATTR_SOURCE=HEAD` it wrote `helloXXXX`.
- `git merge-file -p --union` of two index files against an empty base kept the shared
  lines once and both added lines, exit 0. The same without `--union` conflicted only on
  the differing lines, exit 1.

### Re-check of the fixes

One Fable pass on the fixes only (the diff from e8f137e1 to bc22a81c), 2026-10-25. It
confirmed all twelve findings resolved, and re-ran the temporary checkout experiment
(filter ran with `GIT_ATTR_SOURCE`, mode 755 kept, real work tree untouched; without it,
silent plaintext). It also checked and cleared: empty-tree `diff --raw` lists adds and
modes correctly; a killed first pull is safe to repeat; pending survives the invariant
restore; the remote mass delete guard cannot fire in a first pull. Verdict: NEEDS FIXES,
for four new defects introduced by the fixes, all accepted:

| # | Severity | Defect | Resolution |
|---|---|---|---|
| N1 | Critical | A rejected push went back to the pull steps with the local versions still written in `data/`. The pull reads `B` from `data/`, so a local edit read as unchanged and was overwritten by the remote version with no merge and no copy. | 6.10 step 8: restore the invariant and delete pending before every retry; 6.7 step 7 states that every entry into the pull steps starts from a restored invariant. Test 30. |
| N2 | Major | Turning every filesystem error into a keep-both while the base advanced let a transient failure (disk full, read-only file) leave a stale local file that the next push sent over the other Mac's edit. | 6.7 step 8: keep-both only for a type clash (`EISDIR`, `ENOTDIR`); any other error stops the run before the base moves. 6.9 states what the next push does after a type clash. Test 24. |
| N3 | Minor | The exec-bit rule had no answer with no `B` (added on both sides). | 6.8: keep `L`'s bit, or `R`'s on a `.fresh-machine` Mac. Test 25. |
| N4 | Minor | Rollout step 2 ran the old push on both Macs, but the old push never applies, so the second Mac would push its stale copies over the main Mac's. | Section 9 step 2: the old push runs on the main Mac only; the second Mac takes the new code through one last old pull. The reviewer suggested ordering both pushes instead; not taken, because with two old pushes the later one still overwrites the remote with copies that Mac never applied. |
