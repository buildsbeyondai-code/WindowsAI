# SysPerf automatic restore points.
#
# Creating a restore point needs an administrator, and SysPerf runs without one.
# So, once, with one UAC prompt, -Mode install registers a scheduled task that
# runs this script elevated as the signed-in user. After that SysPerf starts the
# task (-Mode request, unprivileged) whenever it needs a restore point, and the
# task (-Mode create) writes the outcome to status.json for SysPerf to read.
#
#   -Mode install  -User DOMAIN\name   (elevated) copy the script to a folder only
#                                      administrators can change, register the task,
#                                      turn System Restore on for the system drive
#                                      if it is off, and record the current state
#   -Mode create                       (elevated, run by the task) create one
#   -Mode request  [-WaitSec N]        (unprivileged) start the task, wait for its result
#   -Mode uninstall                    (elevated) remove the task and the folder
param([string]$Mode = 'request', [string]$User = '', [int]$WaitSec = 240,
      [string]$Description = 'SysPerf: before changes')
$ErrorActionPreference = 'Stop'
$Task = 'SysPerf Restore Point'
$Dir = Join-Path $env:ProgramData 'SysPerf\restore'
$Status = Join-Path $Dir 'status.json'

function Write-Status($ok, $message, $created) {
    $rp = @(Get-ComputerRestorePoint -ErrorAction SilentlyContinue)
    $newest = $rp | Sort-Object SequenceNumber | Select-Object -Last 1
    $at = if ($newest) { [DateTimeOffset]::new([Management.ManagementDateTimeConverter]::ToDateTime($newest.CreationTime)).ToUnixTimeSeconds() } else { $null }
    [ordered]@{ ok = [bool]$ok; message = [string]$message; created = [bool]$created
        ts = [DateTimeOffset]::Now.ToUnixTimeSeconds(); newest = $at; count = $rp.Count
        newest_description = if ($newest) { [string]$newest.Description } else { $null }
        drive = "$env:SystemDrive" } | ConvertTo-Json -Compress |
        Set-Content -LiteralPath $Status -Encoding UTF8
}

switch ($Mode) {
    'install' {
        if (-not $User) { throw 'install needs -User' }
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        # Only administrators and SYSTEM may change what the elevated task runs;
        # the user may read the status.
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $inherit = 'ContainerInherit, ObjectInherit'
        foreach ($sid in 'S-1-5-32-544', 'S-1-5-18') {
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
                (New-Object Security.Principal.SecurityIdentifier $sid), 'FullControl', $inherit, 'None', 'Allow'))) }
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-545'), 'ReadAndExecute', $inherit, 'None', 'Allow')))
        Set-Acl -LiteralPath $Dir -AclObject $acl
        $script = Join-Path $Dir 'restore_point.ps1'
        Copy-Item -LiteralPath $PSCommandPath -Destination $script -Force
        $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Mode create"
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $Task -Action $action -Principal $principal -Settings $settings `
            -Description 'Creates a System Restore point when SysPerf asks for one.' -Force | Out-Null
        $enabled = $false
        try { Enable-ComputerRestore -Drive "$env:SystemDrive\"; $enabled = $true } catch { }
        Write-Status $true ("installed" + $(if ($enabled) { "; System Restore is on for $env:SystemDrive" } else { "" })) $false
        'installed'
    }
    'create' {
        try {
            Enable-ComputerRestore -Drive "$env:SystemDrive\" -ErrorAction SilentlyContinue
            $before = @(Get-ComputerRestorePoint -ErrorAction SilentlyContinue).Count
            Checkpoint-Computer -Description $Description -RestorePointType MODIFY_SETTINGS
            $after = @(Get-ComputerRestorePoint -ErrorAction SilentlyContinue).Count
            # Windows skips a checkpoint within 24 h of the last one, without an error.
            if ($after -gt $before) { Write-Status $true 'created' $true }
            else { Write-Status $true 'Windows kept the recent restore point (one per 24 hours)' $false }
        } catch { Write-Status $false $_.Exception.Message $false }
    }
    'request' {
        $t0 = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        & schtasks.exe /run /tn $Task 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { 'the restore-point helper is not set up'; exit 2 }
        $deadline = (Get-Date).AddSeconds($WaitSec)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            try { $s = Get-Content -LiteralPath $Status -Raw | ConvertFrom-Json } catch { continue }
            if ($s.ts -ge $t0) { $s | ConvertTo-Json -Compress; if ($s.ok) { exit 0 } else { exit 1 } }
        }
        "the restore-point helper did not answer within $WaitSec s"; exit 1
    }
    'uninstall' {
        Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction SilentlyContinue
        'removed'
    }
    default { throw "unknown mode $Mode" }
}
