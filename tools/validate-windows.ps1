<#
.SYNOPSIS
  SysPerf 4.0 Windows-native validation. Run on a real Windows 10/11 machine
  from the unpacked release folder:

      powershell -ExecutionPolicy Bypass -File tools\validate-windows.ps1

  Every check prints PASS / FAIL / SKIP with the evidence. Nothing is changed on
  the machine: it only reads counters, starts SysPerf on a spare port against a
  throwaway data directory, queries it, and stops it again.

.PARAMETER SkipPytest   Do not run the Python test suite (CI runs it separately).
.PARAMETER Port         Port for the temporary server (default 5199).
#>
param([switch]$SkipPytest, [int]$Port = 5199)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$results = New-Object System.Collections.Generic.List[object]
function Check($name, [scriptblock]$body) {
    try {
        $detail = & $body
        $results.Add([pscustomobject]@{Check = $name; Result = 'PASS'; Detail = "$detail"})
    } catch {
        $results.Add([pscustomobject]@{Check = $name; Result = 'FAIL'; Detail = $_.Exception.Message})
    }
}
function Skip($name, $why) { $results.Add([pscustomobject]@{Check = $name; Result = 'SKIP'; Detail = $why}) }

$os = Get-CimInstance Win32_OperatingSystem
Check 'Windows version (10/11)' {
    if ([int]$os.BuildNumber -lt 10240) { throw "build $($os.BuildNumber) is older than Windows 10" }
    "$($os.Caption) build $($os.BuildNumber)"
}
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Check 'Privilege level reported' { if ($isAdmin) { 'administrator' } else { 'standard user (expected default)' } }
Check 'PowerShell available' { "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)" }

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
Check 'Python 3.9+' {
    if (-not $py) { throw 'python not on PATH' }
    $v = & $py -c "import sys;print('%d.%d'%sys.version_info[:2]);sys.exit(0 if sys.version_info>=(3,9) else 1)"
    if ($LASTEXITCODE) { throw "Python $v is too old" }; "Python $v"
}

$collector = Join-Path $root 'src\sysperf\collect_win.ps1'
Check 'Collector emits CPU, memory, disk, process keys' {
    $json = & powershell -NoProfile -ExecutionPolicy Bypass -File $collector -Full
    $o = $json | ConvertFrom-Json
    $need = 'cpu_pct','committed_mb','available_mb','total_ram_mb','c_free_gb','processes','handles'
    $missing = $need | Where-Object { $null -eq $o.$_ }
    if ($missing) { throw "missing: $($missing -join ', ')" }
    "cpu=$($o.cpu_pct)% avail=$($o.available_mb)MB free=$($o.c_free_gb)GB procs=$($o.processes)"
}

$env:PYTHONPATH = Join-Path $root 'src'
$data = Join-Path $env:TEMP ("sysperf-validate-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $data | Out-Null
$env:SYSPERF_DATA_DIR = $data
Check 'sysperf --where (platform resolution)' {
    $w = & $py -m sysperf --where; if ($LASTEXITCODE) { throw 'non-zero exit' }
    if (-not ($w -match 'windows')) { throw "platform not detected as windows:`n$w" }; 'platform=windows'
}

$proc = $null
Check 'Background service starts and serves the dashboard' {
    $script:proc = Start-Process -FilePath $py -ArgumentList '-m','sysperf','--port',"$Port",'--mode','observe' `
        -PassThru -WindowStyle Hidden
    $ok = $false
    for ($i = 0; $i -lt 60 -and -not $ok; $i++) {
        Start-Sleep -Seconds 1
        try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3; $ok = $true } catch { }
    }
    if (-not $ok) { throw 'no /api/health within 60 s' }
    $page = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 10
    if ($page.Content -notmatch 'AI Self-Evolution') { throw 'dashboard missing the 4.0 tab' }
    "can_collect=$($h.platform.can_collect)"
}
Check 'First sample collected (monitoring loop)' {
    for ($i = 0; $i -lt 90; $i++) {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 5
        if ($h.samples -gt 0) { return "samples=$($h.samples)" }
        Start-Sleep -Seconds 2
    }
    throw 'no sample in 180 s'
}
Check 'Dashboard responsiveness (/api/metrics < 3 s)' {
    $t = Measure-Command { Invoke-RestMethod "http://127.0.0.1:$Port/api/metrics" -TimeoutSec 10 | Out-Null }
    if ($t.TotalSeconds -gt 3) { throw "$([math]::Round($t.TotalSeconds,2)) s" }; "$([math]::Round($t.TotalMilliseconds)) ms"
}
Check 'Default autonomy is observe; evolution reports why it is idle' {
    $a = Invoke-RestMethod "http://127.0.0.1:$Port/api/autonomy"
    $c = Invoke-RestMethod "http://127.0.0.1:$Port/api/codevolve"
    if ($a.level -ne 'observe') { throw "level=$($a.level)" }
    "isolation=$($c.isolation.backend); gate=$($c.gate)"
}
Check 'Offline: works with no AI provider' {
    $c = Invoke-RestMethod "http://127.0.0.1:$Port/api/codevolve"
    if ($c.llm.available) { throw 'an AI provider is configured; offline path not exercised' }
    $r = Invoke-RestMethod "http://127.0.0.1:$Port/api/rca"; 'rule-based RCA served without AI'
}
Check 'Clean shutdown' {
    if ($script:proc) { Stop-Process -Id $script:proc.Id -Force; Start-Sleep 1 }
    if (Get-Process -Id $script:proc.Id -ErrorAction SilentlyContinue) { throw 'still running' }; 'stopped'
}
Check 'Scheduled-task API available (installer dependency)' {
    Get-Command Register-ScheduledTask | Out-Null; 'ScheduledTasks module present'
}

if ($SkipPytest) { Skip 'Python test suite' '-SkipPytest' }
else {
    Check 'Python test suite (pytest)' {
        Push-Location $root
        try { $out = & $py -m pytest -q -rs 2>&1; if ($LASTEXITCODE) { throw ($out | Select-Object -Last 15 | Out-String) }
              ($out | Select-Object -Last 1) } finally { Pop-Location }
    }
}
Remove-Item -Recurse -Force $data -ErrorAction SilentlyContinue
$results | Format-Table -AutoSize -Wrap
$failed = @($results | Where-Object Result -eq 'FAIL').Count
Write-Host "`n$failed check(s) failed."
exit $failed
