<#
    daily-app-reset.ps1 - flattens the long-term handle/memory drift.

    The Auto-Improve dashboard identified that the slow degradation of the
    handles / commit / compression / connection metrics is driven by WebView2
    app accumulation over uptime (Edge, Teams, new Outlook, ChatGPT, OneNote).
    Only restarting those apps resets the accumulation. This script closes just
    those USER apps; their embedded WebView2 children exit with them.

    It NEVER touches security (CrowdStrike, CyberArk), VPN (GlobalProtect),
    OneDrive sync, the Windows shell (explorer) or system processes. The apps
    simply reopen next time you launch them.

    Scheduled to run daily, only while the machine is idle, so it cannot close an
    app you are actively using. Run manually any time: powershell -File this.ps1
#>
$ErrorActionPreference = 'SilentlyContinue'

# User apps whose accumulation drives the drift. Parents are killed; their
# WebView2 host children exit with them. Infrastructure WebView2 (VPN/OneDrive/
# SearchHost) is deliberately NOT in this list.
$apps = @('msedge', 'OUTLOOK', 'olk', 'ms-teams', 'Teams',
          'ChatGPT', 'ChatGPT Classic', 'ONENOTE')

$before = (Get-Process | Measure-Object HandleCount -Sum).Sum
$killed = 0
foreach ($a in $apps) {
    foreach ($p in (Get-Process $a -ErrorAction SilentlyContinue)) {
        try { $p.CloseMainWindow() | Out-Null } catch { $null = $_ }
    }
}
Start-Sleep -Seconds 3
foreach ($a in $apps) {
    foreach ($p in (Get-Process $a -ErrorAction SilentlyContinue)) {
        try { Stop-Process -Id $p.Id -Force; $killed++ } catch { $null = $_ }
    }
}
Start-Sleep -Seconds 3
$after = (Get-Process | Measure-Object HandleCount -Sum).Sum

# Lightweight log so the dashboard / you can see it ran.
$logDir = 'C:\ProgramData\SysPerf'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$line = ('{0}  reset {1} app process(es); handles {2} -> {3} (freed {4})' -f `
    (Get-Date -Format o), $killed, $before, $after, ($before - $after))
Add-Content -Path (Join-Path $logDir 'daily-app-reset.log') -Value $line
Write-Output $line
