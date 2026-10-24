# Restart WSL to activate the .wslconfig cap (Remediation Plan P0-1 + P1-4),
# then verify automatically.
#
# This MUST run as a detached Windows process: `wsl --shutdown` kills every WSL
# shell, including the kiro-cli session that launched it. A Windows process is not
# a child of the WSL VM, so it survives and can do the verification the dying
# session cannot.

$ErrorActionPreference = 'Continue'
$out = "$env:USERPROFILE\wsl-cap-restart.log"

function Log([string]$m) {
    $line = '[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $m
    Add-Content -Path $out -Value $line
}

function ProcWS([string]$name) {
    $p = Get-Process $name -ErrorAction SilentlyContinue
    if ($p) { return [string][math]::Round(($p | Measure-Object WS -Sum).Sum / 1MB, 0) }
    return 'n/a'
}

function FreeMB() {
    $os = Get-CimInstance Win32_OperatingSystem
    return [string][math]::Round($os.FreePhysicalMemory / 1KB, 0)
}

Set-Content -Path $out -Value ('=== WSL cap activation - ' +
    (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ===')

Log 'Grace period before shutdown (8s)...'
Start-Sleep -Seconds 8

Log '--- BEFORE (Windows side) ---'
Log ('vmmemWSL     : ' + (ProcWS 'vmmemWSL') + ' MB')
Log ('Compression  : ' + (ProcWS 'Memory Compression') + ' MB')
Log ('FreePhysical : ' + (FreeMB) + ' MB')

Log 'Running: wsl --shutdown'
$sd = & wsl.exe --shutdown 2>&1
foreach ($l in $sd) { if ("$l".Trim()) { Log ('  wsl: ' + $l) } }

Log 'Waiting 15s for the VM to stop fully...'
Start-Sleep -Seconds 15

Log '--- wsl -l -v ---'
$lv = (& wsl.exe -l -v 2>&1) -replace "`0", ''
foreach ($l in $lv) { if ("$l".Trim()) { Log ('  ' + $l) } }

Log 'Restarting WSL; waiting for systemd to bring services up (55s)...'
$boot = & wsl.exe -d Ubuntu -- bash -lc "sleep 55; echo guest-up" 2>&1
foreach ($l in $boot) { if ("$l".Trim()) { Log ('  guest: ' + $l) } }

Log '--- GUEST STATE AFTER RESTART ---'
$probe = 'echo nproc=$(nproc); ' +
         'echo mem_total_mb=$(free -m | awk ''NR==2{print $2}''); ' +
         'echo mem_avail_mb=$(free -m | awk ''NR==2{print $7}''); ' +
         'echo swap_mb=$(free -m | awk ''NR==3{print $2}''); ' +
         'for s in docker nginx 3pp-dashboard-api sysperf-dashboard test-agent-api; do ' +
         'echo svc_$s=$(systemctl is-active $s 2>/dev/null); done; ' +
         'echo containers=$(docker ps -q 2>/dev/null | wc -l)'
$g = & wsl.exe -d Ubuntu -- bash -lc $probe 2>&1
foreach ($l in $g) { if ("$l".Trim()) { Log ('  ' + $l) } }

Log '--- AFTER (Windows side) ---'
Start-Sleep -Seconds 5
Log ('vmmemWSL     : ' + (ProcWS 'vmmemWSL') + ' MB')
Log ('Compression  : ' + (ProcWS 'Memory Compression') + ' MB')
Log ('FreePhysical : ' + (FreeMB) + ' MB')

# --- verdict --------------------------------------------------------------
function Field($lines, [string]$name) {
    foreach ($l in $lines) {
        $t = "$l".Trim()
        if ($t.StartsWith($name + '=')) { return $t.Substring($name.Length + 1).Trim() }
    }
    return ''
}
$np = Field $g 'nproc'
$mt = Field $g 'mem_total_mb'

Log ''
if (-not $np -or -not $mt) {
    Log ("RESULT: INCONCLUSIVE - could not read guest state (nproc='" + $np +
         "' mem='" + $mt + "'). Open a WSL shell and run: nproc; free -m")
}
elseif (([int]$np -le 6) -and ([int]$mt -le 10600)) {
    Log ('RESULT: CAP ACTIVE  (nproc=' + $np + ', guest RAM=' + $mt +
         ' MB) - target was 6 vCPU / ~10 GB')
}
else {
    Log ('RESULT: CAP NOT ACTIVE (nproc=' + $np + ', guest RAM=' + $mt +
         ' MB) - check $env:USERPROFILE\.wslconfig')
}

Log ''
Log ('Full log     : ' + $out)
Log 'Detail report: run  bash ~/work/verify-wsl-cap.sh  in a new WSL shell'
Log 'Dashboard    : http://localhost:8888/sysperf/'
Log '=== done ==='
