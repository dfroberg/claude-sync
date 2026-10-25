# claude-sync

Cross-machine sync for `~/.claude` memories, settings, skills, agents, commands,
and hooks, through this **git-crypt-encrypted** repo. Public-repo safe: everything
under `data/` is encrypted at rest.

**Three-way sync:** every machine keeps a local base (the last remote commit it has
fully taken in). A pull compares base, remote and the local file path by path, merges
what it can, and never replaces a local edit without a merge or a kept copy. So an
edit made in one Claude session survives a pull started by another session.

**User/path aware:** on push, `/Users/<you>/...` and `-Users-<you>-...` project dir
names are canonicalized to a `__USER__` sentinel; on pull they're rehydrated to
the local user. So `dfroberg` and `dannyfroberg` are the same person.

## What syncs (allowlist)

`CLAUDE.md`, `settings.json`, `mcp_settings.json`, `keybindings.json`,
`skills/`, `agents/`, `commands/`, `hooks/`, and `projects/*/memory/` (memories
only, **never** transcripts, plugins, caches, auth/credentials).

Symlinked files and folders are never read, written or deleted through, on either side.

## Usage

    claude-sync pull      # take remote changes into ~/.claude (three-way, per path)
    claude-sync push      # collect ~/.claude into the repo, commit, push
    claude-sync status    # base, head, origin, pending push, local changes (no lock, no network)
    claude-sync dry-run   # list what a push would send: A added, M modified, D deleted (alias: dry)

Exit code 0 for success, a skipped run (lock busy) or offline; 1 for an error; 2 for bad usage.

### Conflicts, trash and the notice

- **Both sides changed a file.** Text files are merged. `MEMORY.md` indexes are unioned and
  `settings.json` is merged key by key. When a merge is not clean, the local file stays in
  place and the remote copy is saved under `~/.claude-sync/conflicts/<UTC stamp>/`.
- **The remote deleted a file you did not touch.** It moves to
  `~/.claude-sync/trash/<UTC stamp>/` instead of being removed. A file you edited is kept.
- **You deleted a file.** The next push removes it on the remote; the other machine moves
  its copy to `trash/`.
- Folders in `conflicts/` and `trash/` older than 30 days are removed at the end of each run.
- When something needs a look, the run prints one line (at most 200 characters), for example
  `claude-sync: 2 conflicts kept both, remote copies in ~/.claude-sync/conflicts/2026-10-25T16-31-02Z (see last-sync.log)`.
  The same applies to local edits kept over a remote delete, to errors, and to a new Mac whose
  files were replaced by the remote version (the local copies are saved in `conflicts/`).
  Everything else is in `~/.claude-sync/last-sync.log`.
- Safety guards: a run that would delete more than 20 files and more files than it leaves is
  refused (override once with `CLAUDE_SYNC_ALLOW_PRUNE=1`); a 0-byte file never replaces a
  non-empty synced one; a push never commits plaintext into the encrypted repo.

## New machine

    git clone <repo> ~/.claude-sync
    git-crypt unlock <keyfile>       # same key exported from the first machine
    touch ~/.claude-sync/.fresh-machine
    ~/.claude-sync/bin/claude-sync pull

The marker tells the first pull that the local files on this Mac are defaults (for example a
`settings.json` holding only the hooks): where they differ from the remote, the remote version
is taken and the local copy is saved in `conflicts/`. Without the marker, the local file wins
and the remote copy goes to `conflicts/` (right for a Mac that already synced with the old
tool). The marker is removed once the first pull succeeds. Until a machine has pulled once, a
push pulls first and sends nothing if that fails.

## Automation

Home Manager installs the hooks (see the dotnix `claude-sync-hooks` module): a `SessionStart`
hook runs `claude-sync pull` (60 s), a `SessionEnd` hook only kicks the push timer (10 s), and
a timer runs `claude-sync push` every 5 minutes. All hooks end in `|| true` so they never break
a session. Runs are serialised by a lock (`~/.claude-sync/.sync.lock`): a pull waits up to 20 s,
a push up to 60 s, and a run that cannot get the lock exits 0 and logs `lock busy`.

## Troubleshooting

- `claude-sync status` shows `local changes: locked` when the repo is still encrypted (no key),
  and `unknown (a run is in progress or was interrupted)` while another run holds the repo; the
  next run repairs an interrupted one.
- `CLAUDE_SYNC_DEBUG=1` adds DEBUG lines to the log. `CLAUDE_SYNC_LOCK_WAIT=<seconds>` overrides
  every lock wait.
