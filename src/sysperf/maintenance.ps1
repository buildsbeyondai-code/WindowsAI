# SysPerf maintenance checks (read-only unless -Mode names a fix).
#
#   -Mode probe                  JSON report: protection, cleanup, registry,
#                                security, crashes, services, tasks, app updates
#   -Mode fix-registry -Backup D export, then remove, the current user's start-up
#                                entries and uninstall entries whose program is gone
#   -Mode clear-browser-cache    empty the cache folders of browsers that are closed
#
# Every section is independent: one that fails reports {"error": ...} and the
# rest still run. Nothing here needs administrator rights.
param([string]$Mode = 'probe', [string]$Backup = '')
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

function Section([scriptblock]$body) {
    try { & $body } catch { @{ error = $_.Exception.Message } }
}

# --------------------------------------------------------------- helpers ---
function Get-ExePath([string]$cmd) {
    # The program a Run value or UninstallString starts, without its arguments.
    if (-not $cmd) { return $null }
    $c = [Environment]::ExpandEnvironmentVariables($cmd.Trim())
    if ($c.StartsWith('"')) { $p = $c.Substring(1, [Math]::Max(0, $c.IndexOf('"', 1) - 1)) }
    elseif ($c -match '^(.+?\.(exe|com|bat|cmd|lnk|vbs|ps1))(\s|$)') { $p = $Matches[1] }
    else { $p = ($c -split '\s+')[0] }
    if ($p -match '^(rundll32|msiexec|cmd|powershell|pwsh|wscript|cscript|explorer)(\.exe)?$') { return $null }
    if ($p -notmatch '[\\/]') { return $null }        # resolved through PATH: not checkable
    return $p
}

function Get-DirSize([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $s = (Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue |
          Measure-Object Length -Sum).Sum
    [math]::Round(($s | ForEach-Object { if ($_) { $_ } else { 0 } }) / 1MB, 1)
}

$RunKeys = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
             'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
             'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
             'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
             'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run')
$UninstallKeys = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
                   'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
                   'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')

function Get-RegistryOrphan {
    $out = @()
    foreach ($k in $RunKeys) {
        $item = Get-ItemProperty -LiteralPath $k
        if (-not $item) { continue }
        foreach ($prop in $item.PSObject.Properties) {
            if ($prop.Name -like 'PS*') { continue }
            $exe = Get-ExePath ([string]$prop.Value)
            if ($exe -and -not (Test-Path -LiteralPath $exe)) {
                $out += [pscustomobject]@{ kind = 'startup'; key = $k; name = $prop.Name
                    target = $exe; user = $k.StartsWith('HKCU') }
            }
        }
    }
    foreach ($k in $UninstallKeys) {
        foreach ($sub in (Get-ChildItem -LiteralPath $k)) {
            $p = Get-ItemProperty -LiteralPath $sub.PSPath
            if (-not $p.DisplayName -or $p.SystemComponent -eq 1) { continue }
            $exe = Get-ExePath ([string]$p.UninstallString)
            $loc = [string]$p.InstallLocation
            $gone = ($exe -and -not (Test-Path -LiteralPath $exe)) -and
                    (-not $loc -or -not (Test-Path -LiteralPath $loc))
            if ($gone) {
                $out += [pscustomobject]@{ kind = 'uninstall'; key = $sub.Name; name = [string]$p.DisplayName
                    target = $exe; user = $k.StartsWith('HKCU') }
            }
        }
    }
    $out
}

$Browsers = @(
    @{ name = 'Microsoft Edge'; proc = 'msedge'; root = "$env:LOCALAPPDATA\Microsoft\Edge\User Data" },
    @{ name = 'Google Chrome'; proc = 'chrome'; root = "$env:LOCALAPPDATA\Google\Chrome\User Data" },
    @{ name = 'Brave'; proc = 'brave'; root = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data" })
function Get-BrowserCacheDir($b) {
    if (-not (Test-Path -LiteralPath $b.root)) { return @() }
    Get-ChildItem -LiteralPath $b.root -Directory | Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' } |
        ForEach-Object { foreach ($c in 'Cache', 'Code Cache', 'GPUCache') {
            $d = Join-Path $_.FullName $c; if (Test-Path -LiteralPath $d) { $d } } }
}
function Get-FirefoxCacheDir {
    $r = "$env:LOCALAPPDATA\Mozilla\Firefox\Profiles"
    if (Test-Path -LiteralPath $r) { Get-ChildItem -LiteralPath $r -Directory | ForEach-Object {
        $d = Join-Path $_.FullName 'cache2'; if (Test-Path -LiteralPath $d) { $d } } }
}

# ------------------------------------------------------------------ fixes ---
if ($Mode -eq 'fix-registry') {
    if (-not $Backup) { 'no backup folder given - nothing changed'; exit 1 }
    New-Item -ItemType Directory -Force -Path $Backup | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $mine = @(Get-RegistryOrphan | Where-Object user)
    if (-not $mine) { 'no orphaned entries for this user - nothing changed'; exit 0 }
    $n = 0
    foreach ($o in $mine) {
        $n++
        $file = Join-Path $Backup ("registry-$stamp-$n.reg")
        $regKey = ($o.key -replace '^HKCU:\\', 'HKCU\' -replace '^HKEY_CURRENT_USER', 'HKCU')
        & reg.exe export $regKey $file /y | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $file)) {
            "backup of $regKey failed - stopped before changing it"; exit 1 }
        if ($o.kind -eq 'startup') { Remove-ItemProperty -LiteralPath $o.key -Name $o.name -ErrorAction Stop }
        else { Remove-Item -LiteralPath ('Registry::' + $o.key) -ErrorAction Stop }
        "removed $($o.kind) entry '$($o.name)' (missing $($o.target)); undo: reg import `"$file`""
    }
    exit 0
}

if ($Mode -eq 'clear-browser-cache') {
    $freed = 0
    foreach ($b in $Browsers + @(@{ name = 'Firefox'; proc = 'firefox'; ff = $true })) {
        if (Get-Process -Name $b.proc -ErrorAction SilentlyContinue) { "skipped $($b.name): it is running"; continue }
        $dirs = if ($b.ff) { Get-FirefoxCacheDir } else { Get-BrowserCacheDir $b }
        foreach ($d in $dirs) {
            $before = Get-DirSize $d
            Get-ChildItem -LiteralPath $d -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
            $freed += $before - (Get-DirSize $d)
        }
        if ($dirs) { "cleared $($b.name) cache" }
    }
    "freed {0:N0} MB" -f $freed
    exit 0
}

# ------------------------------------------------------------------ probe ---
$now = Get-Date
$report = [ordered]@{ at = [DateTimeOffset]::Now.ToUnixTimeSeconds() }

$report.protection = Section {
    # Restore points themselves need admin to list; the event System Restore
    # writes when it creates one (8194) is readable. No provider = none recorded.
    $rp = $null
    try { $rp = Get-WinEvent -FilterHashtable @{ LogName = 'Application'; ProviderName = 'Microsoft-Windows-SystemRestore'; Id = 8194 } -MaxEvents 1 -ErrorAction Stop } catch { Write-Verbose "no such event: $_" }
    $cbs = $null
    try { $cbs = Get-WinEvent -LogName Setup -MaxEvents 1 -ErrorAction Stop } catch { Write-Verbose "no such event: $_" }
    [ordered]@{
        last_restore_point = if ($rp) { [DateTimeOffset]::new($rp.TimeCreated).ToUnixTimeSeconds() } else { $null }
        last_servicing = if ($cbs) { [DateTimeOffset]::new($cbs.TimeCreated).ToUnixTimeSeconds() } else { $null }
    }
}

$report.cleanup = Section {
    $items = @()
    $items += [ordered]@{ id = 'temp'; label = 'Your TEMP folder'; mb = Get-DirSize $env:TEMP; fix = 'clear_temp' }
    $items += [ordered]@{ id = 'crashdumps'; label = 'App crash dumps'; mb = Get-DirSize "$env:LOCALAPPDATA\CrashDumps"; fix = $null }
    foreach ($b in $Browsers) {
        $dirs = @(Get-BrowserCacheDir $b)
        if ($dirs) { $items += [ordered]@{ id = $b.proc; label = "$($b.name) cache"
            mb = [math]::Round((($dirs | ForEach-Object { Get-DirSize $_ }) | Measure-Object -Sum).Sum, 1); fix = 'clear_browser_cache' } }
    }
    $ff = @(Get-FirefoxCacheDir)
    if ($ff) { $items += [ordered]@{ id = 'firefox'; label = 'Firefox cache'
        mb = [math]::Round((($ff | ForEach-Object { Get-DirSize $_ }) | Measure-Object -Sum).Sum, 1); fix = 'clear_browser_cache' } }
    $wu = "$env:SystemRoot\SoftwareDistribution\Download"
    $items += [ordered]@{ id = 'wucache'; label = 'Windows Update download cache'; mb = Get-DirSize $wu; fix = 'windows_update_cleanup' }
    # Large files in the folders people save to, newest scan wins; capped so a
    # huge profile cannot stall the probe.
    $roots = 'Downloads', 'Desktop', 'Documents', 'Videos' | ForEach-Object { Join-Path $env:USERPROFILE $_ } | Where-Object { Test-Path -LiteralPath $_ }
    $big = Get-ChildItem -LiteralPath $roots -Recurse -Force -File -ErrorAction SilentlyContinue |
        Where-Object Length -ge 500MB | Sort-Object Length -Descending | Select-Object -First 10 |
        ForEach-Object { [ordered]@{ path = $_.FullName; mb = [math]::Round($_.Length / 1MB); modified = [DateTimeOffset]::new($_.LastWriteTime).ToUnixTimeSeconds() } }
    [ordered]@{ items = $items; large_files = @($big) }
}

$report.registry = Section {
    $o = @(Get-RegistryOrphan)
    [ordered]@{ orphans = @($o | ForEach-Object { [ordered]@{ kind = $_.kind; key = $_.key; name = $_.name; target = $_.target; user = [bool]$_.user } })
                checked = @{ startup_keys = $RunKeys.Count; uninstall_keys = $UninstallKeys.Count } }
}

$report.security = Section {
    $mp = Get-MpComputerStatus
    $fw = @(Get-NetFirewallProfile | ForEach-Object { [ordered]@{ name = [string]$_.Name; enabled = [bool]$_.Enabled } })
    $av = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct |
        ForEach-Object { [ordered]@{ name = [string]$_.displayName; active = (([int]$_.productState -shr 12) -band 0xF) -eq 1 } })
    [ordered]@{
        defender = if ($mp) { [ordered]@{ service = [bool]$mp.AMServiceEnabled; realtime = [bool]$mp.RealTimeProtectionEnabled
            signature_age_days = [int]$mp.AntivirusSignatureAge; quick_scan_age_days = [int]$mp.QuickScanAge
            mode = [string]$mp.AMRunningMode } } else { $null }
        antivirus = $av; firewall = $fw }
}

$report.crashes = Section {
    $since = $now.AddDays(-30)
    $bsod = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WER-SystemErrorReporting'; Id = 1001; StartTime = $since } |
        ForEach-Object { $code = if ($_.Message -match '0x[0-9a-fA-F]{8}') { $Matches[0] } else { '' }
            [ordered]@{ at = [DateTimeOffset]::new($_.TimeCreated).ToUnixTimeSeconds(); code = $code } })
    $power = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-Power'; Id = 41; StartTime = $since }).Count
    $apps = Get-WinEvent -FilterHashtable @{ LogName = 'Application'; ProviderName = 'Application Error'; Id = 1000; StartTime = $now.AddDays(-7) } |
        ForEach-Object { $_.Properties[0].Value } | Group-Object | Sort-Object Count -Descending | Select-Object -First 5 |
        ForEach-Object { [ordered]@{ app = [string]$_.Name; count = $_.Count } }
    [ordered]@{ bugchecks_30d = $bsod; unexpected_shutdowns_30d = $power; app_crashes_7d = @($apps) }
}

$report.services = Section {
    $svc = Get-CimInstance Win32_Service -Filter "StartMode='Auto'" |
        Where-Object { $_.PathName -and $_.PathName -notmatch '\\Windows\\(system32|SysWOW64|servicing|Microsoft\.NET)\\' -and $_.PathName -notmatch 'svchost\.exe' }
    @($svc | Sort-Object DisplayName | Select-Object -First 60 | ForEach-Object {
        [ordered]@{ name = [string]$_.Name; display = [string]$_.DisplayName; state = [string]$_.State; delayed = [bool]$_.DelayedAutoStart } })
}

$report.tasks = Section {
    @(Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' -and $_.State -ne 'Disabled' } |
        Select-Object -First 60 | ForEach-Object { $i = $_ | Get-ScheduledTaskInfo
            [ordered]@{ name = [string]$_.TaskName; path = [string]$_.TaskPath; state = [string]$_.State
                last_result = [int]$i.LastTaskResult
                last_run = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { [DateTimeOffset]::new($i.LastRunTime).ToUnixTimeSeconds() } else { $null } } })
}

$report.app_updates = Section {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return @{ available = $false; apps = @() } }
    $raw = & winget upgrade --accept-source-agreements --disable-interactivity 2>$null | Out-String
    $lines = $raw -split "`r?`n" | ForEach-Object { $_ -replace '^.*\r', '' }
    $h = [Array]::FindIndex([string[]]$lines, [Predicate[string]]{ param($l) $l -match '^Name\s+Id\s+Version\s+Available' })
    $apps = @()
    if ($h -ge 0) {
        $hdr = $lines[$h]; $ci = $hdr.IndexOf('Id'); $cv = $hdr.IndexOf('Version'); $ca = $hdr.IndexOf('Available'); $cs = $hdr.IndexOf('Source')
        foreach ($l in $lines[($h + 2)..($lines.Count - 1)]) {
            if ($l.Length -lt $ca -or $l -match '^\d+ upgrades? available' -or $l -match '^-+$') { continue }
            $end = if ($cs -gt 0 -and $l.Length -ge $cs) { $cs } else { $l.Length }
            $id = $l.Substring($ci, $cv - $ci).Trim()
            if ($id -notmatch '^[A-Za-z0-9][A-Za-z0-9._+-]*$') { continue }
            $apps += [ordered]@{ name = $l.Substring(0, $ci).Trim(); id = $id
                current = $l.Substring($cv, $ca - $cv).Trim(); available = $l.Substring($ca, $end - $ca).Trim() }
        }
    }
    # 5.4: where each app is installed. A machine-wide installer needs an
    # administrator, so an unattended update of one would wait on a UAC prompt.
    $scope = @{}; $cur = $null
    $det = & winget list --upgrade-available --details --accept-source-agreements --disable-interactivity 2>$null | Out-String
    foreach ($l in ($det -split "`r?`n")) {
        if ($l -match '^\(\d+/\d+\) .*\[([^\]]+)\]\s*$') { $cur = $Matches[1]; continue }
        if (-not $cur) { continue }
        if ($l -match '^Installed Scope:\s*(\w+)') { $scope[$cur] = $Matches[1].ToLower() }
        elseif ($l -match '^Local Identifier:\s*(MSIX|ARP\\Machine|ARP\\User)' -and -not $scope.ContainsKey($cur)) {
            $scope[$cur] = @{ 'MSIX' = 'msix'; 'ARP\Machine' = 'machine'; 'ARP\User' = 'user' }[$Matches[1]] }
    }
    foreach ($a in $apps) { $a.scope = $scope[$a.id] }
    [ordered]@{ available = $true; apps = $apps }
}

$report | ConvertTo-Json -Depth 6 -Compress
