# SysPerf for the Microsoft Store (MSIX)

`SysPerf.exe` is a native window (WebView2) onto the dashboard. If SysPerf is
already running on 127.0.0.1:5056 it attaches to it; otherwise it runs the
dashboard in-process and closes it when the window closes.

## Build

```powershell
cd packaging\msix
.\build-msix.ps1 -IdentityName "<Package/Identity/Name>" `
                 -Publisher "<Package/Identity/Publisher>" `
                 -PublisherDisplayName "<Package/Properties/PublisherDisplayName>" `
                 -DisplayName "<the product name you reserved>"
```

Output: `%LOCALAPPDATA%\SysPerf\msix-build\SysPerf_<version>_<arch>.msix`, where
`<arch>` (`x64` or `arm64`) is the build Python's architecture. PyInstaller cannot
cross-compile, so the Arm64 package needs an Arm64 Python on Windows on Arm.
The **MSIX** workflow (`.github/workflows/msix.yml`, run it from the Actions tab)
builds both on GitHub's `windows-latest` and `windows-11-arm` runners with the
Partner Center identity, and uploads them as the `msix-x64` and `msix-arm64`
artifacts.
No Windows SDK or admin rights needed; makeappx is fetched from the
`Microsoft.Windows.SDK.BuildTools` NuGet package on first run.
Every Python package in the build comes from `requirements.lock` and
`build.lock` with `--require-hashes`, so the same commit builds the same app.

## Submit to the Store

1. Sign up at <https://partner.microsoft.com/dashboard> (Windows & Xbox
   developer account; individual accounts are free).
2. **Apps and games > New product > MSIX or PWA app**, reserve the name "SysPerf"
   (or another name if it's taken).
3. **Product management > Product identity**: copy *Package/Identity/Name*,
   *Package/Identity/Publisher* and *Package/Properties/PublisherDisplayName*.
4. Rebuild with those three values (above). The defaults (`SysPerf.Dashboard`,
   `CN=SysPerf`) are placeholders and Partner Center rejects them.
5. Start a submission. **Packages**: upload both .msix files, x64 and arm64, with the
   same version (unsigned is fine, because the Store signs it). Windows on Arm devices
   get the native Arm64 package; never ship 32-bit `arm`, which future Arm devices drop. **Store listings**: description, at least 1 screenshot (1366x768 or
   larger), and the app icon. **Properties**: category *Utilities & tools*, plus a
   privacy policy URL (publish `PRIVACY.md` and paste its link).
6. **Submission options > Restricted capabilities**: explain `runFullTrust`:
   *"SysPerf is a desktop performance monitor. It runs PowerShell and WMI
   queries to measure CPU, memory, disk and network, which needs a full-trust
   Win32 process."*
7. Submit. Certification usually takes 1–3 business days.

## Store policy compliance (built in)

The packaged app runs in **Store mode** automatically (`config.STORE_MODE`, true
for the frozen `SysPerf.exe`). No feature is removed. Everything that acts
outside the app starts **off** and is listed on Home under *Optional features*,
with what it does, how to undo it, and a one-click switch:

| Feature | Scripted install | Store app |
|---|---|---|
| Automatic fixes (autonomy) | Observe | Observe (opt-in) |
| Explorer watchdog | on | **off** (opt-in) |
| Tray auto-quit (asks per app) | on | **off** (opt-in) |
| OneDrive lock-state policy | env flag | **off** (opt-in, remembered) |
| AI self-evolution | off | off; patches go to *your* source checkout, never into the signed package |

Other Store-specific behaviour:
- **Restart** relaunches the app; there's no supervisor to do it.
- **Self-test** runs from your source checkout, using the installed Python (`py`).
- **Privacy:** `PRIVACY.md` is the policy. Publish it (for example in the GitHub
  repo) and paste its URL into Partner Center.

## Local test install

Either turn on Developer Mode and run `Add-AppxPackage -Register
%LOCALAPPDATA%\SysPerf\msix-build\layout\AppxManifest.xml`, or build with
`-SignForTest`, trust `SysPerf-test.cer` under *Local Machine > Trusted People*
(admin), then double-click the .msix.
