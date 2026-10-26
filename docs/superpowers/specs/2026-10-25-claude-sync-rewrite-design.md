# claude-sync rewrite: design (TO-772)

Status: approved by the owner on 2026-10-25, then revised after the grok-4.7 adversarial
round (section 12). The changes that alter approved behaviour are listed at the end of
section 11 for the owner to check.
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

The shim keeps the existing PATH loop, changes to the repo root it lives in, and runs
`python3 -E -s -B -m claude_sync` there: the package is not installed anywhere, `-m`
would otherwise put the hook's project directory first on `sys.path` (a `json.py` there
would shadow the standard library), `-E` ignores `PYTHONPATH`, and `-B` stops bytecode
files. It uses the first candidate that exists and reports Python 3.9 or newer, trying in
order: `/etc/profiles/per-user/$USER/bin/python3`, `$HOME/.nix-profile/bin/python3`,
`/run/current-system/sw/bin/python3`, `/opt/homebrew/bin/python3`, then `/usr/bin/python3`
only if `xcode-select -p` succeeds (otherwise that stub opens an install dialog). With none
found it appends a timestamped ERROR to `last-sync.log` in the repo root, prints the
one-line notice from 6.11 to stdout, and exits 0, so a hook never breaks a session but the
session still sees the problem.

`.gitignore` gains, anchored at the repo root so the same names under `data/` still sync:
`/.sync.lock`, `/.fresh-machine`, `/conflicts/`, `/trash/`, `/claude_sync/__pycache__/`,
`/tests/__pycache__/`, `/last-sync.log.1`, `/.hold-*`; and `*.sync-tmp.*` at any depth
(round 3 re-check, item 4: `data/` holds `__pycache__` folders under `skills/` today).

### 6.2 Paths and environment

- `CLAUDE` = `$HOME/.claude`. `CLAUDE_CONFIG_DIR` is ignored on purpose: Orca and the work
  profile set it, but the synced tree is always `~/.claude` (their `projects/`, `skills/`
  and so on are symlinks into it).
- `REPO` = `$CLAUDE_SYNC_REPO` or `$HOME/.claude-sync`. `DATA` = `$REPO/data`.
- `ME` = `$CLAUDE_SYNC_USER` or the login name. The override exists for tests.
- `BRANCH` = `$CLAUDE_SYNC_BRANCH` or `main`.
- Every git command runs as `git -C $REPO`, and git-crypt runs with `$REPO` as its working
  directory, with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_PREFIX`,
  `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_COMMON_DIR`,
  `GIT_NAMESPACE`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`, every `GIT_CONFIG_KEY_*` and
  `GIT_CONFIG_VALUE_*`, `GIT_ASKPASS` and `SSH_ASKPASS` removed from its environment: hooks
  start in the user's project, which is itself a git repo, and may inherit them (a parent
  `git -c` exports `GIT_CONFIG_PARAMETERS`, which could replace the git-crypt filters).
  An inherited `GIT_SSH_COMMAND` is kept, as the user's own choice; without one, the tool
  sets `ssh -o BatchMode=yes -o ConnectTimeout=10`. Commands are argument lists, never
  shell strings.

### 6.3 What syncs and how paths are rewritten

Unchanged from today:
- Files `CLAUDE.md`, `settings.json`, `mcp_settings.json`, `keybindings.json`; trees
  `skills/`, `agents/`, `commands/`, `hooks/`; and `projects/<proj>/memory/**` stored as
  `data/memory/<canonical proj>/**`.
- Regular files only. A local path that is a symlink, or passes through a symlinked
  directory, is outside the sync on this machine in both directions: never collected,
  never counted as a local delete, and never read, written or trashed by pull (one INFO
  line per run gives the count). Pull therefore never writes outside `~/.claude`. Today
  none of the four top-level files is a symlink, and three skill folders are (Orca's,
  linked into `~/.agents/skills`, not in the repo). Files named `*.sync-tmp.*` are
  ignored.
- A repo path with an empty, `.` or `..` component, or one that would resolve outside
  its root (`~/.claude`, `conflicts/`, `trash/` or `data/`), is refused with an ERROR.
- Text files (same extension list as `is_text` today) are canonicalised on the way in:
  `/Users/<ME>/` becomes `/Users/__USER__/` and `-Users-<ME>-` becomes
  `-Users-__USER__-`, and the reverse on the way out (every write into `~/.claude`,
  including restores and parked copies). Project directory names use the same rule. Other
  files are copied byte for byte. Canonicalisation is a byte replacement; content is never
  decoded as text, so invalid UTF-8 cannot raise.
- The exec bit travels (git records it). Other mode bits do not.
- Entries in `data/` are regular files (mode 100644 or 100755). A remote entry of any
  other mode (a symlink or a submodule, which `git diff --raw` shows with mode 120000 or
  160000, usually as status `T`) stops the run with an ERROR naming it. Neither the old nor
  the new code creates such entries.
- Every write into `~/.claude`, `data/`, `conflicts/` or `trash/` creates missing parent
  directories first. Writes into `~/.claude` and `data/` are atomic (a temporary file named
  `*.sync-tmp.*` in the same directory, then a rename).

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
the base tree, with no untracked or ignored files. Every locked run starts by restoring it
in case a killed run broke it: if `HEAD != base` or `git status --porcelain -- data`
(which lists untracked files too) is not empty, run `git reset -q --hard base`; then
always run `git clean -qfdx -- data`, because the porcelain check does not show ignored
leftovers such as `*.sync-tmp.*`. With no base, the same restore runs against `HEAD` (HEAD
does not move). The clean matters: `reset --hard` leaves untracked files from a killed run
in place, and the next `git add -A -- data` would commit them. This is safe because `data/` is
derived and any unpushed local content still lives in `~/.claude`, where the next scan
finds it again.

Pending push. A second local ref, `refs/claude-sync/pending`, is set by push to the commit
it is about to push, just before `git push` (6.10 step 8). On success push moves base to
it and deletes it. Whichever run fetches next resolves it, right after its fetch and
before anything reads `data/`:
1. `P` = the commit pending points at.
2. If `P` is an ancestor of `O`, the push landed although the pushing run never saw the
   reply (timeout or kill): `git reset -q --hard P`, then
   `git update-ref refs/claude-sync/base P`.
3. `git update-ref -d refs/claude-sync/pending`, whether or not step 2 ran.

The ref is deleted last, so a kill between these steps leaves pending in place and the
next run repeats them. Without this, this machine's own landed commit would come back as
remote changes and produce spurious keep-both conflicts.

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
  - `$REPO/.fresh-machine` present (a new Mac): the local copy is saved to `conflicts/`
    first, then the remote file goes in place (6.9). A new Mac's local files are defaults, for example a
    `settings.json` holding only the hooks the nix ensure step wrote, and pushing them
    would overwrite the real ones on the other Mac. The nix bootstrap creates the marker
    when it clones (section 7); a by-hand setup creates it with `touch` (README). The
    marker is removed when the base is first set, so it survives a first pull that fails
    (for example before the key is installed).
- Files that exist only locally are not in the diff and are not touched; the first push
  publishes them. Files deleted locally since the old code last ran come back from the
  remote, because there is no base to show they were deleted. Both are the safe direction.
- Push with no base runs the pull steps (6.7 steps 2 to 9) first, in the same process
  and under the lock push already holds; it never takes the lock a second time. If they
  do not set the base (offline, an error, a guard), push stops without pushing anything.
  Nothing is pushed until this machine has taken the remote in once.
- The first pull reads every remote file (about 1368 today), so it is slower than a
  normal pull and may not finish inside the 60 s SessionStart hook. If the hook kills it,
  nothing is lost (the base has not moved) and the next timer push, which has no timeout,
  completes it through the in-process pull above.
- Keep-both at a first pull has a cost the other Mac pays. On a Mac without the marker the
  local copy stays and the base moves to `O`, so the next push publishes the local copy.
  The other Mac's file still equals its base there, so it takes the published copy with
  no merge and no notice. Its own version then lives only in git history and in this
  Mac's `conflicts/` (30 days), and only this Mac's notice mentions it. This is the
  owner's keep-both rule (section 11) applied to the first run; rollout step 2 keeps such
  paths near zero.

### 6.5 Lock

- `fcntl.flock` on `$REPO/.sync.lock`, opened with create and read-write. The kernel
  releases it when the process dies, including on SIGKILL, so it cannot go stale. One run
  takes it once; nothing inside a run takes it again.
- Waits with a budget, then skips with an INFO log line and exit 0: pull 20 s, push 60 s,
  status none (it does not take the lock), dry-run 20 s. Overridable through
  `CLAUDE_SYNC_LOCK_WAIT`.
- Transition guard: while the old `.sync.lock.d` directory exists and is younger than
  900 s, an old-code run may be active, so the new code treats the lock as busy. An
  older one is removed with a WARN line. 900 s is the old code's own stale limit
  (`CLAUDE_SYNC_LOCK_STALE`, bin/claude-sync line 149), so an old run would remove it at
  the same age.

### 6.6 Repo checks at the start of every locked run

1. Remove `.git/index.lock`, `.git/HEAD.lock`, ref `*.lock` files and
   `.git/packed-refs.lock` older than 60 s (we hold the lock, so they belong to a killed
   run), with a WARN line each. When this run kills a git command at its timeout, it
   removes those lock files at once, whatever their age, since the killed child was the
   only git process allowed to hold them. A `.git/index.lock` younger than 60 s that is
   still there afterwards means another git process is running: a git child runs in its
   own session, so it can outlive a run killed by a hook timeout and still be rewriting
   `data/`. The run then logs INFO `git busy` and exits 0 without reading or writing
   anything (round 3, finding 2). A `.git/index.lock` of 60 s or older that is still
   there (the removal failed, for example a root-owned file) stops the run with an ERROR
   asking for it to be removed by hand, so it never turns into a silent skip on every
   run.
2. Remove `.git/rebase-merge` and `.git/rebase-apply` left by the old code, with a WARN
   line. If the directory holds an `autostash` file, first run
   `git stash store -m "autostash of a killed sync run" <its sha>` (as bin/claude-sync
   line 238 does today); a failed store is logged as WARN and does not stop the removal.
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
with the `\0GITCRYPT` header is refused with an ERROR and the run stops. The check runs on
the raw bytes, before canonicalisation. This guard is load-bearing, not a backstop: git
returns undecrypted content without any warning whenever the filter does not run
(verified, see 6.7 step 7).

### 6.7 Pull

1. Lock, checks.
2. `git fetch -q origin +refs/heads/<BRANCH>:refs/remotes/origin/<BRANCH>` with a 20 s
   timeout, `GIT_TERMINAL_PROMPT=0`, and ssh `BatchMode=yes`, `ConnectTimeout=10`. The
   explicit refspec makes sure `origin/<BRANCH>` is updated; `git fetch origin <BRANCH>`
   alone can leave it stale without an error, depending on the clone's fetch config.
   Failure: classify (6.11); offline gives WARN and exit 0, anything else gives ERROR and
   exit 1. Stop either way.
3. `O` = `origin/<BRANCH>`. Resolve a pending push (6.4), then read the base again. If
   there is a base and `O == base`, stop: nothing remote to take.
4. Common ancestor `M`: with no base, the empty tree (6.4); otherwise always `base`. The
   base tree is exactly what this machine last agreed with, and a tree diff needs no
   ancestry, so this holds even if the remote history was rewritten. In that case (base
   is not an ancestor of `O`) log one INFO line, "remote history was rewritten", and go
   on. No merge-base is computed.
5. Remote changes: `git diff --raw -z --no-renames M O -- data/`. This lists content and
   exec-bit changes. git-crypt encryption is deterministic, so blob ids compare correctly
   without decrypting anything.
6. Remote mass delete guard: if the remote deletes more than 20 files and the deletions
   also outnumber the files left under `data/` in `O`, stop with an ERROR before writing
   anything, without resetting and without moving the base. (The same predicate guards
   push in 6.10 step 6, counted against the tree about to be committed. A renamed folder
   counts as deletes plus adds, but cannot outnumber what remains, so it passes.) Every
   pull and push on this machine then stops the same way, with the stdout
   notice, until the remote is fixed or the user runs
   `CLAUDE_SYNC_ALLOW_PRUNE=1 claude-sync pull`, which applies the deletions (into trash).
   Applying only the other changes and moving the base, as an earlier draft did, would
   leave the skipped deletions unreachable and let the next push undo them on the remote.
7. Read contents for the changed paths only. `L` = canonical local content (absent if no
   file). A local path that exists but cannot be read or listed is an ERROR that stops
   the run; only a path that is confirmed absent counts as absent. `B` = decrypted
   content at `M`: absent with no base, otherwise read from the `data/` work tree, because
   the invariant guarantees it holds the decrypted base tree. Every path into these steps
   must therefore start from a restored invariant, including a push retry (6.10 step 8).
   `R` comes from a temporary checkout of those paths into a private temporary directory
   with a temporary index:
   `GIT_ATTR_SOURCE=<rev> GIT_INDEX_FILE=<tmp index> git -C $REPO --work-tree=<tmp dir> checkout <rev> --pathspec-from-file=- --pathspec-file-nul`.
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
    Every stop with an ERROR, in any command, prints the notice too before exit 1.

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

"Write `R` locally" always means: decanonicalise `R` (6.3), then write it atomically. "Move
to trash" means a move to `$REPO/trash/<stamp>/<path relative to ~/.claude>`, where
`<stamp>` is the run's start time in UTC as `2026-10-25T16-31-02Z` (no colons); all of a
run's conflicts and trash share one stamp. A path that is a symlink locally, or passes
through one, is not in this table at all (6.3).

When only the exec bit differs between `L` and `R`, the bit follows its own three-way
rule: if `L`'s bit equals `B`'s, take `R`'s (chmod); otherwise keep `L`'s. A merged
result gets its exec bit by the same rule. With no `B` (added on both sides, including a
first pull), keep `L`'s bit, except on a machine marked `.fresh-machine`, where `R`'s bit
is taken, matching which side keeps the place in 6.4.

Race guard: just before replacing a local file or moving it to trash, re-stat it; if it
appeared, disappeared, or its size or mtime moved since `L` was read, re-read it
(canonicalised, like any `L`) and decide again. After 3 such attempts the path becomes a
keep-both: the local file is left as it is and the remote version is parked. When the
remote deleted the path there is nothing to park, so the local file is kept as a local
edit beating a remote delete. Reads never follow a symlink, so a path that turns into
one after the check stops the run instead of reading outside `~/.claude`. This narrows
the window where a session's write lands between the read and the rename. If the scan of
a later push reads a file in the middle of a session's write, that push may publish the
partial content; the push after the write completes publishes the full file. This risk is
the same as today's and is not solved here.

### 6.9 Merge rules

- Text files: `git merge-file -p <L> <B> <R>` (temporary files outside `~/.claude`).
  Exit 0 is a clean merge: write the result locally. The next push publishes it. Exit 1
  to 127 is a conflict count. Any other exit (git reports its own errors as 255, verified
  for a missing input and a binary input) is an ERROR that stops the run before the base
  moves, never a keep-both.
- `MEMORY.md` (any project): the same with `--union`, so conflicting hunks keep both
  sides' lines. `--union` exits 0 even when hunks conflicted, so its exit code says
  nothing about conflicts. Check after merging: every `](file.md)` link that is in both
  `L` and `R`, or that one side added (absent from `B`), must be in the result. A link
  one side removed on purpose may be gone. If the check fails, fall back to keep-both.
- `*.json`: the merged result must parse with `json.loads` with duplicate keys rejected
  (an `object_pairs_hook` that raises on a repeated key), otherwise keep-both. A line
  merge in which both sides added the same key would otherwise pass with one value
  silently dropped. A merged JSON result is never written unless it passed this check.
  A remote version taken whole (the local file unchanged) is written as the other Mac
  has it, so a `.json` file with comments, which strict parsing rejects, still syncs.
- Binary files (not in the text list), or text with conflicting hunks: keep-both.
- Keep-both: the local file stays as it is. The remote version, decanonicalised, goes to
  `$REPO/conflicts/<stamp>/<path relative to ~/.claude>` (stamp as in 6.8). Logged as
  WARN and reported on stdout. Because the base then advances, the local version wins on
  the next push, and the remote version is preserved in git history as well as in
  `conflicts/`. On the other Mac the published local version replaces its file with no
  notice there; that is the owner's keep-both rule (section 11).
  For a type clash (6.7 step 8) the local side is a directory, so the next push sends the
  directory's files and a delete of the remote file, which the other Mac moves to trash
  by the 6.8 table.
- Added on both sides (no `B` for the path, which includes every differing path in a
  first pull): merge against an empty base by the same rules. Verified with
  `git merge-file`: git narrows the conflict, so lines both sides share are kept once and
  only the differing lines conflict; with `--union` the result is a line union in order.
  If it does not merge cleanly: keep-both, except in a first pull on a machine marked
  `.fresh-machine` (6.4), where the local file is first copied (not moved) to
  `conflicts/`, and only once that copy is written does the remote file go in place. A
  failed copy stops the run like any other write error (6.7 step 8), so the local file is
  never replaced without a saved copy.

### 6.10 Push

1. Lock, checks (the invariant restore makes `data/` equal base).
2. No base: run pull steps 2 to 9 first, in this process under the lock already held
   (6.4). If they do not set the base, stop without pushing (WARN when offline, ERROR
   otherwise).
3. Scan: for every allowlisted local file, compare canonical local bytes and exec bit with
   the file in the `data/` work tree, which the invariant guarantees is the decrypted base.
   Also list files in `data/` with no local counterpart (local deletes). Only a path
   confirmed absent is a delete: a file or folder that cannot be read or listed is an
   ERROR that stops the run before any delete or commit. Symlinked paths are skipped
   (6.3). In-process reads, no forks, no git calls for unchanged files. If nothing
   differs: log "no changes" at DEBUG level and exit 0 without touching the network. This
   makes the 5 minute timer cheap. A pending push left by an earlier run is then resolved
   by the next run that fetches.
4. Fetch (as 6.7 step 2) and resolve a pending push (6.4), then read the base again. If
   `O != base`, run pull steps 4 to 9 first, in this process. If they stop for any
   reason (a guard, an error, the ciphertext check), push restores the invariant and
   stops without committing or pushing. If the base moved in this step for any reason
   (pending resolved, or the pull steps ran), scan again; if the new scan is empty, exit
   0.
5. Empty-file guard: a 0-byte local file whose base version is not empty is never pushed.
   If its mtime is older than 10 minutes it is restored from base with a WARN (the empty
   file is copied to trash first and then replaced in place, so a kill between the two
   never leaves the path missing; the restore is decanonicalised and takes the base's exec
   bit); a younger one may be a write in progress and is left alone.
6. Local mass delete guard: more than 20 deletions, and the deletions also outnumber the
   files that would remain under `data/` after the push (the predicate of 6.7 step 6):
   refuse with ERROR unless `CLAUDE_SYNC_ALLOW_PRUNE=1`, restore the invariant, exit 1.
7. Write changed files into `data/` (atomic, through temporary names matching
   `*.sync-tmp.*`), set their exec bit, delete removed ones, `git add -A -- data`. If the
   index then has no staged change (every difference was skipped, for example by the
   empty-file guard), log "no changes" and exit 0 without committing and without setting
   pending. Otherwise commit `sync: <ME>@<LocalHostName> <UTC time>` on top of `HEAD`
   (which is `O`).
8. `git update-ref refs/claude-sync/pending HEAD`, then `git push -q origin HEAD:<BRANCH>`
   with a 30 s timeout. Rejected because the remote moved: first restore the invariant
   (`git reset -q --hard base`, `git clean -qfdx -- data`) and delete pending, then go
   back to step 4 (fetch, pull steps, a full new scan, and steps 5 to 8 again), at most 3
   attempts in total, then ERROR. The restore is required, not
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
- Each run that fetches or writes anything logs one summary line at INFO, for example
  `pull done in 0.8s: 3 remote changes, 2 written, 1 merged, 0 kept-both, 0 trashed`. A
  push that finds no local changes (6.10 step 3) logs at DEBUG only, so the 5 minute
  timer does not fill the log.
- Rotation: above 5 MB the log is renamed to `last-sync.log.1` (one generation).
- Git failures log the command and the last 4 stderr lines (hint lines dropped).
- Offline means the stderr matches a network pattern (could not resolve host, network is
  unreachable, connection timed out or refused, operation timed out, `ssh: connect to
  host`) or the subprocess timed out. Offline is logged as WARN everywhere (pull, push,
  and the pull steps inside push). Everything else is an ERROR.
- Exit codes: 0 for success, skip or offline; 1 for an error. Hooks keep `|| true`.
- An uncaught exception is logged with its traceback and exits 1.
- SIGTERM or SIGHUP (a hook timeout, a closed session) ends the run through the normal
  exit path: the running git child and its process group are killed, a WARN
  `terminated by signal` is logged, a run that holds the lock removes the git lock files
  at once (as after a timeout kill, 6.6 step 1), and the exit code is 128 plus the
  signal number.
- stdout notice: plain text, one line, at most 200 characters, only when there is
  something to act on (kept-both conflicts, local-edit-beats-delete, ERROR, or the shim
  finding no usable python). Example:
  `claude-sync: 2 conflicts kept both, remote copies in ~/.claude-sync/conflicts/2026-10-25T16-31-02Z (see last-sync.log)`.
- Retention: at the end of each locked run, including one that stopped with an error,
  stamp folders (6.8) in `conflicts/` and `trash/` older than 30 days are removed.

### 6.12 Commands

- `pull`, `push`: as above.
- `status`: no lock, no network, no writes. Prints base (or "none"), HEAD,
  `origin/<BRANCH>` from the last fetch, whether a pending push or `.fresh-machine`
  exists, how many local changes the scan finds, how many `conflicts/` folders exist, and
  the last summary line from the log. The scan count is replaced by "locked" when
  `data/CLAUDE.md` starts with the git-crypt header, and by "unknown (a run is in
  progress or was interrupted)" when the invariant (6.4) does not hold, because in both
  cases `data/` is not the decrypted base and a count would be wrong.
- `dry-run` (alias `dry`): takes the lock (20 s wait), runs the checks of 6.6 including
  the unlock and the invariant restore, then the push scan only; prints what would be
  added, changed and deleted. No commit, no fetch, no push. When it checks nothing
  (the run lock is busy, or git is busy as in 6.6 step 1) it prints one
  `claude-sync: ...` line saying so, so an empty answer always means no local changes.

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
Hook timeouts after section 7: SessionStart 60 s, SessionEnd 10 s (kick only). For a
normal pull, the lock wait (20 s) plus the fetch (20 s) plus the work still fit inside
60 s. A first pull may not; a kill there is harmless and the timer push completes it
(6.4).

## 7. Nix module changes (dotnix, separate branch and worktree)

In `home-manager/claude-sync-hooks.nix`:
1. `ensureScript` keeps the claude-sync hooks correct instead of only adding missing ones.
   It replaces any claude-sync entry that differs from the wanted one, and runs over
   `~/.claude/settings.json` and every regular file matching
   `~/Library/Application Support/orca/claude-profiles/*/home/settings.json`. Symlinks
   (such as `~/.claude-work/settings.json`) are skipped. It parses the file, replaces only
   hook entries whose command contains `claude-sync` (adding a wanted one where none
   exists), and keeps every other key and every other hook entry, Orca's included, in
   place. The result is parsed again before an atomic rename, and an unchanged file is not
   rewritten.
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
- `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL` set to a temporary gitconfig (user name
  and email, no hooks path), and `XDG_CONFIG_HOME` removed, so the real git configuration
  and the git-lfs pre-push hook are not used. With `XDG_CONFIG_HOME` set, git reaches the
  real `~/.config/git/config` even under a temporary `HOME` (seen on this Mac).
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
6. settings.json: both sides change different keys; the result is valid JSON holding both
   sides' changes. Both sides add the same key with different values; the result is a
   keep-both, never a file with a duplicate key and never invalid JSON.
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
    `HEAD == base` with a clean `data/`. After one final push, every edit's final bytes
    are in the remote tree and, after a pull there, on the other machine.
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
    are removed by the next run and never committed, including when the ignored file is
    the only leftover and `HEAD == base`.
27. Push landed but unseen: the remote has this machine's commit, pending is set, and the
    base was not moved (as after a timeout or a kill). The next run moves the base to it,
    with no keep-both and no stdout notice.
28. MEMORY.md removals: one side removes an entry while the other adds one in a different
    hunk (at least one unchanged line apart). The removal stays removed, the addition is
    kept, and there is no keep-both. (`--union` keeps both sides of a single conflicting
    hunk, so a removal inside the same hunk as the other side's change comes back; that
    case is not claimed.)
29. Stale tracking ref: `origin/<BRANCH>` is rewound by hand in the clone; the next pull
    still sees the remote tip.
30. Rejected push: machine A has a local edit to a file and is pushing; machine B pushes a
    change to the same file after A's fetch and before A's push lands, so A's push is
    rejected. A's edit is not overwritten: the result is a clean merge, or a keep-both
    with the stdout notice. A second variant where B changed a different file ends with
    both changes on the remote and on both machines.
31. Hook environment: the tool runs with its working directory inside another git repo
    and with `GIT_DIR`, `GIT_WORK_TREE` and `GIT_INDEX_FILE` pointing at that repo. The
    sync works, and the other repo's HEAD, index and files are unchanged.
32. Push after a failed in-push pull: the remote has a change the pull steps cannot apply
    (a write error on that path); push stops, nothing is committed or pushed, and the
    invariant holds.
33. First pull completed by push: no base, a first pull killed before its update-ref, then
    a push with no hook timeout. The push sets the base through its own pull steps
    without waiting on its own lock, then pushes the local-only files.
34. Symlinks: a local symlink at a synced path (file and folder) pointing outside
    `~/.claude` is not read, written, trashed or counted as a delete, and its target is
    unchanged after a pull that changes that path remotely.
35. Unreadable local folder: a synced folder without read permission makes push stop with
    an ERROR and push no deletes.
36. Fresh-machine copy order: when the copy of the local file into `conflicts/` fails, the
    local file is still in place and the base does not move.
37. Nothing to commit: every local difference is skipped (a young 0-byte file); push
    exits 0, makes no commit and leaves no pending ref.
38. `status` on a locked repo prints "locked", and after a killed push prints "unknown",
    instead of a change count.

## 9. Rollout (each step needs the owner's yes)

Code first, nix second, never the other way round (section 7).

1. Review the branch `dfroberg/TO-772-claude-sync-rewrite`.
2. Run the old `claude-sync push` once on the main Mac (EZ-MBP-GD4RPHKWL4) only, right
   before step 3, so its local files and the remote agree. Pre-check first, because the
   old push writes this Mac's files over whatever the other Mac changed (section 2 item
   2): run `git -C ~/.claude-sync fetch -q origin`, then
   `git -C ~/.claude-sync log -1 --format='%h %cI %s' --grep='@Dannys-MacBook-Pro ' origin/main`
   and the same with `--grep='@EZ-MBP-GD4RPHKWL4 '`. If the second Mac's newest commit
   is newer than the main Mac's newest commit, stop: list its files with
   `git -C ~/.claude-sync show --name-only --format= <sha> -- data` and decide with the
   owner, path by path, before pushing. (A second-Mac commit older than the main Mac's
   newest one was already taken in or overwritten by that later push, so the step adds
   no new loss for it. `HEAD..origin/main` is not enough as a check, because the old
   push moves `HEAD` without applying.) Anything that still differs
   on both sides at its first new-code run has no base to merge against: the local copy
   stays and the remote copy goes to `conflicts/` (6.4). On the main Mac, step 4 brings
   in the new code without a last old pull; on the second Mac the last old-code pull,
   which brings in the new code, still runs the old `apply()` once.
   Do not run the old push on the second Mac (Dannys-MacBook-Pro, 5 of the 3596 sync
   commits): the old push never applies, so it would send that Mac's stale copies over
   the main Mac's (section 2 item 2). Its 5 minute timer runs the old push only when
   `~/.claude-sync/.dirty` exists, so before step 3 remove that file there, or keep that
   Mac asleep with no Claude session, until its step 4. Its next session start then runs
   the old pull one last time, which applies the remote over its local files as the old
   code always has, and brings in the new code; its first new-code run finds local equal
   to the remote. If it holds local work that must survive, copy that work aside before
   that session start.
3. Push the branch `dfroberg/TO-772-ignore-first` to `main` first. It is one commit on
   top of `main` that changes only `.gitignore` (adds the repo-root entries of 6.1:
   `/conflicts/`, `/trash/`, `/.sync.lock`, `/.fresh-machine`, `/last-sync.log.1`,
   `/.hold-*`, the two `__pycache__` folders of the code, and `*.sync-tmp.*`) and
   `.gitattributes` (encrypts `conflicts/**` and `trash/**`). Then merge
   the rewrite branch into `main` of `dfroberg/claude-sync` and push. The merge touches
   only `bin/`, `claude_sync/`, `tests/`, `docs/`, `README.md` (new-machine steps gain the
   `touch .fresh-machine` line), `.gitignore` and `.gitattributes`; `data/` is not
   changed. The order matters for the rollback below: reverting the merge keeps the
   ignore-first commit, so the old tool's `git add -A` never picks up parked plaintext.
4. Main Mac (EZ-MBP-GD4RPHKWL4), right after step 3, with no Claude session starting:
   do not let a session start bring in the new code, because that runs the old pull's
   `apply()` once more and writes the remote's copies over edits made since step 2. Run
   `git -C ~/.claude-sync fetch -q origin && git -C ~/.claude-sync reset --hard -q origin/main`
   and then `~/.claude-sync/bin/claude-sync pull` (now the new code). Files that changed since step 2 differ
   on both sides with no base: the local copy stays and the remote copy is parked
   (6.4), so nothing is lost. Second Mac (Dannys-MacBook-Pro): once a pull has brought
   in the new code (any session start, as step 2 describes), run
   `~/.claude-sync/bin/claude-sync pull` by hand so the first new-code run happens at once
   and sets the base. On both Macs check `~/.claude-sync/bin/claude-sync status`, the log
   and any notice.
5. Only on a Mac where step 4 is done: merge the dotnix branch and switch there.
6. Watch for a week: `find ~/.claude/projects/*/memory -type f -size 0` stays empty, no
   unexpected `conflicts/` folders, no ERROR lines.

Rollback: if the nix change was switched, switch back to the previous home-manager
generation first (the old code with the new hooks is the bad pairing in section 7). Then
revert the merge commit only (never the ignore-first commit of step 3) on `main` and
push; each Mac returns to the old code on its next pull. Before reverting, move
`~/.claude-sync/conflicts` and `~/.claude-sync/trash` out of the repo on each Mac (for
example to `~/claude-sync-parked/`): they hold plaintext copies, and the ignore and
encrypt entries are the only thing between them and the old push's `git add -A`. If
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

Changes made after the owner's approval, from the grok-4.7 round (section 12). These alter
or sharpen approved behaviour:
- Keep-both, consequence made explicit (6.4, 6.9): the next push publishes the local copy,
  and the other Mac takes it with no notice there; its version survives only in git
  history and in this Mac's `conflicts/` for 30 days. Unchanged as a rule, now stated.
- Rollout step 2 gains a pre-check: if the second Mac has a commit newer than the main
  Mac's newest, the old push waits for a path-by-path decision.
- Symlinked paths inside `~/.claude` are outside the sync on that Mac, both ways. Pull
  never writes through a symlink. (Today none of the synced paths is one.)
- The common ancestor is always the base. If the remote history was ever rewritten, the
  sync carries on from the base with an INFO line instead of stopping with an ERROR.
- JSON merges with a key added on both sides become a keep-both instead of a silent pick.
- A remote entry that is a symlink or a submodule stops the sync with an ERROR.

Changes made after the owner's approval, from the final Fable gate on the built branch
(section 12, round 3):
- Rollout step 3 now lands a separate ignore-first commit before the merge, and the
  rollback reverts the merge only and moves the parked folders out of the repo. Parked
  folders are also marked for encryption.
- Rollout step 4 on the main Mac brings in the new code with a fetch and a reset instead
  of a last old pull, so the old `apply()` never runs there again.
- A young `.git/index.lock` makes a run skip (INFO `git busy`, exit 0) instead of
  proceeding (6.6 step 1). SIGTERM and SIGHUP kill the running git child (6.11).
- From the re-check of those fixes: an index lock that cannot be removed stops the run
  with an ERROR; a terminated run clears the git lock files; a dry-run that checks
  nothing says so; the repo-root ignore entries are anchored so the same names under
  `data/` still sync.

## 12. Adversarial review

Two rounds ran on the design and a third, the final gate, on the built branch. The first
ran on Fable, because the lead wrongly concluded that xAI was
unreachable: it checked only for `XAI_API_KEY` in the environment and missed
`~/.config/xai/env`, which defines it. The owner pointed this out, and the required
grok-4.7 round then ran against the approved design (see "Round 2" below). The Fable
round and its re-check stay on record as they happened.

### Round 1: Fable

Run on 2026-10-25 against the draft committed as e8f137e1. The reviewer tested its claims
with small git experiments in a scratch directory. Verdict: NEEDS FIXES (1 critical, 6
major, 5 minor). All twelve findings were accepted; none changed the chosen approach.

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

### Round 1: re-check of the fixes

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

### Round 2: grok-4.7

Run on 2026-10-25 with `grok_review.py` (model grok-4.7, reasoning effort high, exit 0)
against the approved design at 288695ef. Verdict: DO NOT EXECUTE (7 critical, 15 major,
12 minor). The lead checked each claim against the text and, where it named code or git
behaviour, against bin/claude-sync or a scratch git experiment. 27 were accepted, 5
accepted in part and 2 rejected, with reasons below.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| C1 | Critical | Hooks start in the user's project, itself a git repo, and may inherit `GIT_DIR` and friends, so `reset --hard`, `clean` and `push` could hit that repo. | Accepted. 6.2: every git command is `git -C $REPO` with the `GIT_*` location variables removed; argv only. Test 31. |
| C2 | Critical | `python -m claude_sync` from a hook cannot import the package, which is not installed. | Accepted. 6.1: the shim changes to the repo root and runs `python3 -E -s -B -m claude_sync`; its own ERROR goes to the repo's log. |
| C3 | Critical | Push's no-base pull would try to take the lock push already holds, so a killed first pull is never completed. | Accepted. 6.4, 6.5, 6.10 step 2: the pull steps run in the same process under the held lock; the lock is taken once per run. 6.14 states that a first pull may outlast the hook. Test 33. |
| C4 | Critical | After the in-push pull, only the mass delete stop was defined; any other stop still pushed. | Accepted. 6.10 step 4: any stop in the pull steps restores the invariant and ends push without a commit. Test 32. |
| C5 | Critical | No parent directories were created, and trash had no path. | Accepted. 6.3: every write creates missing parents. 6.8: trash path and stamp format. |
| C6 | Critical | The fresh-machine replace wrote the remote file before parking the local one. | Accepted. 6.4, 6.9: copy the local file to `conflicts/` first; a failed copy stops the run. Test 36. |
| C7 | Critical | A first-pull keep-both plus base at `O` lets the next push replace the other Mac's version, with no notice there; rollout step 2 can do the same through the old push. | Accepted in part. The rule stays: keep-both letting local win is the owner's call (section 11). Its consequence is now stated in 6.4 and 6.9 and listed in section 11. Rollout step 2 gains a pre-check (newest second-Mac commit against newest main-Mac commit). Not taken: holding such paths back from every push until a manual resolve, which adds state the owner did not ask for. |
| M1 | Major | The normal write path did not say to decanonicalise. | Accepted. 6.3 and 6.8: every write into `~/.claude` decanonicalises. |
| M2 | Major | Pending resolution was not an algorithm and could commit on the wrong parent. | Accepted in part. 6.4: save the commit, `reset --hard` to it, move the base, delete the ref last. 6.10 step 4: rescan whenever the base moved. Kept: push's no-change exit before the fetch (the next fetching run resolves pending), so the timer stays cheap. |
| M3 | Major | The restore check does not see ignored leftovers. | Accepted. 6.4: `git clean -qfdx -- data` runs on every locked run. Test 26. |
| M4 | Major | `status` and `dry-run` scanned without the unlock or the restore. | Accepted. 6.12: `status` prints "locked" or "unknown" instead of a wrong count; `dry-run` takes the lock and runs the 6.6 checks. Test 38. |
| M5 | Major | `json.loads` accepts duplicate keys, and test 6 accepted a destructive result. | Accepted in part. 6.9: a merged JSON result with a duplicate key is rejected (keep-both). A remote version taken whole is still written as is, so `.json` files with comments keep syncing. Test 6 asserts both sides' keys. A full key-survival check was not added: a clean line merge drops a key only when one side deleted it. |
| M6 | Major | Two mass delete predicates, and "kept" was undefined. | Accepted. One predicate in 6.7 step 6, used by 6.10 step 6: more than 20 deletes and more deletes than files left under `data/` in the destination tree. A folder rename passes, so no rename detection is needed. |
| M7 | Major | With rewritten history, a merge-base diff misses paths that differ between the base and `O`. | Accepted, with a simpler fix than proposed: `M` is always the base (6.7 step 4). A tree diff needs no ancestry, so merge-base and its ERROR are gone. |
| M8 | Major | "Treat as a conflict" after the race retries was undefined, and trash moves had no re-stat. | Accepted in part. 6.8: keep-both after 3 attempts; trash moves re-stat too. Not taken: holding the path back from the next push; the partial-write risk is stated in 6.8 and is today's. |
| M9 | Major | An unreadable local file looked like a delete. | Accepted. 6.7 step 7 and 6.10 step 3: only a confirmed absent path is a delete; anything else stops the run. Test 35. |
| M10 | Major | Test 13 could pass with no publishing; test 28 claimed more than `--union` does. | Accepted. Test 13 checks final bytes on the remote and the other machine; test 28 uses separate hunks and says what is not claimed. |
| M11 | Major | Pull followed symlinks out of `~/.claude`. | Accepted. 6.3: symlinked paths are outside the sync both ways. Test 34. |
| M12 | Major | Decoding text before the ciphertext check could raise on bad bytes. | Accepted. 6.3: byte replacement only; 6.6: the guard checks raw bytes first. |
| M13 | Major | The nix ensure step could drop Orca's own hooks. | Accepted. Section 7 item 1: replace only claude-sync entries, keep everything else, parse again before the rename. |
| M14 | Major | An empty commit would be an ERROR. | Accepted. 6.10 step 7: a clean index after `add` exits 0 without a commit or pending. Test 37. |
| M15 | Major | Removing an old lock directory after 900 s could run beside a live old process. | Rejected after reading the old code: 900 s is its own stale limit (bin/claude-sync line 149), so an old run removes it at the same age. 6.5 now cites it. |
| m1 | Minor | Autostash handling had no steps. | Accepted. 6.6 item 2 spells out `stash store` as bin/claude-sync line 238 does. |
| m2 | Minor | The lock file was never created. | Accepted. 6.5: opened with create. |
| m3 | Minor | A timed-out git command leaves young lock files that block the next run. | Accepted. 6.6 item 1: after killing a git command, its lock files are removed at once. |
| m4 | Minor | A too-old first interpreter was not skipped. | Accepted. 6.1: the first candidate that reports 3.9 or newer. |
| m5 | Minor | Offline had three log levels. | Accepted. 6.11: WARN everywhere. |
| m6 | Minor | "One summary line per run" against "no changes at DEBUG". | Accepted in part. 6.11: every run that fetches or writes logs a summary at INFO; the no-change push stays at DEBUG so the timer does not fill the log. |
| m7 | Minor | Two stamp formats. | Accepted. 6.8: `2026-10-25T16-31-02Z`, one per run. |
| m8 | Minor | The notice was specified only on the success path. | Accepted. 6.7 step 10: every ERROR stop prints it. |
| m9 | Minor | A `git merge-file` error was not told apart from a conflict. | Accepted, verified: a missing input and a binary input both exit 255, a conflict exits 1. 6.9: 1 to 127 is a conflict, anything else is an ERROR. |
| m10 | Minor | A new 0-byte file could be pushed. | Rejected: it has no base version to lose, so publishing an empty new file loses nothing; the guard exists for files that had content. |
| m11 | Minor | The commit message and refspec must be argv. | Accepted. 6.2. |
| m12 | Minor | Diff status `T` was not in the table. | Accepted. 6.3: a remote symlink or submodule entry stops the run with an ERROR. |

### Round 2: re-check of the fixes

One grok-4.7 pass on the fix diffs only (design 288695ef..9dd506fb and the plan), with the
design as the oracle. Verdict: DO NOT EXECUTE (1 critical, 10 major, 5 minor), mostly about
plan wording; the design changes it prompted:
- 6.2: the environment clean-up also removes config injection (`GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`) and the askpass helpers,
  and covers git-crypt. Not taken: always replacing an inherited `GIT_SSH_COMMAND`; it is
  the user's own setting, and without `BatchMode` the worst case is a fetch that hits its
  20 s timeout and is reported as offline.
- 6.8: the race fallback for a remote delete keeps the local file (nothing to park), and
  reads never follow a symlink.
The plan's record lists every item and its resolution.

### Round 3: Fable final gate on the built branch

One Fable pass over the whole branch (code at 09fadbac) and the dotnix commit 0dec33c,
against this design. Verdict: NOT READY (1 critical, 1 major, 4 minor). All six were
verified in the code and fixed:

| # | Sev | Finding | Resolution |
|---|-----|---------|------------|
| 1 | Critical | The documented rollback (revert the merge) also reverts the new `.gitignore`; the old tool's push runs `git add -A` and would commit `conflicts/` and `trash/` (plaintext copies) to the public remote. | Accepted. A separate ignore-first commit (branch `dfroberg/TO-772-ignore-first`, `.gitignore` and `.gitattributes` only) lands on `main` before the merge; the rollback reverts the merge only and moves the parked folders out of the repo first; `.gitattributes` encrypts `conflicts/**` and `trash/**` (section 9). |
| 2 | Major | A git child runs in its own session, so after a hook timeout kills a pull it can keep rewriting `data/` while the next run, which keeps young lock files, reads `data/` as the base and merges against a half-written base. | Accepted. A young `.git/index.lock` makes the run skip with INFO `git busy` (6.6 step 1); SIGTERM and SIGHUP raise through the normal exit path, which kills the child's process group (6.11). Tests `test_young_index_lock_skips_pull`, `test_sigterm_kills_git_child`. |
| 3 | Minor | The empty-file guard moved the file to trash before writing the restore; a kill between the two left the path missing, and the next push would publish a delete. | Accepted. Copy to trash, then replace in place (6.10 step 5). Test `test_empty_file_guard_crash_keeps_path`. |
| 4 | Minor | Equal content with only the exec bit different became a keep-both for a non-text file, against 6.8's own rule for the bit. | Accepted. Equal content takes the bit from the three-way exec rule, for every file type. Five tests in `test_decide.py`. |
| 5 | Minor | The hooks ensure script skipped symlinked Orca profile settings but not a symlinked `~/.claude/settings.json`. | Accepted. Both are skipped (dotnix commit cece391). Not the case on this Mac today. |
| 6 | Minor | On the main Mac, the session start that brings in the new code still ran the old pull's `apply()`, writing the remote's copies over edits made since step 2. | Accepted. Section 9 step 4: fetch and reset, then the new pull, on the main Mac. |

### Round 3: re-check of the fixes

One Fable pass over the six fixes only. Verdict: ready for the owner's rollout review,
all six closed. It raised four new items and three wording points, all fixed:

| # | Sev | Item | Resolution |
|---|-----|------|------------|
| 1 | Minor | The lock removal swallows its error, so a stale `.git/index.lock` that cannot be removed (root-owned) made every run skip silently, where before it was a loud ERROR. | An index lock of 60 s or older that survives the removal stops the run with an ERROR (6.6 step 1). |
| 2 | Minor | A dry-run that skipped (git busy) printed nothing, which reads as "no local changes". | A dry-run that checks nothing, because git or the run lock is busy, prints one line saying so (6.12). |
| 3 | Low | A run ended by SIGTERM left the killed child's lock files, so the next run skipped for up to 60 s. | A terminated run that holds the lock removes them at once, as a timeout kill does (6.11). |
| 4 | Low | The unanchored `conflicts/`, `trash/` and `__pycache__/` ignore entries also matched those names under `data/`, so such files silently never synced; `data/skills/` holds `__pycache__` folders today. | The repo-root entries are anchored with a leading slash (6.1); the ignore-first commit is rebuilt with the same file. |
| - | Wording | Section 9: name the full path of the new tool in step 4; move the parked folders before reverting, not before the first old-code push; step 3's list missed `__pycache__/`. | Fixed in section 9. |
