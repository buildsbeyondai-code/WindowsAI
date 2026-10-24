<#
.SYNOPSIS
    Removes SysPerf for the current user.

.DESCRIPTION
    Stops the dashboard, removes the sign-in task, Start Menu entries, the
    virtual environment and the application files. Collected history is kept by
    default so a later reinstall can still show your baseline; pass -Purge to
    delete it too.

    No administrator rights are required.

.PARAMETER Purge
    Also delete collected data: history.jsonl, baseline.json, adaptive_state.json,
    hw_watchlist.json and sysperf.config.json.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File uninstall.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Purge
#>
[CmdletBinding()]
param([switch]$Purge)

$ErrorActionPreference = 'Continue'

$AppName    = 'SysPerf'
$TaskName   = 'SysPerf Dashboard'
$InstallDir = Join-Path $env:LOCALAPPDATA $AppName
$startMenu  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\SysPerf'

function Write-Step { param([string]$T) Write-Host "==> $T" -ForegroundColor Cyan }
function Write-Ok   { param([string]$T) Write-Host "    $T" -ForegroundColor Green }

Write-Step 'Stopping SysPerf'
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue |
    Stop-ScheduledTask -ErrorAction SilentlyContinue
# Only processes started from this install are stopped — a python.exe running
# something else is left alone.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" } |
    ForEach-Object {
        Write-Ok "Stopping PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Write-Step 'Removing the sign-in task'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'SysPerf.lnk'
if (Test-Path $startupLnk) { Remove-Item $startupLnk -Force }
Write-Ok 'Autostart removed'

Write-Step 'Removing Start Menu entries'
if (Test-Path $startMenu) { Remove-Item $startMenu -Recurse -Force }
Write-Ok 'Shortcuts removed'

# Older versions set these; current ones do not. Cleaned up either way.
Write-Step 'Removing environment variables'
[Environment]::SetEnvironmentVariable('SYSPERF_DATA_DIR', $null, 'User')
$pp = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'User')
if ($pp) {
    $kept = ($pp -split ';' | Where-Object { $_ -and $_ -notlike "*$InstallDir*" }) -join ';'
    if ($kept) { [Environment]::SetEnvironmentVariable('PYTHONPATH', $kept, 'User') }
    else { [Environment]::SetEnvironmentVariable('PYTHONPATH', $null, 'User') }
}
Write-Ok 'User environment cleaned'

Write-Step 'Removing application files'
foreach ($sub in 'app', 'venv', 'sysperf.cmd', 'sysperf-report.cmd',
                 'sysperf_launch.pyw') {
    $p = Join-Path $InstallDir $sub
    if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
}
Write-Ok 'Program files removed'

if ($Purge) {
    Write-Step 'Purging collected data'
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Ok "Deleted $InstallDir"
} else {
    $kept = @()
    foreach ($f in 'history.jsonl', 'baseline.json', 'adaptive_state.json',
                   'hw_watchlist.json', 'sysperf.config.json') {
        if (Test-Path (Join-Path $InstallDir $f)) { $kept += $f }
    }
    if ($kept.Count) {
        Write-Host ''
        Write-Host "  Your data was kept in $InstallDir" -ForegroundColor Yellow
        Write-Host "    $($kept -join ', ')" -ForegroundColor Yellow
        Write-Host '  Re-run with -Purge to delete it.' -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host '  SysPerf uninstalled.' -ForegroundColor Green
Write-Host '  Nothing else on the system was changed — SysPerf never modified any' -ForegroundColor DarkGray
Write-Host '  Windows setting, so there is nothing to revert.' -ForegroundColor DarkGray
Write-Host ''
