<#
.SYNOPSIS
    Reversible client-side Wi-Fi / network optimisation for the System Performance Dashboard.

.DESCRIPTION
    Applies the SAFE, reversible client-side fixes surfaced by the dashboard's
    "Optimisation & fix plan":
      1. Prefer the 5 GHz band on the Intel Wi-Fi adapter (roaming band preference).
      2. Disable adapter power-saving (stops the NIC downshifting the link when idle).
      3. Flush DNS + reset the socket/IP stack (clears stale/erratic state).

    It does NOT change your router/AP. Wi-Fi CHANNEL and channel-WIDTH overlap are
    an access-point setting - fix those in your router admin page (2.4 GHz -> a
    non-overlapping channel 1/6/11 at 20 MHz; 5 GHz -> a clear 80 MHz channel;
    enable band steering).

    RUN AS ADMINISTRATOR. All steps are reversible - see -Revert.

.PARAMETER Revert
    Undo the changes: restore 'no band preference' and re-enable adapter power saving.
    (Winsock/IP resets are one-shot and need no revert.)

.EXAMPLE
    # From an elevated PowerShell:
    powershell -ExecutionPolicy Bypass -File .\optimize-wifi.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\optimize-wifi.ps1 -Revert

.NOTES
    Winsock/IP stack reset requires a reboot to fully take effect.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$Revert)

$ErrorActionPreference = 'Stop'

# --- require elevation -----------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Warning "This script must be run as Administrator. Right-click PowerShell -> Run as administrator."
    exit 1
}

# --- locate the wireless adapter ------------------------------------------
$wnic = Get-NetAdapter -Physical | Where-Object { $_.PhysicalMediaType -match '802\.11' } | Select-Object -First 1
if (-not $wnic) { Write-Warning "No 802.11 (Wi-Fi) adapter found."; exit 1 }
Write-Information -InformationAction Continue "Wi-Fi adapter: $($wnic.InterfaceDescription) [$($wnic.Name)]"

# 'Preferred Band' advanced property keyword (Intel drivers expose *BandPreference /
# 'PreferredBand'); we discover the actual registry keyword rather than assume it.
function Get-BandProp {
    Get-NetAdapterAdvancedProperty -Name $wnic.Name -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'Preferred Band|Band Preference' -or
                       $_.RegistryKeyword -match 'BandPreference|PreferredBand|RoamPref' } |
        Select-Object -First 1
}

if ($Revert) {
    Write-Information -InformationAction Continue "`n== REVERT =="
    $bp = Get-BandProp
    if ($bp) {
        # 0 / "No Preference" restores the driver default.
        Set-NetAdapterAdvancedProperty -Name $wnic.Name -RegistryKeyword $bp.RegistryKeyword -RegistryValue 0 -NoRestart
        Write-Information -InformationAction Continue "  Band preference reset to 'No Preference'."
    }
    Set-NetAdapterPowerManagement -Name $wnic.Name -AllowComputerToTurnOffDevice Enabled -ErrorAction SilentlyContinue
    Write-Information -InformationAction Continue "  Adapter power-saving re-enabled."
    Write-Information -InformationAction Continue "Done. A reconnect (or reboot) applies the revert."
    exit 0
}

Write-Information -InformationAction Continue "`n== OPTIMISE =="

# 1) Prefer 5 GHz -----------------------------------------------------------
$bp = Get-BandProp
if ($bp) {
    # Intel: 1 = Prefer 2.4 GHz, 2 = Prefer 5 GHz (values vary; 'Prefer 5GHz' display value is used when present).
    $val = ($bp.ValidDisplayValues | Where-Object { $_ -match '5' } | Select-Object -First 1)
    if ($PSCmdlet.ShouldProcess($wnic.Name, "Set Preferred Band to 5 GHz")) {
        if ($val) {
            Set-NetAdapterAdvancedProperty -Name $wnic.Name -RegistryKeyword $bp.RegistryKeyword -DisplayValue $val -NoRestart
        } else {
            Set-NetAdapterAdvancedProperty -Name $wnic.Name -RegistryKeyword $bp.RegistryKeyword -RegistryValue 2 -NoRestart
        }
        Write-Information -InformationAction Continue "  Preferred Band -> 5 GHz ($($bp.RegistryKeyword))."
    }
} else {
    Write-Information -InformationAction Continue "  (Adapter exposes no 'Preferred Band' property - skipping; steer via the router 5 GHz SSID.)"
}

# 2) Disable NIC power saving ----------------------------------------------
if ($PSCmdlet.ShouldProcess($wnic.Name, "Disable power saving")) {
    Set-NetAdapterPowerManagement -Name $wnic.Name -AllowComputerToTurnOffDevice Disabled -ErrorAction SilentlyContinue
    Write-Information -InformationAction Continue "  Adapter power-saving disabled (prevents idle link downshift)."
}

# 3) Flush DNS + reset stack ------------------------------------------------
if ($PSCmdlet.ShouldProcess("network stack", "Flush DNS + reset Winsock/IP")) {
    ipconfig /flushdns | Out-Null
    netsh winsock reset  | Out-Null
    netsh int ip reset   | Out-Null
    Write-Information -InformationAction Continue "  DNS flushed; Winsock + IP stack reset (reboot to fully apply)."
}

Write-Information -InformationAction Continue "`nClient-side optimisation applied. Router-side (channel/width/band-steering) must be"
Write-Information -InformationAction Continue "changed on your access point. Re-run with -Revert to undo. A reboot is recommended."
