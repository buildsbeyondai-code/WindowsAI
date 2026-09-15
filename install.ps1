<#
.SYNOPSIS
    Installs SysPerf (System Performance Agent) for the current user.

.DESCRIPTION
    Creates a private virtual environment, installs SysPerf into it, registers a
    sign-in task so the dashboard is always available, and adds Start Menu
    shortcuts.

    No administrator rights are required. Everything is written under
    %LOCALAPPDATA%\SysPerf and the per-user Start Menu. Nothing outside those
    locations is modified, no system service is created, no registry policy is
    touched, and no system setting is changed.

.PARAMETER Port
    Port for the local dashboard. Default 5056.

.PARAMETER NoAutoStart
    Install without registering the sign-in task. Start it manually instead.

.PARAMETER NoStart
    Do not launch the dashboard after installing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Port 5100 -NoAutoStart

.NOTES
    Part of SysPerf — Apache-2.0. See LICENSE.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 5056,
    [switch]$NoAutoStart,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$AppName    = 'SysPerf'
$TaskName   = 'SysPerf Dashboard'
$InstallDir = Join-Path $env:LOCALAPPDATA $AppName
$AppDir     = Join-Path $InstallDir 'app'
$VenvDir    = Join-Path $InstallDir 'venv'
$SourceDir  = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "    $Text" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Preconditions
# ---------------------------------------------------------------------------
Write-Step 'Checking prerequisites'

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw 'PowerShell 5.1 or later is required. It ships with Windows 10 and 11.'
}

$osVer = [Environment]::OSVersion.Version
if ($osVer.Major -lt 10) {
    throw "SysPerf targets Windows 10/11. Detected $osVer."
}
if ($osVer.Build -lt 22000) {
    Write-Warn "Windows 11 is build 22000 or later; this machine is build $($osVer.Build)."
    Write-Warn 'SysPerf should still work, but it is only tested on Windows 11.'
}
Write-Ok "Windows build $($osVer.Build), PowerShell $($PSVersionTable.PSVersion)"

# ---------------------------------------------------------------------------
# 2. Locate a usable Python
# ---------------------------------------------------------------------------
Write-Step 'Locating Python 3.9 or later'

function Get-PythonCandidates {
    $found = New-Object System.Collections.Generic.List[string]
    # The py launcher is the most reliable way to find a real install.
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $listed = & $py.Source -0p 2>$null
            foreach ($line in $listed) {
                if ($line -match '([A-Za-z]:\\.+?python\.exe)\s*$') { $found.Add($Matches[1].Trim()) }
            }
        } catch {}
    }
    foreach ($name in 'python3.exe', 'python.exe') {
        foreach ($cmd in (Get-Command $name -All -ErrorAction SilentlyContinue)) {
            # The Microsoft Store alias stub is not a Python install; it only
            # opens the Store, so a venv built from it fails confusingly later.
            if ($cmd.Source -and $cmd.Source -notlike '*\WindowsApps\*') { $found.Add($cmd.Source) }
        }
    }
    return $found | Select-Object -Unique
}

function Test-PythonVersion {
    param([string]$Exe)
    try {
        $v = & $Exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if (-not $v) { return $false }
        $parts = $v.Trim().Split('.')
        return ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 9)
    } catch { return $false }
}

$python = $null
foreach ($candidate in (Get-PythonCandidates)) {
    if (Test-PythonVersion $candidate) { $python = $candidate; break }
}

if (-not $python) {
    Write-Warn 'No Python 3.9+ found.'
    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw @'
Python 3.9 or later is required and winget is unavailable to install it.

Install Python from https://www.python.org/downloads/windows/ (tick
"Add python.exe to PATH"), then run this installer again.
'@
    }
    Write-Host ''
    Write-Host '    SysPerf needs Python. It can be installed for you with:' -ForegroundColor Yellow
    Write-Host '        winget install --id Python.Python.3.12 --scope user' -ForegroundColor Yellow
    Write-Host ''
    $answer = Read-Host '    Install Python now? [y/N]'
    if ($answer -notmatch '^(y|yes)$') {
        throw 'Installation cancelled: Python is required.'
    }
    & $winget.Source install --id Python.Python.3.12 --scope user `
        --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw "winget failed with exit code $LASTEXITCODE." }
    # A fresh install is not on this session's PATH yet.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'Machine')
    foreach ($candidate in (Get-PythonCandidates)) {
        if (Test-PythonVersion $candidate) { $python = $candidate; break }
    }
    if (-not $python) {
        throw 'Python was installed but could not be located. Open a new terminal and re-run this installer.'
    }
}

$pyVer = (& $python -c "import sys;print('.'.join(map(str,sys.version_info[:3])))").Trim()
Write-Ok "Python $pyVer at $python"

# ---------------------------------------------------------------------------
# 3. Copy the application
# ---------------------------------------------------------------------------
Write-Step "Installing to $InstallDir"

foreach ($needed in 'src\sysperf\__main__.py', 'requirements.txt') {
    if (-not (Test-Path (Join-Path $SourceDir $needed))) {
        throw "Run this script from the SysPerf source directory (missing $needed)."
    }
}

# Stop a running instance so its files can be replaced.
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue |
    Stop-ScheduledTask -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
foreach ($item in 'src', 'requirements.txt', 'pyproject.toml', 'README.md', 'LICENSE', 'NOTICE') {
    $from = Join-Path $SourceDir $item
    if (Test-Path $from) {
        if ($item -eq 'src') {
            $dest = Join-Path $AppDir 'src'
            New-Item -ItemType Directory -Force -Path $dest | Out-Null
            Copy-Item (Join-Path $from '*') -Destination $dest -Recurse -Force
        } else { Copy-Item $from -Destination $AppDir -Force }
    }
}
Write-Ok 'Application files copied'

# ---------------------------------------------------------------------------
# 4. Virtual environment
# ---------------------------------------------------------------------------
Write-Step 'Creating the virtual environment'

$venvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$venvPythonW = Join-Path $VenvDir 'Scripts\pythonw.exe'

if (-not (Test-Path $venvPython)) {
    & $python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
}
& $venvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPython -m pip install --quiet --disable-pip-version-check `
    -r (Join-Path $AppDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw 'Dependency installation failed. If you are behind a corporate proxy, set HTTPS_PROXY and retry.'
}
Write-Ok 'Flask and waitress installed into the private environment'

# ---------------------------------------------------------------------------
# 5. Configuration
# ---------------------------------------------------------------------------
Write-Step 'Writing configuration'

$dataDir = $InstallDir
$cfgPath = Join-Path $dataDir 'sysperf.config.json'
if (Test-Path $cfgPath) {
    $existingCfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
    if ($existingCfg.port) { $Port = [int]$existingCfg.port }
    Write-Ok "Keeping existing configuration at $cfgPath"
} else {
    # Loopback bind with no token: the API is read-only telemetry about this
    # machine and should not be reachable from the network. Setting a non-
    # loopback host in this file makes SysPerf generate and require a token.
    $cfg = [ordered]@{ host = '127.0.0.1'; port = $Port; sample_seconds = 20 }
    $cfg | ConvertTo-Json | Set-Content -Path $cfgPath -Encoding UTF8
    Write-Ok "Created $cfgPath (loopback only, port $Port)"
}

# ---------------------------------------------------------------------------
# 6. Launchers
# ---------------------------------------------------------------------------
Write-Step 'Creating launchers'

# A generated Python shim rather than a PYTHONPATH environment variable: the
# scheduled task cannot set environment variables for its action, and setting a
# user-wide PYTHONPATH would affect every other Python process you run. The
# default data directory is already %LOCALAPPDATA%\SysPerf, so nothing else
# needs configuring.
$shim = Join-Path $InstallDir 'sysperf_launch.pyw'
@"
# Generated by install.ps1 — safe to delete; reinstalling recreates it.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'app' / 'src'))
from sysperf.__main__ import main
sys.exit(main())
"@ | Set-Content -Path $shim -Encoding UTF8

$launcher = Join-Path $InstallDir 'sysperf.cmd'
@"
@echo off
REM SysPerf launcher — starts the dashboard in this console window.
set "PYTHONPATH=$AppDir\src"
"$venvPython" -m sysperf %*
"@ | Set-Content -Path $launcher -Encoding ASCII

$reportCmd = Join-Path $InstallDir 'sysperf-report.cmd'
@"
@echo off
REM Prints a one-off performance report with recommendations.
set "PYTHONPATH=$AppDir\src"
"$venvPython" -m sysperf --report
echo.
pause
"@ | Set-Content -Path $reportCmd -Encoding ASCII

$url = "http://127.0.0.1:$Port/"
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\SysPerf'
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
@"
[InternetShortcut]
URL=$url
"@ | Set-Content -Path (Join-Path $startMenu 'SysPerf Dashboard.url') -Encoding ASCII

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $startMenu 'SysPerf Report.lnk'))
$lnk.TargetPath = $reportCmd
$lnk.WorkingDirectory = $InstallDir
$lnk.Description = 'Print a SysPerf performance report with recommendations'
$lnk.Save()
Write-Ok "Start Menu entries created under $startMenu"

# ---------------------------------------------------------------------------
# 7. Sign-in task
# ---------------------------------------------------------------------------
if ($NoAutoStart) {
    Write-Step 'Removing autostart (-NoAutoStart)'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    $oldStartup = Join-Path ([Environment]::GetFolderPath('Startup')) 'SysPerf.lnk'
    if (Test-Path $oldStartup) { Remove-Item -LiteralPath $oldStartup -Force }
} else {
    Write-Step 'Registering the sign-in task'
    $registered = $false
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        # pythonw.exe runs without a console window.
        $action = New-ScheduledTaskAction -Execute $venvPythonW -Argument "`"$shim`"" `
            -WorkingDirectory $AppDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
            -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings `
            -Description 'SysPerf — local read-only performance dashboard' | Out-Null
        $registered = $true
        Write-Ok "Scheduled task '$TaskName' registered (starts at sign-in)"
    } catch {
        Write-Warn "Task Scheduler registration failed: $($_.Exception.Message)"
    }

    if (-not $registered) {
        # Corporate policy sometimes blocks task creation. The Startup folder is
        # a per-user fallback that needs no special rights.
        $startup = [Environment]::GetFolderPath('Startup')
        $sLnk = $shell.CreateShortcut((Join-Path $startup 'SysPerf.lnk'))
        $sLnk.TargetPath = $venvPythonW
        $sLnk.Arguments = "`"$shim`""
        $sLnk.WorkingDirectory = $AppDir
        $sLnk.WindowStyle = 7
        $sLnk.Description = 'SysPerf — local read-only performance dashboard'
        $sLnk.Save()
        Write-Ok "Fallback: shortcut added to the Startup folder ($startup)"
    }
}

# ---------------------------------------------------------------------------
# 8. Start and verify
# ---------------------------------------------------------------------------
if (-not $NoStart) {
    Write-Step 'Starting SysPerf'
    Start-Process -FilePath $venvPythonW -ArgumentList "`"$shim`"" `
        -WorkingDirectory $AppDir -WindowStyle Hidden

    $ok = $false
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 750
        try {
            $r = Invoke-WebRequest -Uri "${url}api/health" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch {}
    }
    if ($ok) {
        Write-Ok "Dashboard responding at $url"
    } else {
        Write-Warn "The dashboard did not answer on $url yet."
        Write-Warn "Run `"$launcher`" in a console to see the startup output."
    }
}

Write-Host ''
Write-Host '  SysPerf installed.' -ForegroundColor Green
Write-Host ''
Write-Host "  Dashboard    $url"
Write-Host "  Report       $reportCmd"
Write-Host "  Config       $cfgPath"
Write-Host "  Data         $dataDir"
Write-Host "  Uninstall    powershell -ExecutionPolicy Bypass -File uninstall.ps1"
Write-Host ''
Write-Host '  The first full sample captures this machine''s baseline. Scores stay at' -ForegroundColor DarkGray
Write-Host '  0% until you change something — that is the point: progress is measured' -ForegroundColor DarkGray
Write-Host '  from where you started, not against someone else''s machine.' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  SysPerf only reads. It never changes a setting on your behalf.' -ForegroundColor DarkGray
Write-Host ''
