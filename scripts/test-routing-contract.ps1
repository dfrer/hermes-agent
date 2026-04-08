[CmdletBinding()]
param(
    [string]$RepoRoot = ""
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

$testFileCandidates = @(
    @("tests/agent/test_routing_guard.py"),
    @("tests/test_model_tools.py"),
    @("tests/agent/test_skill_commands.py"),
    @("tests/test_cli_preloaded_skills.py", "tests/cli/test_cli_preloaded_skills.py"),
    @("tests/test_api_key_providers.py", "tests/hermes_cli/test_api_key_providers.py"),
    @("tests/test_auth_commands.py", "tests/hermes_cli/test_auth_commands.py")
)

$testFiles = New-Object System.Collections.Generic.List[string]
foreach ($candidateGroup in $testFileCandidates) {
    $resolved = $null
    foreach ($candidate in $candidateGroup) {
        $fullPath = Join-Path $RepoRoot $candidate
        if (Test-Path $fullPath) {
            $resolved = $candidate
            break
        }
    }
    if (-not $resolved) {
        throw "Could not find any expected routing contract test file in candidates: $($candidateGroup -join ', ')"
    }
    $testFiles.Add($resolved)
}

Write-Host "Running Hermes routing contract suite..."
Write-Host "Repo: $RepoRoot"

$args = @(
    "-m", "pytest",
    "-o", "addopts="
) + $testFiles + @("-q")

Push-Location $RepoRoot
try {
    & python @args
    if ($LASTEXITCODE -ne 0) {
        throw "Routing contract suite failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Host "Routing contract suite passed."
