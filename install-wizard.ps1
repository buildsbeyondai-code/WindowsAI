<#
    install-wizard.ps1 — graphical installer for SysPerf.

    A no-admin, per-user Windows Forms wizard that wraps install.ps1: it checks
    prerequisites, lets you pick the port and start-up options, runs the install
    with a live log, and offers to open the dashboard when done.

    Run:  powershell -ExecutionPolicy Bypass -File install-wizard.ps1
    (or just double-click it via the bundled SysPerf-Setup .exe).

    Part of SysPerf — Apache-2.0. See LICENSE.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $here 'setup-runner.ps1'

# --- resolve a Python interpreter (for the prerequisite line) ----------------
function Find-Python {
    foreach ($cmd in @('py -3', 'python', 'python3')) {
        $parts = $cmd.Split(' ')
        try {
            $extra = @(); if ($parts.Count -gt 1) { $extra = @($parts[1..($parts.Count-1)]) }; $v = & $parts[0] @extra -c "import sys; assert sys.version_info >= (3,9); import venv, ensurepip; print(sys.executable); print(sys.version.split()[0])" 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match '(\d+\.\d+\.\d+)') { return [pscustomobject]@{ Path = [string](@($v)[0]); Version = $Matches[1] } }
        } catch {}
    }
    return $null
}
$py = Find-Python

# --- colours / fonts ---------------------------------------------------------
$ink   = [System.Drawing.Color]::FromArgb(29,29,31)
$muted = [System.Drawing.Color]::FromArgb(110,110,115)
$accent= [System.Drawing.Color]::FromArgb(0,113,227)
$panel = [System.Drawing.Color]::White
$fontH = New-Object System.Drawing.Font('Segoe UI',16,[System.Drawing.FontStyle]::Bold)
$fontN = New-Object System.Drawing.Font('Segoe UI',9.5)
$fontM = New-Object System.Drawing.Font('Consolas',9)

# --- form --------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = 'SysPerf Setup'
$form.Size = New-Object System.Drawing.Size(640,560)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.BackColor = $panel

$header = New-Object System.Windows.Forms.Panel
$header.Size = New-Object System.Drawing.Size(640,74)
$header.Location = New-Object System.Drawing.Point(0,0)
$header.BackColor = [System.Drawing.Color]::FromArgb(15,20,32)
$form.Controls.Add($header)

$title = New-Object System.Windows.Forms.Label
$title.Text = 'Install SysPerf'
$title.Font = $fontH
$title.ForeColor = [System.Drawing.Color]::White
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(22,14)
$header.Controls.Add($title)

$sub = New-Object System.Windows.Forms.Label
$sub.Text = 'Self-improving Windows performance dashboard · per-user · no admin'
$sub.Font = $fontN
$sub.ForeColor = [System.Drawing.Color]::FromArgb(154,167,189)
$sub.AutoSize = $true
$sub.Location = New-Object System.Drawing.Point(24,46)
$header.Controls.Add($sub)

function New-Label($text,$x,$y,$w,$color,$font) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Location = New-Object System.Drawing.Point($x,$y)
    $l.Size = New-Object System.Drawing.Size($w,20); $l.ForeColor = $color; $l.Font = $font
    $form.Controls.Add($l); return $l
}

# prerequisite line
$preIcon = if ($py) { '✓' } else { '!' }
$preCol  = if ($py) { [System.Drawing.Color]::FromArgb(29,135,74) } else { [System.Drawing.Color]::FromArgb(200,120,0) }
$preText = if ($py) { "Python detected: $($py.Path) ($($py.Version))" } else { 'Python not found — install Python 3.9+ from python.org, then re-run.' }
New-Label "$preIcon  $preText" 24 92 580 $preCol $fontN | Out-Null

# options
New-Label 'Dashboard port' 24 134 140 $ink $fontN | Out-Null
$portBox = New-Object System.Windows.Forms.TextBox
$portBox.Text = '5056'; $portBox.Location = New-Object System.Drawing.Point(170,131)
$portBox.Size = New-Object System.Drawing.Size(80,24); $portBox.Font = $fontN
$form.Controls.Add($portBox)

$cbAuto = New-Object System.Windows.Forms.CheckBox
$cbAuto.Text = 'Start automatically when I sign in'
$cbAuto.Checked = $true; $cbAuto.Location = New-Object System.Drawing.Point(24,168)
$cbAuto.Size = New-Object System.Drawing.Size(400,24); $cbAuto.Font = $fontN
$form.Controls.Add($cbAuto)

$cbLaunch = New-Object System.Windows.Forms.CheckBox
$cbLaunch.Text = 'Open the dashboard when setup finishes'
$cbLaunch.Checked = $true; $cbLaunch.Location = New-Object System.Drawing.Point(24,196)
$cbLaunch.Size = New-Object System.Drawing.Size(400,24); $cbLaunch.Font = $fontN
$form.Controls.Add($cbLaunch)

New-Label 'Installs under %LOCALAPPDATA%\SysPerf. Nothing outside your profile is changed.' 24 226 580 $muted $fontN | Out-Null

# log box
$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true; $log.ScrollBars = 'Vertical'; $log.ReadOnly = $true
$log.Location = New-Object System.Drawing.Point(24,256)
$log.Size = New-Object System.Drawing.Size(590,190); $log.Font = $fontM
$log.BackColor = [System.Drawing.Color]::FromArgb(11,16,32)
$log.ForeColor = [System.Drawing.Color]::FromArgb(159,208,255)
$form.Controls.Add($log)

$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(24,456)
$bar.Size = New-Object System.Drawing.Size(590,8); $bar.Style = 'Marquee'; $bar.Visible = $false
$form.Controls.Add($bar)

# buttons
$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = 'Install'; $btnInstall.Location = New-Object System.Drawing.Point(438,476)
$btnInstall.Size = New-Object System.Drawing.Size(90,30); $btnInstall.Font = $fontN
$btnInstall.BackColor = $accent; $btnInstall.ForeColor = [System.Drawing.Color]::White
$btnInstall.FlatStyle = 'Flat'; $btnInstall.Enabled = [bool]$py
$form.Controls.Add($btnInstall)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = 'Close'; $btnClose.Location = New-Object System.Drawing.Point(534,476)
$btnClose.Size = New-Object System.Drawing.Size(80,30); $btnClose.Font = $fontN
$btnClose.FlatStyle = 'Flat'
$form.Controls.Add($btnClose)
$btnClose.Add_Click({ $form.Close() })

# --- install action ----------------------------------------------------------
$script:proc = $null
$script:outFile = $null
$script:url = "http://127.0.0.1:$($portBox.Text)/"

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 400

$btnInstall.Add_Click({
    if ($btnInstall.Tag -eq 'done') { Start-Process $script:url; return }
    if (-not (Test-Path $installer)) {
        $log.AppendText("ERROR: install.ps1 not found next to the wizard.`r`n"); return
    }
    $port = 0; if (-not [int]::TryParse($portBox.Text, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { $log.AppendText("Enter a port from 1 to 65535.`r`n"); return }
    $script:url = "http://127.0.0.1:$port/"
    $script:statusFile = [IO.Path]::GetTempFileName()
    $args = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',"`"$installer`"",'-Port',"$port",'-StatusFile', ('"' + $script:statusFile + '"'),'-PythonExe', ('"' + $py.Path + '"'))
    if (-not $cbAuto.Checked) { $args += '-NoAutoStart' }
    $btnInstall.Enabled = $false; $portBox.Enabled = $false
    $cbAuto.Enabled = $false; $cbLaunch.Enabled = $false
    $bar.Visible = $true
    $log.AppendText("Installing on port $port ...`r`n")
    $script:outFile = [System.IO.Path]::GetTempFileName()
    $script:proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $args `
        -RedirectStandardOutput $script:outFile -RedirectStandardError "$($script:outFile).err" `
        -WindowStyle Hidden -PassThru
    $script:lastLen = 0
    $timer.Start()
})

$timer.Add_Tick({
    try {
        $chunks = foreach ($f in @($script:outFile, "$($script:outFile).err")) {
            if ($f -and (Test-Path -LiteralPath $f)) {
                Get-Content -Raw -LiteralPath $f -ErrorAction SilentlyContinue
            }
        }
        $txt = $chunks -join "`r`n"
        if ($txt) { $log.Text = $txt; $log.SelectionStart = $log.Text.Length; $log.ScrollToCaret() }
    } catch {}
    if ($script:proc -and $script:proc.HasExited) {
        $timer.Stop(); $bar.Visible = $false; $script:proc.WaitForExit()
        $exitStatus = 'unknown'
        if (Test-Path -LiteralPath $script:statusFile) {
            $reported = [IO.File]::ReadAllText($script:statusFile).Trim()
            if ($reported -match '^\d+$') { $exitStatus = $reported }
        }
        if ($exitStatus -eq '0') {
            $log.AppendText("`r`n✓ Installed successfully.`r`n")
            $btnInstall.Text = 'Open dashboard'; $btnInstall.Enabled = $true
            $btnInstall.Tag = 'done'
            if ($cbLaunch.Checked) { Start-Process $script:url }
        } else {
            $log.AppendText("`r`n✗ Install failed (exit $exitStatus). See the log above.`r`n")
            $btnInstall.Enabled = $true; $portBox.Enabled = $true
            $cbAuto.Enabled = $true; $cbLaunch.Enabled = $true
        }
    }
})

# second click (after success) opens the dashboard


[void]$form.ShowDialog()

