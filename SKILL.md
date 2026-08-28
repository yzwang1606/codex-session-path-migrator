---
name: codex-session-path-migrator
description: Safely migrate Codex Windows App thread/session workspace bindings after a project folder moves to a new path. Use when old Codex chats disappear after moving or renaming a workspace.
argument-hint: "<old-path> <new-path>"
disable-model-invocation: true
---

# Codex Session Path Migrator

Safely rebind local Codex Windows App history from an old workspace path to a new workspace path.

## Core rule

When this Skill is invoked *from a running Codex App*, it may only perform read-only work:

- `inspect`
- `verify`
- `prepare`

It MUST NOT perform `migrate`, `rollback`, or `cleanup` while Codex is running. The wrapper and Python helper both enforce this rule independently.

Formal mutation must be executed from an external PowerShell window after Codex App has been fully exited.

## Inputs

Use `$0` as the old absolute Windows project path and `$1` as the new absolute Windows project path.

Do not guess either path when there is material ambiguity. If the user asks for automatic detection, only present candidates during `inspect`; require the selected paths before mutation.

## Safe workflow

1. While Codex is running, run `inspect` or `prepare` only.
2. Present the dry-run summary to the user.
3. Generate the exact external PowerShell `migrate` command.
4. Ask the user to fully exit Codex App.
5. The user runs `migrate` in an external PowerShell window.
6. The tool creates a scoped backup before mutation and prints a Backup ID.
7. Run `verify` locally.
8. Reopen Codex App and confirm the expected old chats are displayed under the new project path.
9. If correct, fully exit Codex App again and run `cleanup` for the exact Backup ID.
10. If incorrect, fully exit Codex App and run `rollback` for the exact Backup ID.

Never auto-delete a migration backup before the user confirms the Codex UI is correct.

## Commands

From this Skill directory:

```powershell
# Safe while Codex is running
.\scripts\migrate.ps1 inspect -OldPath "$0" -NewPath "$1"
.\scripts\migrate.ps1 prepare -OldPath "$0" -NewPath "$1"
.\scripts\migrate.ps1 verify  -OldPath "$0" -NewPath "$1"

# Requires Codex App to be fully closed
.\scripts\migrate.ps1 migrate -OldPath "$0" -NewPath "$1"

# After UI confirmation; requires Codex closed
.\scripts\migrate.ps1 cleanup -BackupId "<backup-id>" -ConfirmBackupDeletion

# If migration is wrong; requires Codex closed
.\scripts\migrate.ps1 rollback -BackupId "<backup-id>" -ConfirmRollback
```

## What `prepare` does

`prepare` is read-only. It:

- performs the same discovery as `inspect`;
- reports affected SQLite rows and session/state files;
- reports whether normal and/or `\\?\` path forms are present;
- prints copy/paste-ready external PowerShell commands for `migrate` and `verify`.

It does not create a backup or modify Codex state.

## What the tool may update

When present and actually matching the old workspace path:

- `%USERPROFILE%\.codex\state_5.sqlite` → `threads.cwd`
- `%USERPROFILE%\.codex\sqlite\codex-dev.db` → `local_thread_catalog.cwd`
- affected JSONL records under `sessions` and `archived_sessions`
- path-bearing values in `.codex-global-state.json`
- path-bearing values in `session_index.jsonl`

Only recognized path-bearing keys are rewritten. Arbitrary strings in chat content are not replaced.

The tool recognizes ordinary Windows paths and their `\\?\` extended-length equivalents while preserving the original prefix style for each value.

## Backup policy

Backups are stored under:

`%USERPROFILE%\.codex\session-path-migrator-backups\<backup-id>`

Only files that will be mutated are copied. Each backup contains a manifest with hashes and migration metadata.

`cleanup`:

- requires `-ConfirmBackupDeletion`;
- refuses to run while Codex is running;
- refuses arbitrary paths;
- only deletes a backup created by this tool;
- validates backup file hashes before deletion;
- removes the now-empty backup parent directory when appropriate.

This keeps the backup only until the user has confirmed the migration is visibly correct, minimizing storage use without sacrificing rollback safety.

## Failure behavior

If mutation fails after backup creation:

- stop immediately;
- preserve the backup;
- print the Backup ID;
- instruct the user to keep Codex closed and run `rollback`.

If local verification passes but the Codex UI is wrong, do not repeatedly edit state. Keep the backup and roll back after closing Codex.

## Windows path caveat

Codex has had Windows path-canonicalization issues involving `C:\...` versus `\\?\C:\...`. Local verification is necessary but not sufficient; the final acceptance criterion is the user seeing the expected chats under the new project in Codex App.
