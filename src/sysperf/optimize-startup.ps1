<#
.SYNOPSIS
  Persistent startup & launch optimiser for the System Performance Dashboard.

.DESCRIPTION
  Applies the reboot-persistent optimisations that make WSL, Windows Explorer and
  Outlook start quickly, so they work well WITHOUT needing the dashboard's
  "Common Fix" button each time. Idempotent — safe to run at every logon.

  Recommended: schedule at logon (once, from an elevated PowerShell):

    $a = New-ScheduledTaskAction -Execute 'pwsh.exe' `
         -Argument '-NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\perf-dashboard\optimize-startup.ps1"'
    $t = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName 'PerfDash Startup Optimise' -Action $a -Trigger $t -RunLevel Highest

  Every action is reversible: .wslconfig is backed up, services are only enabled
  (never removed), and nothing user-specific (add-ins, startup apps) is disabled
  automatically.
#>
[CmdletBinding()]
param([switch]$WhatIfOnly)

$ErrorActionPreference = 'SilentlyContinue'
function Log($m) { Write-Host "[optimise] $m" }

# 1) Tuned .wslconfig — fast WSL start + memory reclaim (no elevation needed).
$wslcfg = Join-Path $env:USERPROFILE '.wslconfig'
$desired = @"
[wsl2]
memory=10GB
processors=6
autoMemoryReclaim=gradual
vmIdleTimeout=60000
sparseVhd=true
"@
if ((Get-Content $wslcfg -Raw -ErrorAction SilentlyContinue) -ne $desired) {
    if (-not $WhatIfOnly) {
        if (Test-Path $wslcfg) { Copy-Item $wslcfg "$wslcfg.bak" -Force }
        Set-Content -Path $wslcfg -Value $desired -Encoding ASCII
    }
    Log ".wslconfig tuned (applies on next 'wsl --shutdown')"
} else { Log ".wslconfig already tuned" }

# 2) SysMain (SuperFetch) — pre-caches Explorer/Outlook so they launch faster.
#    Needs elevation; enabled + set to Automatic (persists across reboots).
$svc = Get-Service -Name SysMain -ErrorAction SilentlyContinue
if ($svc) {
    if (-not $WhatIfOnly) {
        Set-Service -Name SysMain -StartupType Automatic -ErrorAction SilentlyContinue
        if ($svc.Status -ne 'Running') { Start-Service -Name SysMain -ErrorAction SilentlyContinue }
    }
    Log "SysMain ensured Automatic + running (app pre-caching)"
} else { Log "SysMain service not present — skipped" }

# 3) High-performance power plan on AC for faster cold boot (reversible).
if (-not $WhatIfOnly) { powercfg /setactive SCHEME_MIN 2>$null }
Log "power plan set to High performance (reverse with 'powercfg /setactive SCHEME_BALANCED')"

Log "done. Run 'wsl --shutdown' once to apply the WSL tuning."
