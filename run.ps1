<#
.SYNOPSIS
    Runs SysPerf from the source tree without installing it.

.DESCRIPTION
    Useful for trying SysPerf, for development, and for a one-off report on a
    machine you do not want to install anything on. Creates a virtual environment
    under .venv\ in the source directory and nothing else.

.PARAMETER Report
    Print a one-off performance report and exit instead of serving the dashboard.

.PARAMETER Once
    Take a single sample, print the raw JSON, and exit.

.PARAMETER Port
    Port for the dashboard. Default 5056.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File run.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File run.ps1 -Report
#>
[CmdletBinding()]
param(
    [switch]$Report,
    [switch]$Once,
    [int]$Port = 5056
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Host '==> Creating .venv' -ForegroundColor Cyan
    $py = $null
    foreach ($name in 'py.exe', 'python3.exe', 'python.exe') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue |
               Where-Object { $_.Source -notlike '*\WindowsApps\*' } | Select-Object -First 1
        if ($cmd) { $py = $cmd.Source; break }
    }
    if (-not $py) {
        throw 'Python 3.9+ not found. Install it from https://www.python.org/downloads/windows/'
    }
    & $py -m venv $venv
    & $venvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $venvPython -m pip install --quiet --disable-pip-version-check `
        -r (Join-Path $root 'requirements.txt')
}

$env:PYTHONPATH = Join-Path $root 'src'
if (-not $env:SYSPERF_DATA_DIR) {
    $env:SYSPERF_DATA_DIR = Join-Path $root '.sysperf-data'   # honoured since 2.8.0
}

# Not $args: that is a PowerShell automatic variable and assigning to it is
# both a warning and a source of confusing behaviour.
$cliArgs = @()
if ($Report) { $cliArgs += '--report' }
if ($Once)   { $cliArgs += '--once' }
if (-not $Report -and -not $Once) { $cliArgs += @('--port', $Port, '--open') }

& $venvPython -m sysperf @cliArgs
