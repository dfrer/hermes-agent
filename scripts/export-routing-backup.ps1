[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$BackupRoot = "",
    [string]$BaseRef = "origin/main"
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) {
        throw "Could not determine script path."
    }
    $scriptDir = Split-Path -Parent $scriptPath
    $RepoRoot = (Get-Item (Join-Path $scriptDir "..")).FullName
}

$hermesHome = Split-Path $RepoRoot -Parent
if (-not $BackupRoot) {
    $BackupRoot = Join-Path $hermesHome "routing-backups"
}

function Invoke-GitCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = & git -C $RepoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return ($output | Out-String).TrimEnd()
}

$branch = Invoke-GitCapture @("branch", "--show-current")
$head = Invoke-GitCapture @("rev-parse", "HEAD")
$shortHead = Invoke-GitCapture @("rev-parse", "--short=8", "HEAD")
$commits = Invoke-GitCapture @("rev-list", "--reverse", "$BaseRef..HEAD")
$commitList = @()
if ($commits) {
    $commitList = $commits -split "`r?`n" | Where-Object { $_.Trim() }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $BackupRoot $timestamp
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$bundlePath = Join-Path $dest "routing-integration.bundle"
$patchPath = Join-Path $dest "routing-stack.patch"
$logPath = Join-Path $dest "commits.txt"
$restorePath = Join-Path $dest "RESTORE.md"
$manifestPath = Join-Path $dest "manifest.json"

& git -C $RepoRoot bundle create $bundlePath HEAD | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create git bundle backup."
}

$patchContent = Invoke-GitCapture @("format-patch", "--stdout", "$BaseRef..HEAD")
$patchContent | Out-File -FilePath $patchPath -Encoding utf8

$logContent = Invoke-GitCapture @("log", "--oneline", "$BaseRef..HEAD")
$logContent | Out-File -FilePath $logPath -Encoding utf8

$soulPath = Join-Path $hermesHome "SOUL.md"
$routingSkillPath = Join-Path $hermesHome "skills\\routing-layer\\SKILL.md"
if (Test-Path $soulPath) {
    Copy-Item $soulPath (Join-Path $dest "SOUL.md")
}
if (Test-Path $routingSkillPath) {
    Copy-Item $routingSkillPath (Join-Path $dest "routing-layer.SKILL.md")
}

$restoreText = @"
# Routing Backup Restore

Bundle file:
- routing-integration.bundle

Patch file:
- routing-stack.patch

Restore options:

1. Restore the exact backed-up branch into a fresh clone:

   git clone <upstream hermes repo> hermes-agent-restored
   cd hermes-agent-restored
   git fetch "$bundlePath" HEAD:codex/routing-restored
   git switch codex/routing-restored

2. Reapply only the routing delta on top of a newer upstream checkout:

   git am --3way < routing-stack.patch

Reference files copied with this backup:
- SOUL.md
- routing-layer.SKILL.md
"@
$restoreText | Out-File -FilePath $restorePath -Encoding utf8

$manifest = @{
    created_at = (Get-Date).ToString("o")
    repo_root = $RepoRoot
    branch = $branch
    head = $head
    short_head = $shortHead
    base_ref = $BaseRef
    commit_count = $commitList.Count
    commits = $commitList
    files = @(
        "routing-integration.bundle",
        "routing-stack.patch",
        "commits.txt",
        "RESTORE.md"
    )
}
$manifest | ConvertTo-Json -Depth 6 | Out-File -FilePath $manifestPath -Encoding utf8

Write-Host "Routing backup exported to: $dest"
