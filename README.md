# codex-session-path-migrator

A conservative Windows-oriented Codex Skill for rebinding local Codex Desktop thread/session history after moving a project folder.

## Install

Copy this folder to:

```text
%USERPROFILE%\.agents\skills\codex-session-path-migrator\
```

Then restart Codex App so it can discover the Skill.

## Important safety model

There is an intentional split between **inspection in Codex** and **mutation outside Codex**.

While Codex App is running, the tool permits only:

```text
inspect
prepare
verify
```

The following commands are blocked if any `Codex.exe` / `codex.exe` process is detected:

```text
migrate
rollback
cleanup
```

This block exists in both `migrate.ps1` and `migrate.py`, so bypassing the PowerShell wrapper does not bypass the safety rule.

## Recommended use from Codex

Ask Codex:

```text
Use codex-session-path-migrator for this project move.
Old path: D:\old\project
New path: E:\new\project
Run prepare only. Do not mutate any Codex state. Show me the dry-run summary and the external PowerShell migrate command I should run after I exit Codex App.
```

## Manual workflow

### 1. Inspect / prepare while Codex is open

```powershell
.\scripts\migrate.ps1 prepare `
  -OldPath 'D:\old\project' `
  -NewPath 'E:\new\project'
```

`prepare` is read-only and prints the exact external migration command.

### 2. Fully exit Codex App

Close all Codex windows and ensure no Codex process remains in Task Manager.

### 3. Run migration in an external PowerShell

```powershell
.\scripts\migrate.ps1 migrate `
  -OldPath 'D:\old\project' `
  -NewPath 'E:\new\project'
```

The command creates a scoped backup before changing anything and prints:

```text
Backup ID: YYYYMMDD-HHMMSS-xxxxxxxx
```

Save this ID.

### 4. Verify

```powershell
.\scripts\migrate.ps1 verify `
  -OldPath 'D:\old\project' `
  -NewPath 'E:\new\project'
```

Then reopen Codex App and check that the expected old tasks now appear under the new project path.

### 5A. If the UI is correct, delete the backup promptly

Fully exit Codex App again, then run:

```powershell
.\scripts\migrate.ps1 cleanup `
  -BackupId 'YYYYMMDD-HHMMSS-xxxxxxxx' `
  -ConfirmBackupDeletion
```

Only the exact backup created by this tool is removed.

### 5B. If the UI is wrong, roll back

Fully exit Codex App, then run:

```powershell
.\scripts\migrate.ps1 rollback `
  -BackupId 'YYYYMMDD-HHMMSS-xxxxxxxx' `
  -ConfirmRollback
```

Rollback retains the backup. After confirming Codex is healthy again, close Codex and use `cleanup` to remove it.

## Storage policy

The tool does **not** copy the whole `.codex` directory. It backs up only files that are about to be changed. Backups live under:

```text
%USERPROFILE%\.codex\session-path-migrator-backups\
```

A backup stays only until you confirm the migration in the Codex UI. At that point, `cleanup` deletes it immediately.

## Requirements

- Windows 10/11
- Windows PowerShell 5.1 or PowerShell 7+
- Python 3, standard library only
- Codex App closed for `migrate`, `rollback`, and `cleanup`

## Commands

```text
inspect   read-only discovery
prepare   read-only discovery + copy/paste migration commands
migrate   backup + mutate, Codex must be closed
verify    read-only local verification
rollback  restore from a migration backup, Codex must be closed
cleanup   securely remove one validated migration backup, Codex must be closed
```
