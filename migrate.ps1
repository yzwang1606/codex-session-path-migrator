[CmdletBinding()]
param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet('inspect','prepare','migrate','verify','rollback','cleanup')]
    [string]$Command,

    [string]$OldPath,
    [string]$NewPath,
    [string]$BackupId,
    [switch]$ConfirmRollback,
    [switch]$ConfirmBackupDeletion,
    [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex')
)

$ErrorActionPreference = 'Stop'

function Get-CodexProcesses {
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -match '^codex$'
    })
}

function Assert-CodexClosed {
    $p = Get-CodexProcesses
    if ($p.Count -gt 0) {
        $ids = ($p | ForEach-Object { $_.Id }) -join ', '
        throw "SAFETY BLOCK: Codex is running (PID(s): $ids). '$Command' is a write/destructive operation. Fully exit Codex App, then run this command from an external PowerShell window."
    }
}

function Get-PythonCommand {
    foreach ($candidate in @('python','py')) {
        try {
            if ($candidate -eq 'py') {
                & $candidate -3 -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)" *> $null
                if ($LASTEXITCODE -eq 0) { return @($candidate,'-3') }
            } else {
                & $candidate -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)" *> $null
                if ($LASTEXITCODE -eq 0) { return @($candidate) }
            }
        } catch {}
    }
    throw 'Python 3 is required. Install Python 3 or run this Skill from an environment where python/py is available.'
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$helper = Join-Path $scriptDir 'migrate.py'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw "Missing helper: $helper" }
if (-not (Test-Path -LiteralPath $CodexHome -PathType Container)) { throw "Codex home not found: $CodexHome" }

if ($Command -in @('migrate','rollback','cleanup')) {
    Assert-CodexClosed
}

if ($Command -in @('inspect','prepare','migrate','verify')) {
    if ([string]::IsNullOrWhiteSpace($OldPath) -or [string]::IsNullOrWhiteSpace($NewPath)) {
        throw "OldPath and NewPath are required for '$Command'."
    }
    if (-not (Test-Path -LiteralPath $NewPath -PathType Container)) {
        throw "NewPath does not exist or is not a directory: $NewPath"
    }
}

if ($Command -in @('rollback','cleanup') -and [string]::IsNullOrWhiteSpace($BackupId)) {
    throw "BackupId is required for '$Command'."
}
if ($Command -eq 'rollback' -and -not $ConfirmRollback) {
    throw 'Rollback requires -ConfirmRollback.'
}
if ($Command -eq 'cleanup' -and -not $ConfirmBackupDeletion) {
    throw 'Backup deletion requires -ConfirmBackupDeletion after you have verified the migrated chats in Codex App.'
}

$py = Get-PythonCommand
$args = @($helper, $Command, '--codex-home', $CodexHome)
if ($OldPath) { $args += @('--old-path', $OldPath) }
if ($NewPath) { $args += @('--new-path', $NewPath) }
if ($BackupId) { $args += @('--backup-id', $BackupId) }
if ($ConfirmRollback) { $args += '--confirm-rollback' }
if ($ConfirmBackupDeletion) { $args += '--confirm-backup-deletion' }

if ($py.Count -eq 2) {
    & $py[0] $py[1] @args
} else {
    & $py[0] @args
}
exit $LASTEXITCODE
