# claude-sync

Cross-machine sync for `~/.claude` memories, settings, skills, agents, commands,
and hooks — via this **git-crypt-encrypted** repo. Public-repo safe: everything
under `data/` is encrypted at rest.

**User/path aware:** on push, `/Users/<you>/…` and `-Users-<you>-…` project dir
names are canonicalized to a `__USER__` sentinel; on pull they're rehydrated to
the local user. So `dfroberg` and `dannyfroberg` are the same person.

## What syncs (allowlist)

`CLAUDE.md`, `settings.json`, `mcp_settings.json`, `keybindings.json`,
`skills/`, `agents/`, `commands/`, `hooks/`, and `projects/*/memory/` (memories
only — **never** transcripts, plugins, caches, auth/credentials).

## Usage

    claude-sync dry-run   # preview what would sync (no git)
    claude-sync push      # collect ~/.claude -> repo, commit, push
    claude-sync pull      # pull repo -> apply into ~/.claude
    claude-sync status

## New machine

    git clone <repo> ~/.claude-sync
    git-crypt unlock <keyfile>       # same key exported from the first machine
    ~/.claude-sync/bin/claude-sync pull

## Automation

`SessionStart` hook runs `claude-sync pull`, `Stop` hook runs `claude-sync push`
(both flock-guarded, offline-tolerant, and `|| true` so they never break a session).
