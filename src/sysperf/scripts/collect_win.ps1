# =============================================================================
#  SysPerf — Windows metrics collector
#  Part of the System Performance Agent (https://github.com/) — Apache-2.0
# =============================================================================
#  Emits ONE compact JSON object on stdout.
#
#  READ-ONLY BY DESIGN. Every call in this file is a query: Get-CimInstance,
#  Get-Process, Get-ItemProperty, pnputil /enum-drivers, powercfg /getactivescheme.
#  Nothing is written, started, stopped or reconfigured. Run it yourself to check.
#
#  -Full also gathers the slow tier (services, startup, network, hardware,
#  drivers, platform facts). Omit it for fast polling.
#
#  Portability notes:
#   * Performance data comes from the Win32_PerfFormattedData_* CIM classes, not
#     from Get-Counter with English counter paths — counter names are localised,
#     so the Get-Counter approach fails outright on a non-English Windows.
#   * The system volume is taken from $env:SystemDrive rather than assuming C:.
#   * Power plans are reported by GUID as well as name, because the display name
#     is localised and cannot be matched on reliably.
#   * Targets PowerShell 5.1 (shipped with every Windows 10/11) — no PS7-only
#     syntax such as ternary or null-coalescing operators.
# =============================================================================
param([switch]$Full)
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Get-SafeValue {
    param([scriptblock]$Block, $Default = $null)
    try { $v = & $Block; if ($null -eq $v) { return $Default } return $v }
    catch { return $Default }
}

$os   = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
$all  = Get-CimInstance Win32_Process
$proc = Get-Process

# --- memory / kernel / cpu counters (locale-independent CIM classes) ---------
$mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$sys = Get-CimInstance Win32_PerfFormattedData_PerfOS_System
$cpu = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor |
       Where-Object { $_.Name -eq '_Total' } | Select-Object -First 1

$cpuPct = $null
if ($cpu -and $null -ne $cpu.PercentProcessorTime) { $cpuPct = [double]$cpu.PercentProcessorTime }
if ($null -eq $cpuPct) {
    $cpuPct = Get-SafeValue { [double](Get-CimInstance Win32_Processor |
              Measure-Object -Property LoadPercentage -Average).Average }
}

# Disk active-time (% of the interval the physical disks were busy). Localised-
# name-free via the CIM perf class; _Total aggregates all physical disks.
$physDisk = Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -eq '_Total' } | Select-Object -First 1
$diskActive = $(if ($physDisk -and $null -ne $physDisk.PercentIdleTime) {
    [math]::Round([math]::Max(0, [math]::Min(100, 100 - [double]$physDisk.PercentIdleTime)), 1) } else { $null })

function MB($bytes) { if ($null -eq $bytes) { return $null } return [math]::Round([double]$bytes / 1MB, 0) }

# --- embedded-browser (WebView2) attribution via ancestor walk ---------------
# Each msedgewebview2.exe belongs to a host application. Walking up the parent
# chain attributes the process to the app that embedded it, which is what makes
# "can I close this?" answerable.
$idx = @{}
foreach ($p in $all) { $idx[[int]$p.ProcessId] = $p }
$edgeProcs = @($all | Where-Object { $_.Name -eq 'msedge.exe' })
$wv2Procs  = @($all | Where-Object { $_.Name -eq 'msedgewebview2.exe' })

$wvHosts = @{}
foreach ($w in $wv2Procs) {
    $c = $idx[[int]$w.ProcessId]; $g = 0
    while ($c -and $c.Name -eq 'msedgewebview2.exe' -and $g -lt 12) {
        $c = $idx[[int]$c.ParentProcessId]; $g++
    }
    if ($c) { $h = $c.Name -replace '\.exe$', '' } else { $h = 'orphan' }
    if ($wvHosts.ContainsKey($h)) { $wvHosts[$h] += 1 } else { $wvHosts[$h] = 1 }
}

# --- standalone browser composition + extension footprint -------------------
$eMain = 0; $eRend = 0; $eExt = 0; $eUtil = 0; $extWS = 0
$wsById = @{}
foreach ($q in $proc) { $wsById[[int]$q.Id] = $q.WS }
foreach ($e in $edgeProcs) {
    $cl = $e.CommandLine
    if     ($cl -match '--extension-process') { $eExt++; $extWS += [double]$wsById[[int]$e.ProcessId] }
    elseif ($cl -match '--type=renderer')     { $eRend++ }
    elseif ($cl -match '--type=')             { $eUtil++ }
    else                                      { $eMain++ }
}

# --- system volume (never assume C:) ----------------------------------------
$sysDrive = $env:SystemDrive
if (-not $sysDrive) { $sysDrive = 'C:' }
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$sysDrive'"

$comp = Get-Process 'Memory Compression'
$vmm  = Get-Process 'vmmemWSL'
if (-not $vmm) { $vmm = Get-Process 'vmmem' }     # older WSL builds

# --- top consumers (cheap: reuses the process list already gathered) --------
$topMem = @($proc | Group-Object ProcessName | ForEach-Object {
    [pscustomobject]@{
        name  = $_.Name
        mb    = [math]::Round((($_.Group | Measure-Object WS -Sum).Sum) / 1MB, 0)
        count = $_.Count
    } } | Sort-Object mb -Descending | Select-Object -First 8)

$topHandles = @($proc | Group-Object ProcessName | ForEach-Object {
    [pscustomobject]@{
        name    = $_.Name
        handles = (($_.Group | Measure-Object Handles -Sum).Sum)
        count   = $_.Count
    } } | Sort-Object handles -Descending | Select-Object -First 6)

$out = [ordered]@{
    ts               = (Get-Date).ToString('o')
    boot             = $os.LastBootUpTime.ToString('o')
    uptime_h         = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 2)
    committed_mb     = MB $mem.CommittedBytes
    commit_limit_mb  = MB $mem.CommitLimit
    available_mb     = [math]::Round($os.FreePhysicalMemory / 1KB, 0)
    total_ram_mb     = [math]::Round($os.TotalVisibleMemorySize / 1KB, 0)
    compression_mb   = $(if ($comp) { [math]::Round($comp.WS / 1MB, 0) } else { 0 })
    vmmem_mb         = $(if ($vmm)  { [math]::Round($vmm.WS  / 1MB, 0) } else { 0 })
    pool_paged_mb    = MB $mem.PoolPagedBytes
    pool_nonpaged_mb = MB $mem.PoolNonpagedBytes
    cache_faults_s   = Get-SafeValue { [int]$mem.CacheFaultsPersec }
    page_faults_s    = Get-SafeValue { [int]$mem.PageFaultsPersec }
    handles          = ($proc | Measure-Object Handles -Sum).Sum
    threads          = ($all | Measure-Object ThreadCount -Sum).Sum
    processes        = $os.NumberOfProcesses
    edge_procs       = $edgeProcs.Count
    wv2_procs        = $wv2Procs.Count
    browser_procs    = $edgeProcs.Count + $wv2Procs.Count
    edge_main        = $eMain
    edge_renderer    = $eRend
    edge_extension   = $eExt
    edge_utility     = $eUtil
    edge_ext_mb      = [math]::Round($extWS / 1MB, 0)
    wv2_hosts        = $wvHosts
    svchost_procs    = @($proc | Where-Object { $_.ProcessName -eq 'svchost' }).Count
    search_procs     = @($proc | Where-Object {
                          $_.ProcessName -in 'SearchIndexer', 'SearchFilterHost',
                                             'SearchProtocolHost', 'SearchHost' }).Count
    system_drive     = $sysDrive
    c_free_gb        = [math]::Round($disk.FreeSpace / 1GB, 1)
    c_size_gb        = [math]::Round($disk.Size / 1GB, 1)
    cpu_queue        = Get-SafeValue { [int]$sys.ProcessorQueueLength } 0
    cpu_pct          = $(if ($null -ne $cpuPct) { [math]::Round($cpuPct, 1) } else { $null })
    disk_active_pct  = $diskActive
    top_mem          = @($topMem | ForEach-Object { @{ name = $_.name; mb = $_.mb; count = $_.count } })
    top_handles      = @($topHandles | ForEach-Object { @{ name = $_.name; handles = $_.handles; count = $_.count } })
}

# --- Wi-Fi connection (fast path) -------------------------------------------
# Get-NetAdapter + Get-NetConnectionProfile work without elevation or the
# Location privacy permission and give the SSID, negotiated link speed, adapter
# model and network category. netsh wlan is then TRIED for the richer signal% /
# band / channel / Rx-Tx detail; on Windows 11 that is gated behind Location
# services and can fail, in which case detail_limited is flagged. READ-ONLY.
$wifi = [ordered]@{ present = $false }
$wnic = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.PhysicalMediaType -match '802\.11' } | Select-Object -First 1
if ($wnic -and $wnic.Status -eq 'Up') {
    $wifi.present = $true
    $wifi.state   = 'connected'
    $wifi.adapter = [string]$wnic.InterfaceDescription
    $ls = [string]$wnic.LinkSpeed; $lm = 0.0
    if ($ls -match '([\d.]+)\s*(\w+)') {
        $lm = [double]$Matches[1]
        if     ($Matches[2] -match 'Gbps') { $lm *= 1000 }
        elseif ($Matches[2] -match 'Kbps') { $lm /= 1000 }
    }
    $wifi.link_mbps = [math]::Round($lm, 0)
    if     ($wnic.InterfaceDescription -match 'Wi-Fi 7|BE2\d\d')   { $wifi.radio = '802.11be (Wi-Fi 7)' }
    elseif ($wnic.InterfaceDescription -match 'Wi-Fi 6E')          { $wifi.radio = '802.11ax (Wi-Fi 6E)' }
    elseif ($wnic.InterfaceDescription -match 'Wi-Fi 6|AX2\d\d')   { $wifi.radio = '802.11ax (Wi-Fi 6)' }
    elseif ($wnic.InterfaceDescription -match 'AC 9\d\d\d|Wi-Fi 5'){ $wifi.radio = '802.11ac (Wi-Fi 5)' }
    $prof = Get-NetConnectionProfile -ErrorAction SilentlyContinue |
            Where-Object { $_.InterfaceAlias -eq $wnic.Name } | Select-Object -First 1
    if ($prof) {
        $wifi.ssid     = [string]$prof.Name
        $wifi.category = [string]$prof.NetworkCategory
        $wifi.ipv4     = [string]$prof.IPv4Connectivity
    }
    $wifi.detail_limited = $true
    $wl = (& "$env:SystemRoot\System32\netsh.exe" wlan show interfaces 2>$null) -join "`n"
    if ($wl -and $wl -match '(?im)^\s*Signal\s*:') {
        $wifi.detail_limited = $false
        function WLo([string]$label) {
            $m = [regex]::Match($wl, "(?im)^\s*$label\s*:\s*(.+?)\s*$")
            if ($m.Success) { $m.Groups[1].Value.Trim() } else { $null }
        }
        if (-not $wifi.ssid) { $wifi.ssid = WLo 'SSID' }
        $wifi.bssid = WLo 'BSSID'
        $wifi.band  = WLo 'Band'
        $wifi.auth  = WLo 'Authentication'
        $rd = WLo 'Radio type'; if ($rd) { $wifi.radio = $rd }
        $ch = WLo 'Channel'; if ($ch) { $wifi.channel = [int]($ch -replace '[^\d]', '') }
        $sig = WLo 'Signal'; if ($sig) { $wifi.signal = [int]($sig -replace '[^\d]', '') }
        $rx = WLo 'Receive rate \(Mbps\)';  if ($rx) { $wifi.rx_mbps = [double]($rx -replace '[^\d.]', '') }
        $tx = WLo 'Transmit rate \(Mbps\)'; if ($tx) { $wifi.tx_mbps = [double]($tx -replace '[^\d.]', '') }
    }
}
$out.wifi = $wifi
if ($wifi.present -and $null -ne $wifi.signal) { $out.wifi_signal = $wifi.signal }

if ($Full) {
    # ---------------------------------------------------------------- machine
    $cs  = Get-CimInstance Win32_ComputerSystem
    $cpi = Get-CimInstance Win32_Processor | Select-Object -First 1
    $out.hostname       = $env:COMPUTERNAME
    $out.machine_vendor = [string]$cs.Manufacturer
    $out.machine_model  = [string]$cs.Model
    $out.cpu_name       = ([string]$cpi.Name).Trim()
    $out.cpu_cores      = [int]$cpi.NumberOfCores
    $out.cpu_threads    = [int]$cpi.NumberOfLogicalProcessors
    $out.os_caption     = [string]$os.Caption
    $out.os_build       = [string]$os.BuildNumber
    $out.os_version     = [string]$os.Version

    # --------------------------------------------------------------- services
    $svc = Get-Service
    $out.services_running = @($svc | Where-Object { $_.Status -eq 'Running' }).Count
    $out.services_total   = $svc.Count

    # ---------------------------------------------------------------- startup
    # Win32_StartupCommand lists every registered entry regardless of its Task
    # Manager enable/disable state. StartupApproved marks a disabled entry with a
    # leading 0x03 byte, so those are excluded: the metric then reflects what
    # actually launches at sign-in, and rewards disabling (which is reversible)
    # rather than demanding registry deletion.
    $disabled = New-Object System.Collections.Generic.HashSet[string]
    $saKeys = @(
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder'
    )
    foreach ($k in $saKeys) {
        $p = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
        if ($p) {
            $p.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object {
                if ($_.Value -and $_.Value[0] -eq 3) {
                    [void]$disabled.Add(($_.Name -replace '\.lnk$', '').Trim().ToLower())
                }
            }
        }
    }
    $sc = @(Get-CimInstance Win32_StartupCommand)
    $enabled = @($sc | Where-Object {
        -not $disabled.Contains(($_.Name -replace '\.lnk$', '').Trim().ToLower())
    })
    $out.startup_apps       = $enabled.Count
    $out.startup_apps_total = $sc.Count
    $out.startup_list = @($enabled | Select-Object -First 20 | ForEach-Object {
        @{ name = [string]$_.Name; location = [string]$_.Location } })

    # ---------------------------------------------------------------- network
    # Only REMOTE established connections are scored. Loopback is local IPC
    # (dev servers, containers, browser helpers), not network load, and it
    # dominates the raw count on a developer machine — including it would make
    # the metric impossible to improve.
    $tcp = Get-NetTCPConnection
    $est = @($tcp | Where-Object { $_.State -eq 'Established' })
    $remote = @($est | Where-Object {
        $_.RemoteAddress -notin '127.0.0.1', '::1', '0.0.0.0', '::' -and
        $_.RemoteAddress -notlike '169.254.*' -and $_.RemoteAddress -notlike 'fe80:*'
    })
    $out.net_remote      = $remote.Count
    $out.net_loopback    = @($est | Where-Object { $_.RemoteAddress -in '127.0.0.1', '::1' }).Count
    $out.net_established = $est.Count
    $out.net_listen      = @($tcp | Where-Object { $_.State -eq 'Listen' }).Count
    $out.net_tcp_total   = @($tcp).Count

    # active physical adapters (link speed) + connection profile
    $out.net_adapters = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' } | ForEach-Object {
            $als = [string]$_.LinkSpeed; $anum = 0.0
            if ($als -match '([\d.]+)\s*(\w+)') {
                $anum = [double]$Matches[1]
                if ($Matches[2] -match 'Gbps') { $anum *= 1000 } elseif ($Matches[2] -match 'Kbps') { $anum /= 1000 }
            }
            @{ name = [string]$_.Name; desc = [string]$_.InterfaceDescription
               link_mbps = [math]::Round($anum, 0); media = [string]$_.MediaType; status = [string]$_.Status } })
    $np = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($np) {
        $out.net_category  = [string]$np.NetworkCategory
        $out.net_ipv4_conn = [string]$np.IPv4Connectivity
        $out.net_ipv6_conn = [string]$np.IPv6Connectivity
    }

    # Wi-Fi neighbourhood scan for band/channel-overlap analysis. Needs the
    # Location permission on Windows 11; when blocked the reason is flagged so
    # the UI can explain how to enable it. READ-ONLY passive scan.
    $scan = [ordered]@{ available = $false; reason = 'unknown'; nets = @() }
    $sraw = (& "$env:SystemRoot\System32\netsh.exe" wlan show networks mode=bssid 2>$null) -join "`n"
    if ($sraw -match '(?i)location permission|requires elevation') { $scan.reason = 'location' }
    elseif ($sraw -match '(?im)^\s*BSSID \d+') {
        $scan.available = $true; $scan.reason = 'ok'; $snets = @()
        foreach ($sblk in ($sraw -split '(?im)^SSID \d+ : ')) {
            if ($sblk -notmatch 'BSSID') { continue }
            $sssid = (($sblk -split "`n")[0]).Trim()
            foreach ($bblk in ($sblk -split '(?im)^\s*BSSID \d+\s*:')) {
                if ($bblk -notmatch '(?i)Signal') { continue }
                $bsig = [regex]::Match($bblk, '(?im)^\s*Signal\s*:\s*(\d+)').Groups[1].Value
                $bch  = [regex]::Match($bblk, '(?im)^\s*Channel\s*:\s*(\d+)').Groups[1].Value
                $bbnd = [regex]::Match($bblk, '(?im)^\s*Band\s*:\s*(.+?)\s*$').Groups[1].Value
                $brad = [regex]::Match($bblk, '(?im)^\s*Radio type\s*:\s*(.+?)\s*$').Groups[1].Value
                if ($bch) { $snets += @{ ssid = $sssid; bssid = (($bblk -split "`n")[0]).Trim(); signal = [int]$bsig; channel = [int]$bch
                                         band = $bbnd.Trim(); radio = $brad.Trim() } }
            }
        }
        $scan.nets = $snets
    } else { $scan.reason = $(if ($sraw) { 'no-data' } else { 'unavailable' }) }
    $out.wifi_scan = $scan

    # --- component versions (upgrade advisory) ------------------------------
    # GPU + primary network-adapter driver, PowerShell (5.1 and 7 if present),
    # Edge, WebView2 runtime and BIOS/UEFI. Linux/WSL-side runtimes are gathered
    # by the components module. READ-ONLY.
    $gpu = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
           Where-Object { $_.DriverVersion } | Select-Object -First 1
    if ($gpu) {
        $out.gpu_name        = [string]$gpu.Name
        $out.gpu_driver      = [string]$gpu.DriverVersion
        $out.gpu_driver_date = $(if ($gpu.DriverDate) { ([datetime]$gpu.DriverDate).ToString('yyyy-MM-dd') } else { '' })
    }
    $wad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
           Where-Object { $_.Status -eq 'Up' -and $_.PhysicalMediaType -match '802\.11' } | Select-Object -First 1
    if (-not $wad) { $wad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1 }
    if ($wad) {
        $out.net_driver_name     = [string]$wad.InterfaceDescription
        $out.net_driver_ver      = [string]$wad.DriverVersion
        $out.net_driver_date     = $(if ($wad.DriverDate) { ([datetime]$wad.DriverDate).ToString('yyyy-MM-dd') } else { '' })
        $out.net_driver_provider = [string]$wad.DriverProvider
    }
    $out.ps_version = [string]$PSVersionTable.PSVersion.ToString()
    $pwshExe = @("$env:ProgramFiles\PowerShell\7\pwsh.exe", "$env:LOCALAPPDATA\Programs\PowerShell\7\pwsh.exe") |
               Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $pwshExe) { $pc = Get-Command pwsh -ErrorAction SilentlyContinue; if ($pc) { $pwshExe = $pc.Source } }
    if ($pwshExe) { $pv = Get-SafeValue { (& $pwshExe -NoProfile -Command '$PSVersionTable.PSVersion.ToString()') }; if ($pv) { $out.pwsh_version = [string]$pv } }
    # Edge lives under 32-bit Program Files on most installs, 64-bit on some
    # (ARM64 / newer channels). Probe both via environment variables rather than
    # a literal C:\ path, which breaks when Windows is not on C:.
    $edgeRoots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:ProgramW6432) |
                 Where-Object { $_ } | Select-Object -Unique
    foreach ($rt in $edgeRoots) {
        $cand = Join-Path $rt 'Microsoft\Edge\Application\msedge.exe'
        if (Test-Path $cand) { $out.edge_version = [string](Get-Item $cand).VersionInfo.ProductVersion; break }
    }
    if (-not $out.edge_version) {
        # Registry fallback (BLBeacon records the installed Edge version).
        $eb = Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Edge\BLBeacon' -ErrorAction SilentlyContinue
        if ($eb -and $eb.version) { $out.edge_version = [string]$eb.version }
    }
    # WebView2 evergreen runtime: the client GUID is registered under the 32-bit
    # view on x64, the native view on ARM64, and per-user for user-scope installs.
    $wvGuid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($wvKey in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$wvGuid",
                         "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wvGuid",
                         "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wvGuid")) {
        $wv = Get-ItemProperty $wvKey -ErrorAction SilentlyContinue
        if ($wv -and $wv.pv) { $out.webview2_version = [string]$wv.pv; break }
    }
    $bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue
    if ($bios) {
        $out.bios_version = [string]$bios.SMBIOSBIOSVersion
        $out.bios_date    = $(if ($bios.ReleaseDate) { ([datetime]$bios.ReleaseDate).ToString('yyyy-MM-dd') } else { '' })
    }

    # --------------------------------------------------------------- pagefile
    $pf = Get-CimInstance Win32_PageFileUsage | Select-Object -First 1
    if ($pf) {
        $out.pagefile_mb      = [int]$pf.AllocatedBaseSize
        $out.pagefile_peak_mb = [int]$pf.PeakUsage
        $out.pagefile_current_mb = [int]$pf.CurrentUsage
    }
    $out.pagefile_automatic = Get-SafeValue { [bool](Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile }

    # ------------------------------------------------------------- power plan
    # The display name is localised, so the GUID is reported alongside it and is
    # what callers should match on.
    $pcOut = Get-SafeValue { (& "$env:SystemRoot\System32\powercfg.exe" /getactivescheme) -join ' ' } ''
    if ($pcOut -match '([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})') { $out.power_plan_guid = $Matches[1].ToLower() }
    if ($pcOut -match '\(([^)]+)\)') { $out.power_plan = $Matches[1].Trim() }

    # --------------------------------------------------------- restart pending
    $rebootKeys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
    )
    $pending = $false
    foreach ($rk in $rebootKeys) { if (Test-Path $rk) { $pending = $true } }
    $out.restart_pending = $pending

    # ------------------------------------------------------------- protection
    $mp = Get-SafeValue { Get-MpComputerStatus }
    if ($mp) {
        $out.defender_realtime = [bool]$mp.RealTimeProtectionEnabled
        $out.defender_av_signature_age_d = Get-SafeValue { [int]$mp.AntivirusSignatureAge }
    }

    # ---------------------------------------------------------------- hardware
    # Auto-detects Device-Manager-reported faults (error codes), storage health
    # and battery state. A functional glitch that Windows still reports as OK —
    # a touchpad button that misbehaves, say — cannot be detected here; those go
    # in hw_watchlist.json.
    $pnp  = Get-CimInstance Win32_PnPEntity
    $prob = @($pnp | Where-Object { $_.ConfigManagerErrorCode -and $_.ConfigManagerErrorCode -ne 0 })
    $out.hw_problem_devices = $prob.Count
    $out.hw_problem_list = @($prob | Select-Object -First 12 | ForEach-Object {
        @{ name = [string]$_.Name; code = [int]$_.ConfigManagerErrorCode; status = [string]$_.Status } })

    $disks = @(Get-PhysicalDisk)
    $out.hw_disks = @($disks | ForEach-Object {
        @{ name = [string]$_.FriendlyName; media = [string]$_.MediaType
           health = [string]$_.HealthStatus; op = [string]$_.OperationalStatus } })
    $out.hw_disks_unhealthy = @($disks | Where-Object { $_.HealthStatus -ne 'Healthy' }).Count

    $bat = Get-CimInstance Win32_Battery | Select-Object -First 1
    if ($bat) {
        $b = @{ charge = [int]$bat.EstimatedChargeRemaining; status = [int]$bat.BatteryStatus }
        # Design vs full-charge capacity exposes wear, which caps turbo on battery.
        $bsd = Get-SafeValue { Get-CimInstance -Namespace root\wmi -Class BatteryStaticData | Select-Object -First 1 }
        $bfc = Get-SafeValue { Get-CimInstance -Namespace root\wmi -Class BatteryFullChargedCapacity | Select-Object -First 1 }
        if ($bsd -and $bsd.DesignedCapacity)      { $b.design_capacity = [int]$bsd.DesignedCapacity }
        if ($bfc -and $bfc.FullChargedCapacity)   { $b.full_capacity   = [int]$bfc.FullChargedCapacity }
        $out.hw_battery = $b
    }

    $out.hw_pointing = @(Get-CimInstance Win32_PointingDevice | ForEach-Object {
        @{ name = [string]$_.Name; status = [string]$_.Status; code = [int]$_.ConfigManagerErrorCode } })
    $out.hw_touchpad = @($pnp | Where-Object { $_.Name -match 'touch ?pad|Synaptics|Precision Touchpad' } |
        ForEach-Object {
        @{ name = [string]$_.Name; status = [string]$_.Status; code = [int]$_.ConfigManagerErrorCode } })

    # ----------------------------------------------------------------- drivers
    # pnputil /enum-drivers lists third-party (OEM) driver packages in ~4 s and
    # needs no per-driver signature verification. Win32_PnPSignedDriver is the
    # obvious alternative and is so slow it times the whole sample out.
    # READ-ONLY: /enum-drivers only lists.
    $drvList = @()
    try {
        $raw = (& "$env:SystemRoot\System32\pnputil.exe" /enum-drivers 2>$null) -join "`n"
        foreach ($blk in ($raw -split "\r?\n\s*\r?\n")) {
            if ($blk -notmatch 'Published Name') { continue }
            $pub  = ''; $orig = ''; $prov = ''; $cls = ''; $sign = ''
            if ($blk -match 'Published Name:\s*(.+)') { $pub  = $Matches[1].Trim() }
            if ($blk -match 'Original Name:\s*(.+)')  { $orig = $Matches[1].Trim() }
            if ($blk -match 'Provider Name:\s*(.+)')  { $prov = $Matches[1].Trim() }
            if ($blk -match 'Class Name:\s*(.+)')     { $cls  = $Matches[1].Trim() }
            if ($blk -match 'Signer Name:\s*(.+)')    { $sign = $Matches[1].Trim() }
            $ver = ''; $dt = $null
            if ($blk -match 'Driver Version:\s*(\S+)\s+(\S+)') {
                $dtStr = $Matches[1]; $ver = $Matches[2]
                foreach ($f in 'MM/dd/yyyy', 'M/d/yyyy', 'dd/MM/yyyy', 'd/M/yyyy', 'yyyy-MM-dd') {
                    try {
                        $dt = [datetime]::ParseExact($dtStr, $f, [Globalization.CultureInfo]::InvariantCulture)
                        break
                    } catch { Write-Verbose "Optional probe failed: $($_.Exception.Message)" }
                }
                if (-not $dt) { try { $dt = [datetime]$dtStr } catch { Write-Verbose "Optional probe failed: $($_.Exception.Message)" } }
            }
            $drvList += [pscustomobject]@{ pub = $pub; orig = $orig; provider = $prov
                                           class = $cls; signer = $sign; version = $ver; date = $dt }
        }
    } catch { Write-Verbose "Optional probe failed: $($_.Exception.Message)" }
    $out.drv_total    = $drvList.Count
    $out.drv_unsigned = @($drvList | Where-Object { -not $_.signer }).Count

    # Stale = third-party, real device-function driver, older than 4 years.
    # Excluded: synthetic placeholder dates (some vendors ship INFs dated 1968),
    # Microsoft inbox drivers (fixed old INF date but serviced via Windows
    # Update), and non-function packages (vendor control panels, driver
    # extensions, printers, firmware helpers) that a user cannot act on usefully.
    $staleCut  = (Get-Date).AddYears(-4)
    $dateFloor = [datetime]'2000-01-01'
    $optionalClasses = @('SoftwareComponent', 'Extension', 'Printer', 'PrintQueue', 'Firmware')
    $staleAll = @($drvList | Where-Object {
        $_.date -and $_.date -ge $dateFloor -and $_.date -lt $staleCut -and
        $_.provider -notmatch 'Microsoft' -and $optionalClasses -notcontains $_.class })
    # De-duplicate multiple installed copies of the same INF, keeping the newest,
    # so seven copies of one package count as one stale driver.
    $stale = @($staleAll |
        Group-Object { if ($_.orig) { $_.orig } else { $_.pub } } |
        ForEach-Object { $_.Group | Sort-Object date -Descending | Select-Object -First 1 })
    $out.drv_stale = $stale.Count
    $out.drv_stale_list = @($stale | Sort-Object date | Select-Object -First 10 | ForEach-Object {
        @{ name     = $(if ($_.orig) { [string]$_.orig } else { [string]$_.pub })
           version  = [string]$_.version
           date     = $(if ($_.date) { $_.date.ToString('yyyy-MM-dd') } else { '' })
           class    = [string]$_.class
           provider = [string]$_.provider } })

    # Driver-specific Device Manager codes (missing/corrupt/failed/unsigned),
    # distinct from generic device faults.
    $drvCodes = @(1, 3, 10, 18, 19, 28, 31, 32, 37, 39, 48, 52)
    $drvProb = @($pnp | Where-Object {
        $_.ConfigManagerErrorCode -and $drvCodes -contains [int]$_.ConfigManagerErrorCode })
    $out.drv_problem = $drvProb.Count
    $out.drv_problem_list = @($drvProb | Select-Object -First 12 | ForEach-Object {
        @{ name = [string]$_.Name; code = [int]$_.ConfigManagerErrorCode; status = [string]$_.Status } })

    # -------------------------------------------------------------------- WSL
    # Reported from the Windows side so it works whether SysPerf runs natively
    # or inside a distribution.
    $out.wsl_installed = [bool](Get-SafeValue { Get-Command wsl.exe -ErrorAction SilentlyContinue })
    $wslCfg = Join-Path $env:USERPROFILE '.wslconfig'
    $out.wsl_config_present = Test-Path $wslCfg
    if ($out.wsl_config_present) {
        $cfgTxt = Get-Content $wslCfg -Raw
        $out.wsl_autoreclaim = [bool]($cfgTxt -match '(?im)^\s*autoMemoryReclaim\s*=\s*(gradual|dropcache)')
        if ($cfgTxt -match '(?im)^\s*memory\s*=\s*(\d+)\s*([GMgm])[Bb]?') {
            $n = [double]$Matches[1]
            if ($Matches[2] -match '[Gg]') { $out.wsl_cap_mb = [int]($n * 1024) }
            else { $out.wsl_cap_mb = [int]$n }
        }
        if ($cfgTxt -match '(?im)^\s*processors\s*=\s*(\d+)') { $out.wsl_cpus_cfg = [int]$Matches[1] }
    }

    # --- startup & launch latency + CPU temperature (best-effort) ----------
    # The Diagnostics-Performance/Operational log needs admin or membership of
    # "Performance Log Users"; the ACPI thermal zone is exposed by only some
    # laptops. Every probe is guarded, so a missing value simply omits the key
    # and the Python collector carries the previous one forward.
    try {
        $bootEvt = Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 100
        } -MaxEvents 1 -ErrorAction Stop
        $bx = [xml]$bootEvt.ToXml()
        $bt = ($bx.Event.EventData.Data | Where-Object { $_.Name -eq 'BootTime' }).'#text'
        if ($bt) { $out.boot_time_s = [math]::Round([double]$bt / 1000, 1) }
    } catch { Write-Verbose "Optional probe failed: $($_.Exception.Message)" }

    $appStartSec = {
        param($rx)
        try {
            $evts = Get-WinEvent -FilterHashtable @{
                LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 101
                StartTime = $os.LastBootUpTime
            } -MaxEvents 80 -ErrorAction Stop
            foreach ($e in $evts) {
                $xx = [xml]$e.ToXml()
                $nm = ($xx.Event.EventData.Data | Where-Object { $_.Name -eq 'Name' }).'#text'
                if ($nm -and $nm -match $rx) {
                    $tt = ($xx.Event.EventData.Data | Where-Object { $_.Name -eq 'TotalTime' }).'#text'
                    if ($tt) { return [math]::Round([double]$tt / 1000, 1) }
                }
            }
        } catch { Write-Verbose "Optional probe failed: $($_.Exception.Message)" }
        return $null
    }
    $vx = & $appStartSec 'explorer\.exe'; if ($null -ne $vx) { $out.explorer_launch_s = $vx }
    $vx = & $appStartSec 'outlook\.exe';  if ($null -ne $vx) { $out.outlook_launch_s  = $vx }
    $vx = & $appStartSec 'winword\.exe';  if ($null -ne $vx) { $out.word_launch_s     = $vx }
    # LxssManager service startup is not WSL cold-start latency; do not report it as such.

    try {
        $tz = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature `
              -ErrorAction Stop | Select-Object -First 1
        if ($tz -and $tz.CurrentTemperature -gt 0) {
            $out.cpu_temp_c = [math]::Round(($tz.CurrentTemperature / 10) - 273.15, 1)
        }
    } catch { Write-Verbose "Optional probe failed: $($_.Exception.Message)" }
}

$out | ConvertTo-Json -Depth 4 -Compress
