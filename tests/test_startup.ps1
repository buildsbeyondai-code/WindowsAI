# Only the disposable profile below is modified. Never changes host services/power.
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot '../src/sysperf/scripts/optimize-startup.ps1'
$savedProfile = $env:USERPROFILE
$testProfile = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testProfile | Out-Null
try {
    $env:USERPROFILE = $testProfile
    $cfg = Join-Path $testProfile '.wslconfig'
    $original = "# custom settings`r`n[wsl2]`r`nmemory=8GB`r`nprocessors=2`r`nnetworkingMode=mirrored`r`n[experimental]`r`nautoMemoryReclaim=disabled`r`n"
    [IO.File]::WriteAllText($cfg, $original)
    & $script
    if ([IO.File]::ReadAllText($cfg) -ne $original) { throw 'Default invocation changed config' }
    & $script -TuneWsl -WhatIf
    if ([IO.File]::ReadAllText($cfg) -ne $original) { throw 'WhatIf changed config' }
    & $script -TuneWsl
    $first = [IO.File]::ReadAllText($cfg)
    foreach ($setting in 'memory=8GB', 'processors=2', 'networkingMode=mirrored', 'autoMemoryReclaim=gradual', 'sparseVhd=true', 'vmIdleTimeout=60000') {
        if (-not $first.Contains($setting)) { throw "Missing setting $setting" }
    }
    $backups = @(Get-ChildItem -LiteralPath $testProfile -Filter '*.bak')
    if ($backups.Count -ne 1 -or [IO.File]::ReadAllText($backups[0].FullName) -ne $original) { throw 'Backup mismatch' }
    & $script -TuneWsl
    if ([IO.File]::ReadAllText($cfg) -ne $first) { throw 'Not idempotent' }
    if (@(Get-ChildItem -LiteralPath $testProfile -Filter '*.bak').Count -ne 1) { throw 'Unnecessary backup on second run' }
    Write-Host 'Startup config tests passed'
} finally {
    $env:USERPROFILE = $savedProfile
    Remove-Item -LiteralPath $testProfile -Recurse -Force
}
