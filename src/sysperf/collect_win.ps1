# Windows metrics collector for the System Performance Dashboard.
# Emits one compact JSON object on stdout. READ-ONLY: modifies no system state.
# -Full also gathers the slow metrics (service inventory); omit it for fast polling.
param([switch]$Full)
$ErrorActionPreference = 'SilentlyContinue'

$os   = Get-CimInstance Win32_OperatingSystem
$all  = Get-CimInstance Win32_Process
$proc = Get-Process
# Handle accounting. Total system-wide handles include a large, non-actionable
# floor (OS kernel, service hosts, session infrastructure, and the resident
# Defender/CrowdStrike/CyberArk/GlobalProtect/Intune security agents) that grows
# with uptime no matter what the user does. Scoring the raw total penalises the
# user for growth they cannot control and never reaches target. We therefore
# split the floor out and score only the ACTIONABLE application-handle footprint
# (mirrors how net_remote excludes non-actionable loopback IPC).
$sysProcNames = @(
  'System','Registry','Idle','smss','csrss','wininit','winlogon','services','lsass',
  'svchost','fontdrvhost','dwm','LogonUI','WUDFHost','spoolsv','sihost','taskhostw',
  'ctfmon','ShellExperienceHost','StartMenuExperienceHost','SearchHost','SearchIndexer',
  'SearchProtocolHost','SearchFilterHost','RuntimeBroker','dllhost','WmiPrvSE','conhost',
  'dashost','SecurityHealthService','SecurityHealthSystray','audiodg','wlanext',
  'MsMpEng','MsSense','NisSrv','CSFalconService','CSFalconContainer','cb','RepMgr','RepUx',
  'CyberArkEPMService','vf_agent','vfagent','PanGPS','PanGPA','Sense','MpDefenderCoreService',
  'dvcTRUSTagent','deviceTRUST','IntuneManagementExtension'
)
$hTotal  = ($proc | Measure-Object Handles -Sum).Sum
$hSystem = ($proc | Where-Object { $sysProcNames -contains $_.Name } | Measure-Object Handles -Sum).Sum
$hApp    = $hTotal - $hSystem

# Commit-charge accounting. The raw "\Memory\Committed Bytes" counter (committed_mb)
# is ~58% non-actionable: OS/service/security-agent private commit PLUS a large
# kernel + pagefile-backed shared commit gap the user cannot influence. WSL guest
# commit is already scored separately (vmmem_mb). We therefore also compute the
# ACTIONABLE application commit (sum of app-process private bytes, excluding the
# system/security floor and WSL/compression) and score that instead of the raw
# counter (mirrors the handles and net_remote fixes). committed_mb is retained for
# the true "commit charge" hero display and chart.
$commitFloorNames = $sysProcNames + @('vmmemWSL','vmmem','Memory Compression')
$committedAppMB    = [math]::Round((($proc | Where-Object { $commitFloorNames -notcontains $_.Name } | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB)
$committedSystemMB = [math]::Round((($proc | Where-Object { $commitFloorNames -contains $_.Name } | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB)

# --- lock state ---
# LogonUI hosts the sign-in screen. LockApp (the lock curtain) stays resident
# with every thread SUSPENDED after an unlock on Windows 11, so its mere
# presence reported "locked" for hours of active use; only a LockApp with
# running threads counts.
$sessionLocked = [bool](@($proc | Where-Object ProcessName -eq 'LogonUI').Count)
if (-not $sessionLocked) {
    foreach ($la in @($proc | Where-Object ProcessName -eq 'LockApp')) {
        if (@($la.Threads | Where-Object { -not ($_.ThreadState -eq 'Wait' -and $_.WaitReason -eq 'Suspended') }).Count) {
            $sessionLocked = $true
        }
    }
}

# --- one batched Get-Counter call instead of six (major cost saving) ---
$paths = @(
  '\Memory\Committed Bytes'
  '\Memory\Commit Limit'
  '\Memory\Pool Paged Bytes'
  '\Memory\Pool Nonpaged Bytes'
  '\System\Processor Queue Length'
  '\Processor(_Total)\% Processor Time'
  '\PhysicalDisk(_Total)\% Idle Time'
)
$cv = @{}
foreach ($s in (Get-Counter -Counter $paths -MaxSamples 1).CounterSamples) {
    $cv[($s.Path -replace '^\\\\[^\\]+', '')] = $s.CookedValue
}
function CV([string]$k, [double]$div = 1MB, [int]$dp = 0) {
    $m = $cv.Keys | Where-Object { $_ -like "*$k*" } | Select-Object -First 1
    if ($m) { [math]::Round($cv[$m] / $div, $dp) } else { $null }
}

# CPU averaged over the whole interval since the previous probe. The cooked
# '% Processor Time' above covers only ~1 s - the second in which THIS probe's
# own PowerShell is starting up - so it reported the dashboard's own cost
# (20-50%) on a machine that was ~5% busy. The cumulative idle counter divided
# over the full interval dilutes the probe's start-up to well under 1%.
$cpuInterval = $null
try {
    $raw = Get-CimInstance Win32_PerfRawData_PerfOS_Processor -Filter "Name='_Total'" -ErrorAction Stop
    $cpuState = Join-Path ([IO.Path]::GetTempPath()) 'sysperf_cpu_raw.json'
    $cpuNow = @{ n = [double]$raw.PercentProcessorTime; d = [double]$raw.Timestamp_Sys100NS }
    if (Test-Path $cpuState) {
        $cpuPrev = Get-Content $cpuState -Raw | ConvertFrom-Json
        $dd = $cpuNow.d - [double]$cpuPrev.d
        if ($dd -gt 1e8 -and $dd -lt 9e9) {       # 10 s .. 15 min apart
            $busy = (1 - ($cpuNow.n - [double]$cpuPrev.n) / $dd) * 100
            $cpuInterval = [math]::Round([math]::Max(0, [math]::Min(100, $busy)), 1)
        }
    }
    $cpuNow | ConvertTo-Json -Compress | Set-Content -LiteralPath $cpuState
} catch { $null = $_ }

# --- WebView2 host attribution via ancestor walk ---
$idx = @{}
foreach ($p in $all) { $idx[[int]$p.ProcessId] = $p }
$edge = @($all | Where-Object { $_.Name -eq 'msedge.exe' })
$wv2  = @($all | Where-Object { $_.Name -eq 'msedgewebview2.exe' })

$hosts = @{}
foreach ($w in $wv2) {
    $c = $idx[[int]$w.ProcessId]; $g = 0
    while ($c -and $c.Name -eq 'msedgewebview2.exe' -and $g -lt 12) {
        $c = $idx[[int]$c.ParentProcessId]; $g++
    }
    $h = if ($c) { $c.Name -replace '\.exe$','' } else { 'orphan' }
    if ($hosts.ContainsKey($h)) { $hosts[$h] += 1 } else { $hosts[$h] = 1 }
}

# --- Edge composition + extension footprint ---
$eMain=0; $eRend=0; $eExt=0; $eUtil=0; $extWS=0
$wsById = @{}
foreach ($q in $proc) { $wsById[[int]$q.Id] = $q.WS }
foreach ($e in $edge) {
    $cl = $e.CommandLine
    if     ($cl -match '--extension-process') { $eExt++; $extWS += [double]$wsById[[int]$e.ProcessId] }
    elseif ($cl -match '--type=renderer')     { $eRend++ }
    elseif ($cl -match '--type=')             { $eUtil++ }
    else                                      { $eMain++ }
}

$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$comp = Get-Process 'Memory Compression'
$vmm  = Get-Process vmmemWSL

# --- per-process CPU (cumulative processor-seconds) ---------------------------
# .CPU is total processor-seconds since the process started; the dashboard
# differences it across samples (by pid) to attribute CPU% and find the apps
# that use CPU consistently. Reuses the $proc snapshot already taken above, so
# this adds no extra Win32/counter query. READ-ONLY. Top 50 by cumulative CPU
# keeps the payload small while retaining every genuine consumer (idle-but-old
# processes have high cumulative CPU but a zero per-interval delta, so they are
# filtered out downstream).
$ncpu = [Environment]::ProcessorCount
# Executable path by PID from Win32_Process (WMI ExecutablePath is readable for
# user processes where Get-Process .Path is blocked cross-session). Used so the
# App Control tab can relaunch a stopped process from its real image path.
$exeById = @{}
foreach ($w in $all) { if ($w.ExecutablePath) { $exeById[[int]$w.ProcessId] = [string]$w.ExecutablePath } }
$cpuProcs = @(
    $proc | Where-Object { $_.CPU -ne $null } |
        Sort-Object CPU -Descending | Select-Object -First 50 |
        ForEach-Object {
            [ordered]@{
                n     = [string]$_.ProcessName
                pid   = [int]$_.Id
                cpu_s = [math]::Round([double]$_.CPU, 1)
                ws_mb = [math]::Round($_.WS / 1MB, 0)
                exe   = $exeById[[int]$_.Id]
            }
        }
)

# Instantaneous per-process CPU% from the perf counter, aggregated by base
# process name. cpu_procs above is CUMULATIVE processor-seconds, which the
# dashboard diffs across samples by PID — that structurally MISSES short-lived
# processes (each pwsh probe, SearchProtocolHost) and never attributes system
# processes, so a machine pegged by those looked "idle per-process". This is a
# single counter read of every live process's current CPU, summed across
# instances that share a name, expressed as a share of the WHOLE machine. It is
# how the dashboard surfaces the true top consumers (pwsh, sdxhelper, fsdaemon,
# search, security agents) instead of being blind to them.
$cpuTop = @()
try {
    $cs = (Get-Counter '\Process(*)\% Processor Time' -MaxSamples 1 -ErrorAction Stop).CounterSamples
    $agg = @{}
    foreach ($c in $cs) {
        $inst = [string]$c.InstanceName
        if ($inst -eq '_total' -or $inst -eq 'idle') { continue }
        $base = ($inst -replace '#\d+$','')
        $agg[$base] = [double]($agg[$base]) + [double]$c.CookedValue
    }
    $cpuTop = @(
        $agg.GetEnumerator() | Where-Object { $_.Value -ge 5 } |
            Sort-Object Value -Descending | Select-Object -First 12 |
            ForEach-Object {
                [ordered]@{
                    n       = [string]$_.Key
                    cpu_pct = [math]::Round($_.Value / $ncpu, 1)   # share of all cores
                    of1core = [math]::Round($_.Value, 1)           # share of one core
                }
            }
    )
} catch { $cpuTop = @() }

# Last COMPLETED OneDrive sync (epoch seconds, UTC). OneDrive records the
# completion time of its core sync operations per account in the registry:
#   LastDAUEventTimestamp_FindChanges  - a change-detection scan completed
#   LastDAUEventTimestamp_Upload / _Download - a transfer completed
# The most recent of these across all signed-in accounts is the authoritative
# "last synced" time: unlike a settings-file write time it reflects a *completed*
# sync cycle, not incidental disk activity. Best-effort: $null if unavailable.
$odLastSync = $null
try {
    $acctRoot = 'HKCU:\Software\Microsoft\OneDrive\Accounts'
    if (Test-Path $acctRoot) {
        $stamps = foreach ($a in Get-ChildItem $acctRoot -ErrorAction SilentlyContinue) {
            $pv = Get-ItemProperty $a.PSPath -ErrorAction SilentlyContinue
            foreach ($k in 'LastDAUEventTimestamp_FindChanges',
                            'LastDAUEventTimestamp_Upload',
                            'LastDAUEventTimestamp_Download') {
                $val = $pv.$k
                if ($val -and ("$val" -match '^\d+$')) { [int64]$val }
            }
        }
        if ($stamps) { $odLastSync = ($stamps | Measure-Object -Maximum).Maximum }
    }
} catch { $odLastSync = $null }

$out = [ordered]@{
    ts               = (Get-Date).ToString('o')
    boot             = if ($os -and $os.LastBootUpTime) { $os.LastBootUpTime.ToString('o') } else { $null }
    uptime_h         = if ($os -and $os.LastBootUpTime) { [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 2) } else { $null }
    committed_mb     = CV 'Committed Bytes'
    committed_app_mb    = $committedAppMB
    committed_system_mb = $committedSystemMB
    commit_limit_mb  = CV 'Commit Limit'
    available_mb     = if ($os) { [math]::Round($os.FreePhysicalMemory / 1KB, 0) } else { $null }
    total_ram_mb     = if ($os) { [math]::Round($os.TotalVisibleMemorySize / 1KB, 0) } else { $null }
    compression_mb   = if ($comp) { [math]::Round($comp.WS / 1MB, 0) } else { 0 }
    vmmem_mb         = if ($vmm)  { [math]::Round($vmm.WS  / 1MB, 0) } else { 0 }
    pool_paged_mb    = CV 'Pool Paged'
    pool_nonpaged_mb = CV 'Pool Nonpaged'
    handles          = $hApp
    handles_total    = $hTotal
    handles_system   = $hSystem
    processes        = $os.NumberOfProcesses
    edge_procs       = $edge.Count
    wv2_procs        = $wv2.Count
    browser_procs    = $edge.Count + $wv2.Count
    edge_main        = $eMain
    edge_renderer    = $eRend
    edge_extension   = $eExt
    edge_utility     = $eUtil
    edge_ext_mb      = [math]::Round($extWS / 1MB, 0)
    wv2_hosts        = $hosts
    svchost_procs    = @($proc | Where-Object ProcessName -eq 'svchost').Count
    search_procs     = @($proc | Where-Object { $_.ProcessName -in 'SearchIndexer','SearchFilterHost','SearchProtocolHost' }).Count
    onedrive_running = [bool](@($proc | Where-Object ProcessName -eq 'OneDrive').Count)
    onedrive_last_sync = $odLastSync
    session_locked   = $sessionLocked
    c_free_gb        = if ($disk) { [math]::Round($disk.FreeSpace / 1GB, 1) } else { $null }
    c_size_gb        = if ($disk) { [math]::Round($disk.Size / 1GB, 1) } else { $null }
    cpu_queue        = [math]::Round((CV 'Processor Queue Length' 1 0), 0)
    cpu_pct          = if ($null -ne $cpuInterval) { $cpuInterval } else { CV '% Processor Time' 1 1 }
    cpu_pct_instant  = CV '% Processor Time' 1 1
    # % Disk Time can exceed 100 with queued I/O (217% was reported); busy time
    # derived from % Idle Time is bounded 0-100.
    disk_active_pct  = $(if ($null -ne (CV '% Idle Time' 1 1)) { [math]::Round([math]::Max(0, 100 - (CV '% Idle Time' 1 1)), 1) } else { $null })
    ncpu             = $ncpu
    cpu_procs        = $cpuProcs
    cpu_top          = $cpuTop
}

# --- Wi-Fi connection (fast path) ---
# Primary source is Get-NetAdapter + Get-NetConnectionProfile, which work WITHOUT
# elevation or the Location privacy permission and yield the associated SSID,
# negotiated link speed, adapter/radio model and network category. We then TRY
# netsh wlan for the richer signal% / band / channel / Rx-Tx detail; on Win 11
# that call is gated behind Location services and can fail (error 5) - in that
# case we still report the adapter-level data and flag detail_limited so the UI
# can explain what to enable. READ-ONLY: only queries, changes no state.
$wifi = [ordered]@{ present = $false }
try {
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
        # Infer radio generation from the adapter model if netsh detail is blocked.
        if     ($wnic.InterfaceDescription -match 'Wi-Fi 7|BE2\d\d')  { $wifi.radio = '802.11be (Wi-Fi 7)' }
        elseif ($wnic.InterfaceDescription -match 'Wi-Fi 6E')         { $wifi.radio = '802.11ax (Wi-Fi 6E)' }
        elseif ($wnic.InterfaceDescription -match 'Wi-Fi 6|AX2\d\d')  { $wifi.radio = '802.11ax (Wi-Fi 6)' }
        elseif ($wnic.InterfaceDescription -match 'AC 9\d\d\d|Wi-Fi 5'){ $wifi.radio = '802.11ac (Wi-Fi 5)' }
        $prof = Get-NetConnectionProfile -ErrorAction SilentlyContinue |
                Where-Object InterfaceAlias -eq $wnic.Name | Select-Object -First 1
        if ($prof) {
            $wifi.ssid     = [string]$prof.Name
            $wifi.category = [string]$prof.NetworkCategory
            $wifi.ipv4     = [string]$prof.IPv4Connectivity
        }
        # Enrich with signal / band / channel / Rx-Tx when netsh is permitted.
        $wifi.detail_limited = $true
        $wl = (& "$env:SystemRoot\System32\netsh.exe" wlan show interfaces 2>$null) -join "`n"
        if ($wl -and $wl -match '(?im)^\s*Signal\s*:') {
            function WLo([string]$label) {
                $m = [regex]::Match($wl, "(?im)^\s*$label\s*:\s*(.+?)\s*$")
                if ($m.Success) { $m.Groups[1].Value.Trim() } else { $null }
            }
            $wifi.detail_limited = $false
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
} catch { $null = $_ }
$out.wifi = $wifi
# Flat key for the signal-strength trend series (nested objects are not charted).
if ($wifi.present -and $null -ne $wifi.signal) { $out.wifi_signal = $wifi.signal }
# Negotiated link rates per sample, so the Wi-Fi chart shows speed over time (5.4.7).
if ($wifi.present -and $null -ne $wifi.rx_mbps) { $out.wifi_rx_mbps = $wifi.rx_mbps }
if ($wifi.present -and $null -ne $wifi.tx_mbps) { $out.wifi_tx_mbps = $wifi.tx_mbps }

if ($Full) {
    # PowerShell 7: the network block must WAIT 8 s (two TCP snapshots, to detect
    # connections SUSTAINED beyond a burst). Run just that block as a background
    # thread job (ThreadJob, bundled with PS7) so its unavoidable wait OVERLAPS the
    # rest of the sweep, then merge its result at the end. Everything else runs
    # sequentially on the warm main runspace - cheaper and more predictable than a
    # runspace per block, which cold-imports its modules every sweep. READ-ONLY.
    $netBlock = {
        $ErrorActionPreference = 'SilentlyContinue'; $r = [ordered]@{}
        $remoteFilter = { $_.RemoteAddress -notin '127.0.0.1','::1','0.0.0.0','::' -and
                          $_.RemoteAddress -notlike '169.254.*' -and $_.RemoteAddress -notlike 'fe80:*' }
        $tcp1 = Get-NetTCPConnection
        $rem1 = @($tcp1 | Where-Object State -eq 'Established' | Where-Object $remoteFilter)
        $key1 = @{}; foreach ($c in $rem1) { $key1["$($c.LocalAddress):$($c.LocalPort)-$($c.RemoteAddress):$($c.RemotePort)"] = $true }
        Start-Sleep -Seconds 8
        $tcp2 = Get-NetTCPConnection
        $est  = @($tcp2 | Where-Object State -eq 'Established')
        $rem2 = @($est | Where-Object $remoteFilter)
        $sustained = @($rem2 | Where-Object { $key1.ContainsKey("$($_.LocalAddress):$($_.LocalPort)-$($_.RemoteAddress):$($_.RemotePort)") })
        # Exclude OS/sync infrastructure (OneDrive sync, Windows Update/Delivery
        # Optimization in svchost, Defender, VPN) - non-actionable and bursty after
        # every reboot; mirrors the OS-floor exclusion used for commit and handles.
        $infraNames = 'OneDrive','OneDrive.Sync.Service','FileCoAuth','svchost','System','lsass',
                      'services','spoolsv','wlanext','MsMpEng','NisSrv','MicrosoftSecurityApp',
                      'SecurityHealthService','BackgroundTransferHost','expressvpn-service','expressvpnd'
        $pn = @{}; foreach ($p in Get-Process) { $pn[$p.Id] = $p.ProcessName }
        $infra = @($sustained | Where-Object { $pn[[int]$_.OwningProcess] -in $infraNames })
        $sustained = @($sustained | Where-Object { $pn[[int]$_.OwningProcess] -notin $infraNames })
        $r.net_remote_infra = $infra.Count
        $r.net_remote       = $sustained.Count
        $r.net_remote_raw   = $rem2.Count
        $r.net_remote_hosts = @($sustained | Select-Object -ExpandProperty RemoteAddress -Unique).Count
        $r.net_loopback     = @($est | Where-Object { $_.RemoteAddress -in '127.0.0.1','::1' }).Count
        $r.net_established  = $est.Count
        $r.net_listen       = @($tcp2 | Where-Object State -eq 'Listen').Count
        $r.net_tcp_total    = @($tcp2).Count
        $r.net_adapters = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | ForEach-Object {
            $ls = [string]$_.LinkSpeed; $num = 0.0
            if ($ls -match '([\d.]+)\s*(\w+)') { $num = [double]$Matches[1]
                if ($Matches[2] -match 'Gbps') { $num *= 1000 } elseif ($Matches[2] -match 'Kbps') { $num /= 1000 } }
            @{ name=[string]$_.Name; desc=[string]$_.InterfaceDescription; link_mbps=[math]::Round($num,0); media=[string]$_.MediaType; status=[string]$_.Status } })
        $prof = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($prof) { $r.net_category=[string]$prof.NetworkCategory; $r.net_ipv4_conn=[string]$prof.IPv4Connectivity; $r.net_ipv6_conn=[string]$prof.IPv6Connectivity }
        $r
    }
    # Windows PowerShell 5.1 (every Windows PC; the default for Store installs)
    # has no Start-ThreadJob: run the block inline there, paying its 8 s wait,
    # rather than losing every network metric.
    $netJob = $null; $netFrag = $null
    if (Get-Command Start-ThreadJob -ErrorAction SilentlyContinue) { $netJob = Start-ThreadJob -ScriptBlock $netBlock }
    else { $netFrag = & $netBlock }

    $svc = Get-Service
    $out.services_running = @($svc | Where-Object Status -eq 'Running').Count
    $out.services_total   = $svc.Count

    # --- startup optimization: count ENABLED auto-start entries only ---
    # Win32_StartupCommand lists every registered entry regardless of its
    # Task Manager enable/disable state. StartupApproved marks a disabled entry
    # with a leading 0x03 byte, so we exclude those: the metric then reflects
    # what actually launches at boot and rewards disabling (reversible) rather
    # than requiring registry deletion.
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
            [void]$disabled.Add(($_.Name -replace '\.lnk$','').Trim().ToLower())
          }
        }
      }
    }
    $sc = @(Get-CimInstance Win32_StartupCommand)
    $enabled = @($sc | Where-Object {
      -not $disabled.Contains(($_.Name -replace '\.lnk$','').Trim().ToLower())
    })
    $out.startup_apps       = $enabled.Count
    $out.startup_apps_total = $sc.Count

    # --- component versions (upgrade advisory) ---
    # GPU + network driver versions/dates and the Windows build. Cheap CIM/NetAdapter
    # queries; the Linux/WSL-side versions are gathered by components.py. READ-ONLY.
    $gpu = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
           Where-Object { $_.DriverVersion } | Select-Object -First 1
    if ($gpu) {
        $out.gpu_name        = [string]$gpu.Name
        $out.gpu_driver      = [string]$gpu.DriverVersion
        $out.gpu_driver_date = if ($gpu.DriverDate) { ([datetime]$gpu.DriverDate).ToString('yyyy-MM-dd') } else { '' }
    }
    $wad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
           Where-Object { $_.Status -eq 'Up' -and $_.PhysicalMediaType -match '802\.11' } | Select-Object -First 1
    if (-not $wad) {
        $wad = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
               Where-Object Status -eq 'Up' | Select-Object -First 1
    }
    if ($wad) {
        $out.net_driver_name     = [string]$wad.InterfaceDescription
        $out.net_driver_ver      = [string]$wad.DriverVersion
        $out.net_driver_date     = if ($wad.DriverDate) { ([datetime]$wad.DriverDate).ToString('yyyy-MM-dd') } else { '' }
        $out.net_driver_provider = [string]$wad.DriverProvider
    }
    $out.win_build = [string]$os.Version
    $out.ps_version = [string]$PSVersionTable.PSVersion.ToString()
    # Detect PowerShell 7 (pwsh) if installed - machine, user-scope or portable.
    $pwshExe = @("$env:ProgramFiles\PowerShell\7\pwsh.exe",
                 "$env:LOCALAPPDATA\Programs\PowerShell\7\pwsh.exe") |
               Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $pwshExe) { $pc = Get-Command pwsh -ErrorAction SilentlyContinue; if ($pc) { $pwshExe = $pc.Source } }
    if ($pwshExe) {
        try { $out.pwsh_version = [string](& $pwshExe -NoProfile -Command '$PSVersionTable.PSVersion.ToString()') } catch { $null = $_ }
    }
    try {
        $edgeExe = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        if (Test-Path $edgeExe) { $out.edge_version = [string](Get-Item $edgeExe).VersionInfo.ProductVersion }
    } catch { $null = $_ }
    $wv = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue
    if ($wv -and $wv.pv) { $out.webview2_version = [string]$wv.pv }
    $bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue
    if ($bios) {
        $out.bios_version = [string]$bios.SMBIOSBIOSVersion
        $out.bios_date    = if ($bios.ReleaseDate) { ([datetime]$bios.ReleaseDate).ToString('yyyy-MM-dd') } else { '' }
    }

    # --- Wi-Fi neighbourhood scan (band / channel overlap analysis) ---
    # `netsh wlan show networks mode=bssid` lists visible APs with band, channel
    # and signal - the input for channel-overlap analysis. On Win 11 it needs the
    # Location permission; when blocked we flag the reason so the UI can explain
    # how to enable it. READ-ONLY: a passive scan that changes nothing.
    $scan = [ordered]@{ available = $false; reason = 'unknown'; nets = @() }
    try {
        $raw = (& "$env:SystemRoot\System32\netsh.exe" wlan show networks mode=bssid 2>$null) -join "`n"
        if ($raw -match '(?i)location permission|requires elevation') {
            $scan.reason = 'location'
        } elseif ($raw -match '(?im)^\s*BSSID \d+') {
            $scan.available = $true; $scan.reason = 'ok'
            $nets = @()
            foreach ($sblk in ($raw -split '(?im)^SSID \d+ : ')) {
                if ($sblk -notmatch 'BSSID') { continue }
                $ssid = (($sblk -split "`n")[0]).Trim()
                foreach ($bblk in ($sblk -split '(?im)^\s*BSSID \d+\s*:')) {
                    if ($bblk -notmatch '(?i)Signal') { continue }
                    $sig = [regex]::Match($bblk, '(?im)^\s*Signal\s*:\s*(\d+)').Groups[1].Value
                    $ch  = [regex]::Match($bblk, '(?im)^\s*Channel\s*:\s*(\d+)').Groups[1].Value
                    $bnd = [regex]::Match($bblk, '(?im)^\s*Band\s*:\s*(.+?)\s*$').Groups[1].Value
                    $rad = [regex]::Match($bblk, '(?im)^\s*Radio type\s*:\s*(.+?)\s*$').Groups[1].Value
                    $mac = [regex]::Match($bblk, '^\s*([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})').Groups[1].Value
                    if ($ch) {
                        $nets += @{ ssid = $ssid; signal = [int]$sig; channel = [int]$ch;
                                    band = $bnd.Trim(); radio = $rad.Trim(); bssid = $mac.ToLower() }
                    }
                }
            }
            $scan.nets = $nets
        } else {
            $scan.reason = if ($raw) { 'no-data' } else { 'unavailable' }
        }
    } catch { $scan.reason = 'error' }
    $out.wifi_scan = $scan

    # --- hardware health ---
    # Auto-detects Device-Manager-reported faults (yellow-bang error codes),
    # storage SMART health and battery state. NOTE: a functional glitch (e.g. a
    # touchpad button that misbehaves) is NOT flagged here if Windows still
    # reports the device as OK - use hw_watchlist.json to track those manually.
    $prob = @(Get-CimInstance Win32_PnPEntity |
              Where-Object { $_.ConfigManagerErrorCode -and $_.ConfigManagerErrorCode -ne 0 })
    $out.hw_problem_devices = $prob.Count
    $out.hw_problem_list = @($prob | Select-Object -First 12 | ForEach-Object {
        @{ name = $_.Name; code = [int]$_.ConfigManagerErrorCode; status = [string]$_.Status } })

    $disks = @(Get-PhysicalDisk)
    $out.hw_disks = @($disks | ForEach-Object {
        @{ name = [string]$_.FriendlyName; media = [string]$_.MediaType;
           health = [string]$_.HealthStatus; op = [string]$_.OperationalStatus } })
    $out.hw_disks_unhealthy = @($disks | Where-Object { $_.HealthStatus -ne 'Healthy' }).Count

    $bat = Get-CimInstance Win32_Battery | Select-Object -First 1
    if ($bat) { $out.hw_battery = @{ charge = [int]$bat.EstimatedChargeRemaining;
                                     status = [int]$bat.BatteryStatus } }

    # pointing devices + touchpad candidates (Synaptics / touch pad by name)
    $out.hw_pointing = @(Get-CimInstance Win32_PointingDevice | ForEach-Object {
        @{ name = [string]$_.Name; status = [string]$_.Status; code = [int]$_.ConfigManagerErrorCode } })
    $out.hw_touchpad = @(Get-CimInstance Win32_PnPEntity |
        Where-Object { $_.Name -match 'touch ?pad|Synaptics' } | ForEach-Object {
        @{ name = [string]$_.Name; status = [string]$_.Status; code = [int]$_.ConfigManagerErrorCode } })

    # --- driver health ---
    # Third-party (OEM) driver inventory via pnputil — fast (~4s) and needs no
    # per-driver signature verification, unlike Win32_PnPSignedDriver which is so
    # slow it times the whole -Full sample out (>60s). Driver-specific
    # Device-Manager faults come from Win32_PnPEntity error codes below.
    # READ-ONLY: /enum-drivers only lists drivers, it changes nothing.
    $drvList = @()
    try {
        $raw = (& "$env:SystemRoot\System32\pnputil.exe" /enum-drivers 2>$null) -join "`n"
        foreach ($blk in ($raw -split "\r?\n\s*\r?\n")) {
            if ($blk -notmatch 'Published Name') { continue }
            $pub  = if ($blk -match 'Published Name:\s*(.+)') { $Matches[1].Trim() } else { '' }
            $orig = if ($blk -match 'Original Name:\s*(.+)')  { $Matches[1].Trim() } else { '' }
            $prov = if ($blk -match 'Provider Name:\s*(.+)')  { $Matches[1].Trim() } else { '' }
            $cls  = if ($blk -match 'Class Name:\s*(.+)')     { $Matches[1].Trim() } else { '' }
            $sign = if ($blk -match 'Signer Name:\s*(.+)')    { $Matches[1].Trim() } else { '' }
            $ver = ''; $dt = $null
            if ($blk -match 'Driver Version:\s*(\S+)\s+(\S+)') {
                $dtStr = $Matches[1]; $ver = $Matches[2]
                foreach ($f in 'MM/dd/yyyy', 'M/d/yyyy', 'dd/MM/yyyy', 'd/M/yyyy', 'yyyy-MM-dd') {
                    try { $dt = [datetime]::ParseExact($dtStr, $f, [Globalization.CultureInfo]::InvariantCulture); break } catch { $null = $_ }
                }
                if (-not $dt) { try { $dt = [datetime]$dtStr } catch { $null = $_ } }
            }
            $drvList += [pscustomobject]@{ pub = $pub; orig = $orig; provider = $prov;
                                           class = $cls; signer = $sign; version = $ver; date = $dt }
        }
    } catch { $null = $_ }
    $out.drv_total    = $drvList.Count
    $out.drv_unsigned = @($drvList | Where-Object { -not $_.signer }).Count

    $staleCut = (Get-Date).AddYears(-4)
    # Ignore synthetic placeholder dates (e.g. Intel ships many INFs dated 1968)
    # and Microsoft inbox drivers (fixed 2006 baseline INF date, but serviced via
    # Windows Update). Also drop "optional" packages that are not real device
    # function drivers — vendor software/control-panel components, driver
    # extensions, printers and firmware-update helpers — so the list shows only
    # important stale drivers a user might need to update.
    $dateFloor = [datetime]'2000-01-01'
    $optionalClasses = @('SoftwareComponent', 'Extension', 'Printer', 'PrintQueue', 'Firmware')
    $staleAll = @($drvList | Where-Object {
        $_.date -and $_.date -ge $dateFloor -and $_.date -lt $staleCut -and
        $_.provider -notmatch 'Microsoft' -and $optionalClasses -notcontains $_.class })
    # De-duplicate multiple installed copies of the same INF (keep the newest),
    # so e.g. seven ibtusb.inf entries count as one stale driver.
    $stale = @($staleAll |
        Group-Object { if ($_.orig) { $_.orig } else { $_.pub } } |
        ForEach-Object { $_.Group | Sort-Object date -Descending | Select-Object -First 1 })
    $out.drv_stale = $stale.Count
    $out.drv_stale_list = @($stale | Sort-Object date | Select-Object -First 40 | ForEach-Object {
        @{ name     = $(if ($_.orig) { [string]$_.orig } else { [string]$_.pub })
           version  = [string]$_.version
           date     = $(if ($_.date) { $_.date.ToString('yyyy-MM-dd') } else { '' })
           class    = [string]$_.class
           provider = [string]$_.provider } })

    # Driver-specific Device-Manager error codes: missing/corrupt/failed/unsigned
    # (1,3,10,18,19,28,31,32,37,39,48,52) — distinct from generic device faults.
    $drvCodes = @(1, 3, 10, 18, 19, 28, 31, 32, 37, 39, 48, 52)
    $drvProb = @(Get-CimInstance Win32_PnPEntity |
        Where-Object { $_.ConfigManagerErrorCode -and $drvCodes -contains [int]$_.ConfigManagerErrorCode })
    $out.drv_problem = $drvProb.Count
    $out.drv_problem_list = @($drvProb | Select-Object -First 12 | ForEach-Object {
        @{ name = [string]$_.Name; code = [int]$_.ConfigManagerErrorCode; status = [string]$_.Status } })

    # --- startup & launch latency + thermal + battery wear (best-effort) ------
    # Sourced from the Microsoft-Windows-Diagnostics-Performance/Operational log
    # (readable by admins / the "Performance Log Users" group) and root/wmi.
    # Every probe is individually guarded; on failure the key is simply absent
    # and the Python collector carries the previous value (or the row is omitted).
    # READ-ONLY: only reads event logs and WMI, changes nothing.
    try {
        $bootEvt = Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 100
        } -MaxEvents 1 -ErrorAction Stop
        $bx = [xml]$bootEvt.ToXml()
        $bt = ($bx.Event.EventData.Data | Where-Object { $_.Name -eq 'BootTime' }).'#text'
        if ($bt) { $out.boot_time_s = [math]::Round([double]$bt / 1000, 1) }
    } catch { $null = $_ }
    # Fallback when the Diagnostics-Performance log is not readable (it needs
    # admin or "Performance Log Users"; an unprivileged service sees no events):
    # kernel start (Kernel-General 12) to the first user sign-in after it
    # (Winlogon 7001), both in the System log that every user can read.
    if ($null -eq $out.boot_time_s) {
        try {
            $k0 = Get-WinEvent -FilterHashtable @{ LogName = 'System';
                ProviderName = 'Microsoft-Windows-Kernel-General'; Id = 12 } -MaxEvents 1 -ErrorAction Stop
            $lg = Get-WinEvent -FilterHashtable @{ LogName = 'System';
                ProviderName = 'Microsoft-Windows-Winlogon'; Id = 7001; StartTime = $k0.TimeCreated } `
                -ErrorAction Stop | Sort-Object TimeCreated | Select-Object -First 1
            $sec = ($lg.TimeCreated - $k0.TimeCreated).TotalSeconds
            $src = 'system-log'
            # Sign-in long after boot means the PC sat at the lock screen (observed
            # 2026-10-24: 842 s "boot" that settled in ~30 s). Use the end of the
            # boot event burst instead: the last System event before the first
            # quiet gap of 60 s, so waiting time is not scored as boot time.
            if ($sec -gt 120) {
                $prev = $k0.TimeCreated
                foreach ($e in (Get-WinEvent -FilterHashtable @{ LogName = 'System';
                        StartTime = $k0.TimeCreated; EndTime = $lg.TimeCreated } -ErrorAction Stop |
                        Sort-Object TimeCreated)) {
                    if (($e.TimeCreated - $prev).TotalSeconds -ge 60) { break }
                    $prev = $e.TimeCreated
                }
                $sec = ($prev - $k0.TimeCreated).TotalSeconds
                $src = 'system-log-settled'
            }
            if ($sec -gt 0 -and $sec -lt 900) {
                $out.boot_time_s = [math]::Round($sec, 1)
                $out.boot_time_source = $src
            }
        } catch { $null = $_ }
    }

    function AppStartSec([string]$match) {
        try {
            $evts = Get-WinEvent -FilterHashtable @{
                LogName = 'Microsoft-Windows-Diagnostics-Performance/Operational'; Id = 101, 103
            } -MaxEvents 80 -ErrorAction Stop
            foreach ($e in $evts) {
                $xx = [xml]$e.ToXml()
                $nm = ($xx.Event.EventData.Data | Where-Object { $_.Name -eq 'Name' }).'#text'
                if ($nm -and $nm -match $match) {
                    $tt = ($xx.Event.EventData.Data | Where-Object { $_.Name -eq 'TotalTime' }).'#text'
                    if ($tt) { return [math]::Round([double]$tt / 1000, 1) }
                }
            }
        } catch { $null = $_ }
        return $null
    }
    $expS = AppStartSec 'explorer\.exe'
    if ($null -ne $expS) { $out.explorer_launch_s = $expS }
    $olkS = AppStartSec 'outlook\.exe'
    if ($null -ne $olkS) { $out.outlook_launch_s = $olkS }
    $wrdS = AppStartSec 'winword\.exe'
    if ($null -ne $wrdS) { $out.word_launch_s = $wrdS }
    $wslS = AppStartSec 'wsl|lxss|LxssManager'
    if ($null -ne $wslS) { $out.wsl_start_s = $wslS }

    try {
        $tz = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature `
              -ErrorAction Stop | Select-Object -First 1
        if ($tz -and $tz.CurrentTemperature) {
            $out.cpu_temp_c = [math]::Round(($tz.CurrentTemperature / 10) - 273.15, 1)
        }
    } catch { $null = $_ }

    try {
        $bStat = Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData `
                 -ErrorAction Stop | Select-Object -First 1
        $bFull = Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity `
                 -ErrorAction Stop | Select-Object -First 1
        if ($bStat -and $bFull -and $bStat.DesignedCapacity -gt 0) {
            $wear = (1 - ($bFull.FullChargedCapacity / $bStat.DesignedCapacity)) * 100
            $out.battery_wear_pct = [math]::Round([math]::Max(0, $wear), 1)
        }
    } catch { $null = $_ }

    # Merge the overlapped network fragment (its 8 s wait ran alongside the work
    # above). If the job failed/timed out its keys are absent and the Python
    # collector carries the previous values forward. -Wait bounds the sweep.
    $frag = if ($netJob) { Receive-Job $netJob -Wait -AutoRemoveJob -ErrorAction SilentlyContinue } else { $netFrag }
    if ($frag -is [System.Collections.IDictionary]) { foreach ($k in $frag.Keys) { $out[$k] = $frag[$k] } }
}

# Launch & boot times: merge the elevated standby broker's published file. Kept
# OUTSIDE the best-effort inventory block above (which can throw early in a
# restricted/unprivileged environment and skip everything after it), so these
# values always reach the dashboard whenever the broker is running.
try {
    $cands = @((Join-Path $env:ProgramData 'SysPerf\standby\launch_times.json'),
               (Join-Path $env:ProgramData 'SysPerf\launch_times.json'))
    foreach ($lt in $cands) {
        if (Test-Path $lt) {
            $j = Get-Content -LiteralPath $lt -Raw | ConvertFrom-Json
            foreach ($k in 'boot_time_s','wsl_start_s','explorer_launch_s',
                           'outlook_launch_s','word_launch_s') {
                if ($null -eq $out[$k] -and $null -ne $j.$k) { $out[$k] = $j.$k }
            }
            break
        }
    }
} catch { $null = $_ }

# Word COM add-ins loading at startup. More (and slower) add-ins are the usual
# cause of a slow Word cold-start; this names them so the slow ones can be
# disabled. HKCU + HKLM are readable unprivileged. READ-ONLY.
try {
    $paths = @(
        'HKCU:\Software\Microsoft\Office\Word\Addins',
        'HKLM:\Software\Microsoft\Office\Word\Addins',
        'HKLM:\Software\WOW6432Node\Microsoft\Office\Word\Addins'
    )
    $addins = @{}
    foreach ($base in $paths) {
        if (Test-Path $base) {
            foreach ($k in Get-ChildItem $base -ErrorAction SilentlyContinue) {
                $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
                $lb = $p.LoadBehavior
                if ($null -ne $lb -and (([int]$lb) -band 2)) {   # bit1 = load at startup
                    $nm = if ($p.FriendlyName) { [string]$p.FriendlyName } else { [string]$k.PSChildName }
                    $addins[[string]$k.PSChildName] = $nm
                }
            }
        }
    }
    $out.word_addins = $addins.Count
    $out.word_addin_list = @($addins.Values | Sort-Object -Unique | Select-Object -First 20)
} catch { $null = $_ }

$out | ConvertTo-Json -Depth 4 -Compress
