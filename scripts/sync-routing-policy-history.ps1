[CmdletBinding()]
param(
    [string]$HermesHome = "",
    [string]$HistoryRepo = "",
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"

if (-not $HermesHome) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if (-not $scriptPath) {
        throw "Could not determine script path."
    }
    $scriptDir = Split-Path -Parent $scriptPath
    $repoRoot = (Get-Item (Join-Path $scriptDir "..")).FullName
    $HermesHome = Split-Path $repoRoot -Parent
}

if (-not $HistoryRepo) {
    $HistoryRepo = Join-Path $HermesHome "routing-policy-history"
}

function Invoke-HistoryGit {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = & git -c "safe.directory=$HistoryRepo" -C $HistoryRepo @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return ($output | Out-String).TrimEnd()
}

$sourceSoul = Join-Path $HermesHome "SOUL.md"
$sourceSkill = Join-Path $HermesHome "skills\\routing-layer\\SKILL.md"
if (-not (Test-Path $sourceSoul)) {
    throw "Missing source file: $sourceSoul"
}
if (-not (Test-Path $sourceSkill)) {
    throw "Missing source file: $sourceSkill"
}

New-Item -ItemType Directory -Path $HistoryRepo -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $HistoryRepo "skills\\routing-layer") -Force | Out-Null

$gitDir = Join-Path $HistoryRepo ".git"
if (-not (Test-Path $gitDir)) {
    & git init --initial-branch=main $HistoryRepo | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to initialize routing policy history repo at $HistoryRepo"
    }
}

$readmePath = Join-Path $HistoryRepo "README.md"
$readme = @"
# Routing Policy History

This repo is the durable history for the live Hermes routing policy files that live outside the
`hermes-agent` Git repo:

- `SOUL.md`
- `skills/routing-layer/SKILL.md`

Use `scripts/sync-routing-policy-history.ps1` from the `hermes-agent` repo to snapshot the live policy
files here after policy edits. This keeps a restorable history even when Hermes itself is updated.
"@
$readme | Out-File -FilePath $readmePath -Encoding utf8

Copy-Item $sourceSoul (Join-Path $HistoryRepo "SOUL.md") -Force
Copy-Item $sourceSkill (Join-Path $HistoryRepo "skills\\routing-layer\\SKILL.md") -Force

$manifest = @{
    synced_at = (Get-Date).ToString("o")
    hermes_home = $HermesHome
    source_files = @(
        $sourceSoul,
        $sourceSkill
    )
}
$manifest | ConvertTo-Json -Depth 4 | Out-File -FilePath (Join-Path $HistoryRepo "manifest.json") -Encoding utf8

Invoke-HistoryGit @("add", ".") | Out-Null

$status = Invoke-HistoryGit @("status", "--short")
if (-not $status.Trim()) {
    $resolvedHead = ""
    try {
        $resolvedHead = Invoke-HistoryGit @("rev-parse", "--short=8", "HEAD")
    } catch {
        $resolvedHead = ""
    }
    if ($resolvedHead) {
        Write-Host "Routing policy history already up to date at $HistoryRepo ($resolvedHead)"
    } else {
        Write-Host "Routing policy history already up to date at $HistoryRepo"
    }
    exit 0
}

if (-not $Message) {
    $Message = "Sync routing policy $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')"
}

Invoke-HistoryGit @("commit", "-m", $Message) | Out-Null

$head = Invoke-HistoryGit @("rev-parse", "--short=8", "HEAD")
Write-Host "Routing policy history synced to: $HistoryRepo ($head)"
