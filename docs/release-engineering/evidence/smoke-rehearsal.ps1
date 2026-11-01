# Local rehearsal of the msix.yml smoke step: same checks, source entry point,
# and an empty temp dir as the "clean" profile so the live data dir is never read.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSCommandPath
$wt = Join-Path $env:LOCALAPPDATA 'SysPerf\wt-audit'
$py = Join-Path $env:LOCALAPPDATA 'SysPerf\venv\Scripts\python.exe'
function Fail($why) { "FAIL: $why"; exit 1 }
$clean = Join-Path $root clean; $stale = Join-Path $root stale-profile
Remove-Item -Recurse -Force $clean, $stale -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $clean, $stale | Out-Null
'{"t": 1000000000, "available_mb": 1, "total_ram_mb": 2, "committed_mb": 3, "processes": 4}' |
  Set-Content -Encoding utf8 (Join-Path $stale history.jsonl)
$env:PYTHONPATH = Join-Path $wt src
foreach ($kind in @('clean', 'stale')) {
  $env:SYSPERF_DATA_DIR = if ($kind -eq 'stale') { $stale } else { $clean }
  $out = Join-Path $root "smoke-$kind.json"
  $began = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
  $p = Start-Process $py -ArgumentList "`"$wt\packaging\msix\sysperf_app.py`" --smoke `"$out`"" -PassThru -WindowStyle Hidden
  if (-not $p.WaitForExit(300000)) { $p.Kill(); Fail "$kind profile: did not finish in 5 minutes" }
  $body = if (Test-Path $out) { Get-Content -Raw $out } else { '' }
  $head = $body.Substring(0, [Math]::Min(300, $body.Length))
  if ($p.ExitCode -ne 0 -or -not $body) { Fail "$kind profile: exit code $($p.ExitCode); output: $head" }
  $j = $body | ConvertFrom-Json
  if ($null -eq $j.available_mb) { Fail "$kind profile: no available_mb: $head" }
  if ($null -eq $j._t -or [double]$j._t -lt $began - 2) { Fail "$kind profile: sample time $($j._t) before call began ($began)" }
  "$kind profile: rc $($p.ExitCode)  available $($j.available_mb) MB  processes $($j.processes)  taken $([math]::Round([double]$j._t - $began, 1)) s after start  keys $(($j.PSObject.Properties | Measure-Object).Count)"
}
