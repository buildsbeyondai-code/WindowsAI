<#
.SYNOPSIS
  Build SysPerf as an MSIX package for the Microsoft Store.

.DESCRIPTION
  1. Creates a clean build venv and installs SysPerf + PyInstaller + pywebview.
  2. Freezes packaging\msix\sysperf_app.py into a one-folder SysPerf.exe.
  3. Lays out the package (exe folder, logos, AppxManifest.xml).
  4. Packs it with makeappx.exe (downloaded from the Windows SDK BuildTools
     NuGet package when the Windows SDK is not installed; no admin needed).
  5. Optionally signs it with a self-signed certificate for a local test install.

  The Store re-signs what you upload, so the unsigned .msix is what goes to
  Partner Center. -IdentityName / -Publisher / -PublisherDisplayName must be the
  exact values from Partner Center > your app > Product identity.

.EXAMPLE
  .\build-msix.ps1 -IdentityName 12345Contoso.SysPerf -Publisher "CN=ABCD1234-..." -PublisherDisplayName "Contoso"
  .\build-msix.ps1 -SignForTest     # local test build with a self-signed cert
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingConvertToSecureStringWithPlainText', '',
    Justification = 'Throwaway password for the optional local test-signing certificate')]
param(
    [string]$IdentityName = "SysPerf.Dashboard",
    [string]$Publisher = "CN=SysPerf",
    [string]$PublisherDisplayName = "SysPerf",
    [string]$Version = "",
    [string]$Python = "",
    [string]$OutDir = "",
    [switch]$SignForTest
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
if (-not $OutDir) { $OutDir = Join-Path $env:LOCALAPPDATA "SysPerf\msix-build" }
$Work = Join-Path $OutDir "work"
New-Item -ItemType Directory -Force -Path $Work | Out-Null

if (-not $Version) {
    $m = Select-String -Path (Join-Path $Repo "pyproject.toml") -Pattern '^version\s*=\s*"([\d.]+)"' | Select-Object -First 1
    $v = $m.Matches[0].Groups[1].Value.Split(".")
    while ($v.Count -lt 3) { $v += "0" }
    # The Store requires the fourth part to be 0.
    $Version = "$($v[0]).$($v[1]).$($v[2]).0"
}
Write-Host "SysPerf MSIX $Version  ->  $OutDir"

# --- 1. build venv ------------------------------------------------------------
if (-not $Python) {
    $cfg = Join-Path $env:LOCALAPPDATA "SysPerf\venv\pyvenv.cfg"
    if (Test-Path $cfg) {
        $exe = (Get-Content $cfg | Where-Object { $_ -match '^executable\s*=' }) -replace '^executable\s*=\s*', ''
        if ($exe -and (Test-Path $exe)) { $Python = $exe }
    }
    if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$Venv = Join-Path $Work "venv"
# Always a fresh venv: a reused one kept the previous build's packages, so the
# package was not built from the lock files alone (and pip's conflict warning
# about them stopped the build).
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
& $Python -m venv $Venv; if ($LASTEXITCODE) { throw "venv failed" }
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install -q --upgrade pip
# Reproducible: every runtime and build package comes from a hash-locked file,
# then SysPerf itself is installed without resolving anything else.
& $Py -m pip install -q --require-hashes -r (Join-Path $Repo "requirements.lock") -r (Join-Path $Here "build.lock")
if ($LASTEXITCODE) { throw "pip install (locked dependencies) failed" }
& $Py -m pip install -q --no-deps "$Repo"
if ($LASTEXITCODE) { throw "pip install (SysPerf) failed" }

# --- 2. assets + freeze --------------------------------------------------------
$Layout = Join-Path $OutDir "layout"
if (Test-Path $Layout) { Remove-Item -Recurse -Force $Layout }
$Assets = Join-Path $Layout "Assets"
& $Py (Join-Path $Here "make_assets.py") $Assets; if ($LASTEXITCODE) { throw "assets failed" }

& $Py -m PyInstaller --noconfirm --clean --windowed --name SysPerf `
    --icon (Join-Path $Assets "SysPerf.ico") `
    --collect-data sysperf --collect-submodules sysperf --collect-all webview `
    --distpath (Join-Path $Work "dist") --workpath (Join-Path $Work "build") `
    --specpath $Work (Join-Path $Here "sysperf_app.py")
if ($LASTEXITCODE) { throw "PyInstaller failed" }
Copy-Item -Recurse (Join-Path $Work "dist\SysPerf") (Join-Path $Layout "SysPerf")
Remove-Item (Join-Path $Assets "SysPerf.ico")

# --- 3. manifest ---------------------------------------------------------------
$manifest = Get-Content -Raw (Join-Path $Here "AppxManifest.xml")
$manifest = $manifest.Replace("{{IDENTITY_NAME}}", $IdentityName).Replace("{{PUBLISHER}}", $Publisher)
$manifest = $manifest.Replace("{{PUBLISHER_DISPLAY_NAME}}", [Security.SecurityElement]::Escape($PublisherDisplayName)).Replace("{{VERSION}}", $Version)
[IO.File]::WriteAllText((Join-Path $Layout "AppxManifest.xml"), $manifest, (New-Object Text.UTF8Encoding $false))

# --- 4. makeappx ---------------------------------------------------------------
function Find-SdkTool($name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $kit = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\$name" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($kit) { return $kit.FullName }
    $tools = Join-Path $OutDir "sdk-buildtools"
    if (-not (Test-Path $tools)) {
        Write-Host "Windows SDK not found: downloading Microsoft.Windows.SDK.BuildTools from nuget.org"
        $zip = Join-Path $OutDir "buildtools.zip"
        Invoke-WebRequest "https://www.nuget.org/api/v2/package/Microsoft.Windows.SDK.BuildTools" -OutFile $zip
        Expand-Archive $zip $tools -Force
        Remove-Item $zip
    }
    $hit = Get-ChildItem -Recurse (Join-Path $tools "bin") -Filter $name | Where-Object { $_.FullName -match '\\x64\\' } |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $hit) { throw "$name not found in the SDK BuildTools package" }
    return $hit.FullName
}
$MakeAppx = Find-SdkTool "makeappx.exe"
$Msix = Join-Path $OutDir "SysPerf_${Version}_x64.msix"
if (Test-Path $Msix) { Remove-Item -Force $Msix }
& $MakeAppx pack /o /d $Layout /p $Msix | Select-String -Pattern "error|succeeded" | Out-Host
if ($LASTEXITCODE) { throw "makeappx pack failed" }

# --- 5. optional test signing --------------------------------------------------
if ($SignForTest) {
    $SignTool = Find-SdkTool "signtool.exe"
    $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $Publisher } | Select-Object -First 1
    if (-not $cert) {
        $cert = New-SelfSignedCertificate -Type Custom -Subject $Publisher -KeyUsage DigitalSignature `
            -FriendlyName "SysPerf MSIX test" -CertStoreLocation Cert:\CurrentUser\My `
            -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
    }
    $pfx = Join-Path $OutDir "SysPerf-test.pfx"
    $pw = ConvertTo-SecureString -String "sysperf-test" -Force -AsPlainText
    Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $pw | Out-Null
    Export-Certificate -Cert $cert -FilePath (Join-Path $OutDir "SysPerf-test.cer") | Out-Null
    & $SignTool sign /fd SHA256 /a /f $pfx /p "sysperf-test" $Msix | Out-Host
    if ($LASTEXITCODE) { throw "signtool failed" }
    Write-Host "Signed for local testing. Trust SysPerf-test.cer in 'Local Machine > Trusted People' (admin) before installing."
}
Write-Host ""
Write-Host "Built: $Msix"
