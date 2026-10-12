param(
    [ValidateSet('daemon', 'web')][string]$Component = 'daemon',
    [string]$ProjectRoot = '',
    [ValidateSet('offline', 'hardware')][string]$Profile = 'offline',
    [string]$Pipe = '\\.\pipe\autolab-web',
    [string]$ProjectId = ''
)
$ErrorActionPreference = 'Stop'
$platformRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPython = Join-Path $platformRoot '.venv\Scripts\python.exe'
$pythonCommand = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { (Get-Command python -ErrorAction Stop).Source }
Push-Location -LiteralPath $platformRoot
try {
    if ($Component -eq 'web') {
        $staticPath = Join-Path $platformRoot 'QKD_PROJ\webui\frontend\dist'
        if (-not (Test-Path -LiteralPath (Join-Path $staticPath 'index.html'))) {
            throw 'Build the frontend first: cd QKD_PROJ/webui/frontend; npm.cmd ci; npm.cmd run build'
        }
        & $pythonCommand -m QKD_PROJ.webui.backend.app --daemon $Pipe --static $staticPath
    } else {
        if (-not $ProjectRoot) { throw 'Pass -ProjectRoot with the Git checkout you want to develop.' }
        $sourceRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
        $daemonArguments = @('-m', 'QKD_PROJ.orchestrator', '--project-root', $sourceRoot, '--ledger', 'QKD_PROJ/webui/state/web-jobs.db', '--runs', 'QKD_PROJ/runs', '--knowledge-db', 'QKD_PROJ/runs/knowledge.db', '--profile', $Profile, '--pipe', $Pipe)
        if ($ProjectId) { $daemonArguments += @('--knowledge-project', $ProjectId) }
        & $pythonCommand @daemonArguments
    }
    if ($LASTEXITCODE -ne 0) { throw "Platform process exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
