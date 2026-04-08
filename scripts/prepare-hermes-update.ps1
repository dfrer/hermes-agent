[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$Remote = "origin",
    [string]$BaseBranch = "main",
    [switch]$Apply,
    [switch]$Rebase,
    [string]$UpdateBranchPrefix = "codex/upstream-sync",
    [string]$WorktreePath
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

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & git -C $RepoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-GitOutput {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = & git -C $RepoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return ($output | Out-String).Trim()
}

function Ensure-CleanWorktree {
    $status = Get-GitOutput @("status", "--porcelain")
    if ($status) {
        throw @"
Hermes update prep aborted because the worktree is dirty.

Commit or stash your current changes first, then rerun:
  powershell -ExecutionPolicy Bypass -File .\scripts\prepare-hermes-update.ps1 -Apply
"@
    }
}

$branchName = Get-GitOutput @("branch", "--show-current")
if (-not $branchName) {
    throw "Could not determine current branch."
}

Write-Host "Hermes repo: $RepoRoot"
Write-Host "Current branch: $branchName"
Write-Host "Fetching $Remote..."
Invoke-Git @("fetch", $Remote, "--prune")

$behindAhead = Get-GitOutput @("rev-list", "--left-right", "--count", "$branchName...$Remote/$BaseBranch")
$counts = $behindAhead -split "\s+"
$ahead = if ($counts.Length -ge 1) { [int]$counts[0] } else { 0 }
$behind = if ($counts.Length -ge 2) { [int]$counts[1] } else { 0 }

Write-Host "Compared with ${Remote}/${BaseBranch}:"
Write-Host "  Ahead:  $ahead"
Write-Host "  Behind: $behind"

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry run only. To create a disposable update worktree:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\\scripts\\prepare-hermes-update.ps1 -Apply"
    return
}

Ensure-CleanWorktree

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$updateBranch = "$UpdateBranchPrefix-$timestamp"

if (-not $WorktreePath) {
    $parent = Split-Path $RepoRoot -Parent
    $leaf = Split-Path $RepoRoot -Leaf
    $WorktreePath = Join-Path $parent "$leaf-update-$timestamp"
}

if (Test-Path $WorktreePath) {
    throw "Worktree path already exists: $WorktreePath"
}

Write-Host "Creating update worktree:"
Write-Host "  Branch:  $updateBranch"
Write-Host "  Path:    $WorktreePath"

Invoke-Git @("worktree", "add", "-b", $updateBranch, $WorktreePath, $branchName)

$integrationArgs = if ($Rebase) {
    @("rebase", "$Remote/$BaseBranch")
}
else {
    @("merge", "--no-ff", "$Remote/$BaseBranch")
}

Write-Host ""
Write-Host "Update worktree created."
Write-Host "Next steps:"
Write-Host "  1. cd '$WorktreePath'"
Write-Host "  2. git $($integrationArgs -join ' ')"
Write-Host "  3. Resolve conflicts if any"
Write-Host "  4. pwsh ./scripts/test-routing-contract.ps1"
Write-Host "  5. If clean, fast-forward '$branchName' to '$updateBranch'"
Write-Host ""
Write-Host "Suggested fast-forward step after verification:"
Write-Host "  git -C '$RepoRoot' merge --ff-only $updateBranch"
