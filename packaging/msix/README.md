# SysPerf for the Microsoft Store (MSIX)

`SysPerf.exe` is a native window (WebView2) onto the dashboard. If SysPerf is
already running on 127.0.0.1:5056 it attaches to it; otherwise it runs the
dashboard in-process and closes it when the window closes.

## Build

```powershell
cd packaging\msix
.\build-msix.ps1 -IdentityName "<Package/Identity/Name>" `
                 -Publisher "<Package/Identity/Publisher>" `
                 -PublisherDisplayName "<Package/Properties/PublisherDisplayName>"
```

Output: `%LOCALAPPDATA%\SysPerf\msix-build\SysPerf_<version>_x64.msix`.
No Windows SDK or admin rights needed; makeappx is fetched from the
`Microsoft.Windows.SDK.BuildTools` NuGet package on first run.

## Submit to the Store

1. Sign up at <https://partner.microsoft.com/dashboard> (Windows & Xbox
   developer account; individual accounts are free).
2. **Apps and games > New product > MSIX or PWA app**, reserve the name "SysPerf"
   (or another name if it's taken).
3. **Product management > Product identity**: copy *Package/Identity/Name*,
   *Package/Identity/Publisher* and *Package/Properties/PublisherDisplayName*.
4. Rebuild with those three values (above). The defaults (`SysPerf.Dashboard`,
   `CN=SysPerf`) are placeholders and Partner Center rejects them.
5. Start a submission. **Packages**: upload the .msix (unsigned is fine, because the
   Store signs it). **Store listings**: description, at least 1 screenshot (1366x768 or
   larger), and the app icon. **Properties**: category *Utilities & tools*, plus a
   privacy policy URL (required because the app collects system data, even locally).
6. **Submission options > Restricted capabilities**: explain `runFullTrust`:
   *"SysPerf is a desktop performance monitor. It runs PowerShell and WMI
   queries to measure CPU, memory, disk and network, which needs a full-trust
   Win32 process."*
7. Submit. Certification usually takes 1–3 business days.

## Certification risks to settle before submitting

Store policy 10.2/10.8 reviewers check that an app doesn't change the system
in unexpected ways. These SysPerf features act outside the app and should be off by
default in the Store build, or clearly opt-in:

- stopping OneDrive at start-up and on unlock (session policy)
- restarting Explorer (runaway-CPU watchdog)
- tray auto-quit (ends other apps' processes)
- editing Edge policies and other HKCU tweaks
- AI self-evolution: it needs git, WSL and Claude Code, and patches its own source,
  which a packaged (read-only, signed) app can't do. It shows as unavailable.

Inside the package, writes to `%LOCALAPPDATA%` are virtualized per package, so the
Store app keeps its own data separate from a scripted install.

## Local test install

Either turn on Developer Mode and run `Add-AppxPackage -Register
%LOCALAPPDATA%\SysPerf\msix-build\layout\AppxManifest.xml`, or build with
`-SignForTest`, trust `SysPerf-test.cer` under *Local Machine > Trusted People*
(admin), then double-click the .msix.
