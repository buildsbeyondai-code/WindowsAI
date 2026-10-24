[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$StatusFile,
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [int]$Port = 5056,
    [switch]$NoAutoStart,
    [string]$Installer
)
$ErrorActionPreference = 'Stop'
$code = 1
try {
    if (-not $Installer) {
        $Installer = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'install.ps1'
    }
    & $Installer -Port $Port -PythonExe $PythonExe -NonInteractive -NoAutoStart:$NoAutoStart
    if (-not $?) { throw 'The installation script failed.' }
    $code = 0
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
} finally {
    [IO.File]::WriteAllText($StatusFile, [string]$code)
}
exit $code
