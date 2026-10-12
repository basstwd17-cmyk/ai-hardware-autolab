param(
    [string]$PythonCommand = 'python',
    [switch]$SkipPython,
    [switch]$SkipFrontend
)
$ErrorActionPreference = 'Stop'
$platformRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $platformRoot '.venv\Scripts\python.exe'
$requirementsPath = Join-Path $platformRoot 'QKD_PROJ\requirements.txt'
$frontendPath = Join-Path $platformRoot 'QKD_PROJ\webui\frontend'

function Invoke-SetupCommand {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Setup command failed: $Command (exit $LASTEXITCODE)" }
}

Push-Location -LiteralPath $platformRoot
try {
    if (-not $SkipPython) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            Invoke-SetupCommand -Command $PythonCommand -Arguments @('-m', 'venv', (Join-Path $platformRoot '.venv'))
        }
        Invoke-SetupCommand -Command $venvPython -Arguments @('-m', 'pip', 'install', '-r', $requirementsPath)
    }
    if (-not $SkipFrontend) {
        Push-Location -LiteralPath $frontendPath
        try {
            Invoke-SetupCommand -Command 'npm.cmd' -Arguments @('ci')
            Invoke-SetupCommand -Command 'npm.cmd' -Arguments @('run', 'build')
        } finally {
            Pop-Location
        }
    }
    Write-Host "Platform setup completed: $platformRoot"
} finally {
    Pop-Location
}
