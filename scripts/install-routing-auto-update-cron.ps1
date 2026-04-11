[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot = (Get-Item (Join-Path $scriptDir "..")).FullName
}

function Convert-ToWslPathInfo {
    param([string]$PathValue)
    if ($PathValue -match '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.+)$') {
        return @{
            Distro = $Matches[1]
            LinuxPath = "/" + (($Matches[2] -replace '\\', '/').TrimStart('/'))
        }
    }
    return $null
}

function Escape-ShSingleQuoted {
    param([string]$Value)
    return $Value.Replace("'", "'""'""'")
}

$args = @("-m", "hermes_cli.main", "routing", "update", "install", "--repo-root", $RepoRoot)
if ($Json) {
    $args += "--json"
}

$repoWsl = Convert-ToWslPathInfo $RepoRoot
if ($repoWsl) {
    $repoLinux = Escape-ShSingleQuoted $repoWsl.LinuxPath
    $command = "cd '$repoLinux' && if [ -x venv/bin/python ]; then venv/bin/python -m hermes_cli.main routing update install --repo-root '$repoLinux'; else python3 -m hermes_cli.main routing update install --repo-root '$repoLinux'; fi"
    if ($Json) {
        $command = $command -replace '; else', " --json; else"
    }
    & wsl.exe -d $repoWsl.Distro sh -lc $command
    exit $LASTEXITCODE
}

Push-Location $RepoRoot
try {
    & python @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
