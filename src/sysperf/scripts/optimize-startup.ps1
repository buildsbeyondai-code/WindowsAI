<#
.SYNOPSIS
    Explicit, previewable startup settings. No changes without an action switch.
.DESCRIPTION
    -TuneWsl merges settings without dropping existing memory/CPU/network limits.
    -EnableSysMain and -HighPerformance are separate opt-ins requiring suitable
    rights. Save work before restarting WSL; this script never restarts it.
    Use -WhatIf or -WhatIfOnly to preview. WSL changes keep a unique backup.
#>
[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='Medium')]
param(
    [switch]$TuneWsl,
    [switch]$EnableSysMain,
    [switch]$HighPerformance,
    [switch]$WhatIfOnly
)
$ErrorActionPreference = 'Stop'
if ($WhatIfOnly) { $WhatIfPreference = $true }

function Merge-IniSetting {
    param([string]$Text, [string]$Section, [string]$Key, [string]$Value)
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in ($Text -split '\r?\n')) { $lines.Add($line) }
    $sectionStart = -1
    $sectionEnd = $lines.Count
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*\[([^\]]+)\]\s*(?:[#;].*)?$') {
            if ($sectionStart -ge 0) { $sectionEnd = $i; break }
            if ($Matches[1] -ieq $Section) { $sectionStart = $i }
        }
    }
    if ($sectionStart -lt 0) {
        $lines.Add("[$Section]"); $lines.Add("$Key=$Value")
    } else {
        $found = $false
        for ($i = $sectionStart + 1; $i -lt $sectionEnd; $i++) {
            if ($lines[$i] -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
                $lines[$i] = "$Key=$Value"; $found = $true
            }
        }
        if (-not $found) { $lines.Insert($sectionEnd, "$Key=$Value") }
    }
    return ($lines -join "`r`n").TrimEnd() + "`r`n"
}

if ($TuneWsl) {
    $wslcfg = Join-Path $env:USERPROFILE '.wslconfig'
    $original = if (Test-Path -LiteralPath $wslcfg) { [IO.File]::ReadAllText($wslcfg) } else { '' }
    $desired = Merge-IniSetting $original 'wsl2' 'vmIdleTimeout' '60000'
    $desired = Merge-IniSetting $desired 'experimental' 'autoMemoryReclaim' 'gradual'
    $desired = Merge-IniSetting $desired 'experimental' 'sparseVhd' 'true'
    if ($desired -ne $original -and $PSCmdlet.ShouldProcess($wslcfg, 'Merge WSL settings with a backup')) {
        if (Test-Path -LiteralPath $wslcfg) {
            $backup = "$wslcfg.$([guid]::NewGuid().ToString('N')).bak"
            Copy-Item -LiteralPath $wslcfg -Destination $backup
            Write-Host "Backup: $backup"
        }
        [IO.File]::WriteAllText($wslcfg, $desired, [Text.UTF8Encoding]::new($false))
        Write-Host 'WSL settings merged. Save work before manually restarting WSL.'
    }
}
if ($EnableSysMain -and $PSCmdlet.ShouldProcess('SysMain', 'Set Automatic and start service')) {
    Set-Service -Name SysMain -StartupType Automatic
    Start-Service -Name SysMain
}
if ($HighPerformance -and $PSCmdlet.ShouldProcess('Power plan', 'Activate High performance, including on battery')) {
    $previous = & powercfg /getactivescheme
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the current power plan.' }
    Write-Host "Previous plan (record its GUID to restore): $previous"
    & powercfg /setactive SCHEME_MIN
    if ($LASTEXITCODE -ne 0) { throw 'Could not activate High performance.' }
}
if (-not ($TuneWsl -or $EnableSysMain -or $HighPerformance)) {
    Write-Host 'No action selected. Use -TuneWsl, -EnableSysMain or -HighPerformance; add -WhatIf to preview.'
}
