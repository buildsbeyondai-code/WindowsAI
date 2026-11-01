# Disposable test-certificate run for Host.verify_signed (audit F2, item 2).
#
# Run ELEVATED on a DISPOSABLE Windows VM (it trusts a throwaway root for a
# moment), from a checkout of impl/5.5.x-audit-remediation with Python 3.10+:
#
#   powershell -ExecutionPolicy Bypass -File docs\release-engineering\evidence\testcert-run.ps1
#
# Expected: every line ends with the expected verdict and the last line is
# "ALL 5 CASES AS EXPECTED". The script removes its certificates at the end.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$work = Join-Path $env:TEMP "sysperf-testcert-$(Get-Random)"
New-Item -ItemType Directory $work | Out-Null
$py = (Get-Command python).Source

function Test-Signature($file, $signer) {
  $code = @"
import json, sys
sys.path.insert(0, r'$repo\src')
from pathlib import Path
from sysperf import upgrader, codevolve
h = upgrader.Host(run=codevolve.run_text, get=None, staging=Path(r'$work'),
                  repo=Path(r'$repo'), data_dir=Path(r'$work'))
try:
    print('ACCEPTED', json.dumps(h.verify_signed(Path(r'$file'), '$signer')))
except ValueError as e:
    print('REFUSED', e)
"@
  & $py -c $code
}

$certs = @()
try {
  # a code-signing root+leaf whose subject is exactly an approved signer
  $root = New-SelfSignedCertificate -Type Custom -Subject 'CN=SysPerf Test Root' -KeyUsage CertSign `
          -TextExtension @('2.5.29.19={text}CA=true') -CertStoreLocation Cert:\CurrentUser\My
  $leaf = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Johannes Schindelin, O=Johannes Schindelin' `
          -Signer $root -CertStoreLocation Cert:\CurrentUser\My
  $other = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=Johannes Schindelin, O=Someone Else' `
          -Signer $root -CertStoreLocation Cert:\CurrentUser\My
  $certs = @($root, $leaf, $other)

  $exe = Join-Path $work 'signed.exe'; Copy-Item "$env:WINDIR\System32\whoami.exe" $exe
  # whoami.exe is catalog-signed (no embedded signature); the copy gets the test one
  Set-AuthenticodeSignature $exe -Certificate $leaf | Out-Null
  $wrongO = Join-Path $work 'wrong-o.exe'; Copy-Item "$env:WINDIR\System32\whoami.exe" $wrongO
  Set-AuthenticodeSignature $wrongO -Certificate $other | Out-Null

  $results = @()
  $results += ,@('1 untrusted root, exact CN/O', (Test-Signature $exe 'Johannes Schindelin'), 'REFUSED')

  $rootFile = Join-Path $work 'root.cer'; Export-Certificate -Cert $root -FilePath $rootFile | Out-Null
  Import-Certificate -FilePath $rootFile -CertStoreLocation Cert:\LocalMachine\Root | Out-Null   # admin
  $results += ,@('2 trusted root, exact CN/O', (Test-Signature $exe 'Johannes Schindelin'), 'ACCEPTED')
  $results += ,@('3 trusted root, wrong O', (Test-Signature $wrongO 'Johannes Schindelin'), 'REFUSED')
  $results += ,@('4 trusted root, other approved signer asked', (Test-Signature $exe 'Microsoft Corporation'), 'REFUSED')
  $bytes = [IO.File]::ReadAllBytes($exe); $bytes[200] = $bytes[200] -bxor 0xFF
  $tampered = Join-Path $work 'tampered.exe'; [IO.File]::WriteAllBytes($tampered, $bytes)
  $results += ,@('5 trusted root, one byte changed', (Test-Signature $tampered 'Johannes Schindelin'), 'REFUSED')

  $ok = 0
  foreach ($r in $results) {
    $hit = "$($r[1])".StartsWith($r[2]); if ($hit) { $ok++ }
    "{0,-45} expected {1,-8} got: {2}" -f $r[0], $r[2], $r[1]
  }
  if ($ok -eq $results.Count) { "ALL $ok CASES AS EXPECTED" } else { "MISMATCH: $ok of $($results.Count) as expected" }
} finally {
  Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -eq 'CN=SysPerf Test Root' | Remove-Item -ErrorAction SilentlyContinue
  foreach ($c in $certs) { Remove-Item "Cert:\CurrentUser\My\$($c.Thumbprint)" -ErrorAction SilentlyContinue }
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
