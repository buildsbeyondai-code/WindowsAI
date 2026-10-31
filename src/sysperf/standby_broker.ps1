<#
    standby_broker.ps1 - on-demand elevated helper for the Auto-Improve loop.

    WHY THIS EXISTS
    ---------------
    Emptying the Windows standby (cached) memory list needs Administrator.
    The dashboard's auto-improve loop runs unprivileged inside WSL and cannot
    elevate itself. This watcher is the one small elevated piece: it does NOTHING
    on a timer - it only acts when the loop drops a request, and the loop only
    drops a request when the memory scores are low. So the clear happens exactly
    when needed, never on a schedule.

    HOW TO RUN (once per Windows session)
    -------------------------------------
      1. Right-click Windows PowerShell  ->  "Run as administrator".
      2. (first time only) Allow this script to run:
             Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
      3. Have EITHER tool on PATH or next to this script:
             - EmptyStandbyList.exe   (preferred, headless)  OR
             - RAMMap.exe             (Sysinternals; '-Et' empties the list)
      4. Run:
             .\standby_broker.ps1
      5. Leave the window running. It prints a line each time it clears the list.

    To stop: press Ctrl+C or close the window. While it is not running, the
    dashboard automatically falls back to showing the manual RAMMap instructions.
#>
[CmdletBinding()]
param(
    [string]$ExchangeDir = $env:PERFDASH_STANDBY_DIR_WIN,
    [double]$HeartbeatEverySec = 20,
    [string]$Tool = ""        # optional explicit path to EmptyStandbyList/RAMMap
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ExchangeDir)) {
    $ExchangeDir = "C:\ProgramData\SysPerf\standby"
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Error "standby_broker must run elevated. Re-open PowerShell as Administrator."
    exit 1
}

# --- exchange directory (writable by the unprivileged dashboard user) --------
New-Item -ItemType Directory -Force -Path $ExchangeDir | Out-Null
try {
    $acl  = Get-Acl $ExchangeDir
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "Users", "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $ExchangeDir -AclObject $acl
} catch {
    Write-Warning "Could not widen ACLs on $ExchangeDir ($($_.Exception.Message)). Continuing."
}

# --- locate a standby-clearing tool ------------------------------------------
function Resolve-Tool {
    param([string]$Explicit)
    $here = $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($here)) { $here = (Get-Location).Path }
    $cands = @()
    if ($Explicit) { $cands += $Explicit }
    foreach ($n in @("EmptyStandbyList.exe", "RAMMap64.exe", "RAMMap.exe")) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if ($cmd) { $cands += $cmd.Source }
        $cands += (Join-Path $here $n)
    }
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path } }
    return $null
}

function Invoke-Clear {
    param([string]$ToolPath)
    $name = [IO.Path]::GetFileNameWithoutExtension($ToolPath).ToLower()
    if ($name -eq "emptystandbylist") {
        $p = Start-Process -FilePath $ToolPath -ArgumentList "standbylist" -Wait -PassThru -NoNewWindow
        return @{ ok = ($p.ExitCode -eq 0); detail = "EmptyStandbyList rc=$($p.ExitCode)" }
    }
    else {   # RAMMap: -Et empties the standby list and exits
        $p = Start-Process -FilePath $ToolPath -ArgumentList "-Et" -Wait -PassThru
        return @{ ok = ($p.ExitCode -eq 0); detail = "RAMMap -Et rc=$($p.ExitCode)" }
    }
}

$tool = Resolve-Tool -Explicit $Tool
if (-not $tool) {
    Write-Error "No standby-clear tool found. Put EmptyStandbyList.exe or RAMMap.exe on PATH or next to this script."
    exit 1
}

$hb = Join-Path $ExchangeDir "broker.alive"
Write-Information -InformationAction Continue "standby_broker online"
Write-Information -InformationAction Continue "  exchange : $ExchangeDir"
Write-Information -InformationAction Continue "  tool     : $tool"
Write-Information -InformationAction Continue "  heartbeat: every $HeartbeatEverySec s   (Ctrl+C to stop)"
Set-Content -LiteralPath $hb -Value (Get-Date -Format o) -Encoding ASCII

# --- launch-time publisher ----------------------------------------------------
# The Diagnostics-Performance log that records boot and app-launch durations is
# admin-only, so the unprivileged dashboard collector cannot read it. This
# elevated watcher reads it and publishes the values to launch_times.json, which
# the collector merges in. Read-only: only reads event logs.
function Get-AppStart($evts, [string]$m) {
    foreach ($e in $evts) {
        $xx = [xml]$e.ToXml()
        $nm = ($xx.Event.EventData.Data | Where-Object { $_.Name -eq 'Name' }).'#text'
        if ($nm -and $nm -match $m) {
            $tt = ($xx.Event.EventData.Data | Where-Object { $_.Name -eq 'TotalTime' }).'#text'
            if ($tt) { return [math]::Round([double]$tt / 1000, 1) }
        }
    }
    return $null
}
function Publish-LaunchTime([string]$dir) {
    $o = [ordered]@{ at = (Get-Date -Format o) }
    try {
        $b = Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 100
        } -MaxEvents 1 -ErrorAction Stop
        $bx = [xml]$b.ToXml()
        $bt = ($bx.Event.EventData.Data | Where-Object { $_.Name -eq 'BootTime' }).'#text'
        if ($bt) { $o.boot_time_s = [math]::Round([double]$bt / 1000, 1) }
    } catch { $null = $_ }
    try {
        $evts = Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 101, 103
        } -MaxEvents 500 -ErrorAction Stop
        $v = Get-AppStart $evts 'explorer\.exe';        if ($null -ne $v) { $o.explorer_launch_s = $v }
        $v = Get-AppStart $evts 'outlook\.exe';         if ($null -ne $v) { $o.outlook_launch_s = $v }
        $v = Get-AppStart $evts 'winword\.exe';         if ($null -ne $v) { $o.word_launch_s = $v }
        $v = Get-AppStart $evts 'wsl|lxss|LxssManager'; if ($null -ne $v) { $o.wsl_start_s = $v }
    } catch { $null = $_ }
    # Interactive launches (Word/Excel/PowerPoint) never appear in the boot-phase
    # Diagnostics-Performance log; they are timed live in Watch-Launch and
    # merged here so they persist across the periodic event re-reads.
    foreach ($k in $script:activeLaunch.Keys) { $o[$k] = $script:activeLaunch[$k] }
    try {
        $o | ConvertTo-Json -Compress |
            Set-Content -LiteralPath (Join-Path $dir 'launch_times.json') -Encoding ASCII
    } catch { $null = $_ }
}

$lastBeat = Get-Date
$lastLaunch = (Get-Date).AddMinutes(-10)   # force a publish on first loop

# Interactive-launch timing. Boot-phase events never record a manually opened
# app, so time it directly: catch the process just after it starts and measure
# to when its main window appears.
$script:activeLaunch = @{}                 # metric key -> seconds (merged on publish)
$script:seenPids = @{}                     # pid -> $true (already handled)
$script:pending = @{}                      # pid -> @{ start=<DateTime>; key=<metric> }
$LaunchMap = @{ "WINWORD" = "word_launch_s"; "EXCEL" = "excel_launch_s";
                "POWERPNT" = "powerpoint_launch_s" }

function Watch-Launch {
    $new = $false
    foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
        $key = $LaunchMap[$p.ProcessName]
        if (-not $key) { continue }
        if (-not $script:seenPids.ContainsKey($p.Id)) {
            $script:seenPids[$p.Id] = $true
            try { $started = $p.StartTime } catch { $started = $null }
            # only time a genuinely fresh launch (started in the last ~10 s), so
            # apps already open when the watcher starts are not mis-measured
            if ($started -and ((Get-Date) - $started).TotalSeconds -lt 10) {
                $script:pending[$p.Id] = @{ start = $started; key = $key }
            }
        }
        if ($script:pending.ContainsKey($p.Id) -and $p.MainWindowHandle -ne 0) {
            $secs = [math]::Round(((Get-Date) - $script:pending[$p.Id].start).TotalSeconds, 1)
            $script:activeLaunch[$script:pending[$p.Id].key] = $secs
            $script:pending.Remove($p.Id)
            Write-Information -InformationAction Continue ("[{0}] measured {1} = {2}s" -f (Get-Date -Format o),
                        $key, $secs)
            $new = $true
        }
    }
    # forget exited PIDs so a relaunch is timed again
    $live = @{}; foreach ($p in Get-Process -ErrorAction SilentlyContinue) { $live[$p.Id] = $true }
    foreach ($oldPid in @($script:seenPids.Keys)) { if (-not $live.ContainsKey($oldPid)) { $script:seenPids.Remove($oldPid) } }
    return $new
}

while ($true) {
    # time any interactive app launch (Word/Excel/PowerPoint) and publish at once
    if (Watch-Launch) { Publish-LaunchTime $ExchangeDir; $lastLaunch = Get-Date }
    # publish launch times (boot / app cold-start) every ~2 minutes
    if (((Get-Date) - $lastLaunch).TotalSeconds -ge 120) {
        Publish-LaunchTime $ExchangeDir
        $lastLaunch = Get-Date
    }
    # refresh the heartbeat so the dashboard knows the watcher is alive
    if (((Get-Date) - $lastBeat).TotalSeconds -ge $HeartbeatEverySec) {
        Set-Content -LiteralPath $hb -Value (Get-Date -Format o) -Encoding ASCII
        $lastBeat = Get-Date
    }
    # service any pending requests
    $reqs = Get-ChildItem -LiteralPath $ExchangeDir -Filter "*.request" -ErrorAction SilentlyContinue
    foreach ($r in $reqs) {
        $res = [IO.Path]::ChangeExtension($r.FullName, ".result")
        try {
            $out = Invoke-Clear -ToolPath $tool
            $stamp = Get-Date -Format o
            if ($out.ok) {
                Set-Content -LiteralPath $res -Value "OK $($out.detail) $stamp" -Encoding ASCII
                Write-Information -InformationAction Continue "[$stamp] cleared standby list ($($out.detail))"
            } else {
                Set-Content -LiteralPath $res -Value "ERR $($out.detail) $stamp" -Encoding ASCII
                Write-Information -InformationAction Continue "[$stamp] clear FAILED ($($out.detail))"
            }
        } catch {
            Set-Content -LiteralPath $res -Value "ERR $($_.Exception.Message)" -Encoding ASCII
            Write-Information -InformationAction Continue "clear FAILED: $($_.Exception.Message)"
        } finally {
            Remove-Item -LiteralPath $r.FullName -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 500
}
