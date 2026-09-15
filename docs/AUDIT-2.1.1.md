# SysPerf 2.1.1 source audit — 2026-09-15

The repository main branch at `ec5592626d2469c85e1db266df327a7f9b6ca0ad`
contained only README.md, .gitignore and .editorconfig. The source of this change
is the user-supplied SysPerf-2.1.0 archive. No machine telemetry or credentials
are included. This is an evidence-based repair pass, not a guarantee that no
undiscovered defects remain.

## Reproduced defects and repairs

| Area | Defect | Repair / evidence |
| --- | --- | --- |
| Repository | Application absent from main | Restore complete source, tests, docs, installers and CI |
| CLI authentication | --host reloaded defaults and could expose an unauthenticated server; lost --port | Apply CLI overrides before token enforcement; CLI integration test |
| HTTP | App factory accepted unauthenticated public config | Refuse unsafe factory configuration |
| Dashboard authentication | URL token was not sent on API calls | Shared API fetch wrapper adds auth header |
| HTTP input | Non-ASCII auth token could cause 500 | Compare UTF-8 bytes |
| Local API | No Host or cross-origin mutation check | Loopback Host allowlist, Origin/Fetch Metadata validation |
| Recalibration | Cross-site POST could reset baseline | Require CSRF header and reject cross-origin writes |
| Fix API | Arrays/scalars/invalid IDs/truthy string confirmations crashed or behaved unexpectedly | Validate JSON types, unknown IDs, body size |
| Fix execution | Empty selection meant all; implicit batch included caution actions | Explicit non-empty API selection; engine empty list means no work; implicit engine batch safe-only |
| Fix UI | Caution actions were checked by default; double submissions possible | Default-select safe actions only; disable controls while pending; server execution lock |
| TEMP cleanup | Permanent deletion advertised as safe/reversible | Manual Windows Storage review; reversible=false |
| WSL tuning | Overwrote existing configuration, reused backup, wrong INI sections | Dashboard action manual-only; standalone script merges settings, keeps unique backup, supports WhatIf and no-action default |
| Startup script | Unconditionally changed power plan even on battery | Separate explicit power/service switches; report previous power GUID |
| Runner | Missing PowerShell raised before exception handling | Catch interpreter discovery failures; surface PowerShell/native command errors instead of reporting false success; existing failing test now passes |
| Collector bridge | Ignored non-zero exit and depended on locale encoding | Reject failed commands; explicit UTF-8 output/input |
| Config | PowerShell UTF-8 BOM files ignored; invalid typed fields crashed or enabled actions | utf-8-sig loading and default-backed type validation |
| Sampling | Invalid JSON record shape crashed sampling/history loading | Reject invalid samples; skip non-object history lines |
| Slow metrics | OS version, power GUID, pagefile usage, WSL CPU configuration and derived battery wear vanished on fast polls | Carry forward fields and derive battery wear after merge |
| Snapshot state | Anonymization mutated raw history, cached objects shared mutable data, cache race | Detached copies and generation-aware cache publication |
| Privacy | Names leaked in latest, baseline, profile endpoint and derived text | Redact known identifiers throughout response projections; preserve raw local state |
| Scoring | New fault with baseline=target=0 got 100% progress | Non-met zero-span criterion gets 0 and regression status |
| Scoring | Zero-valued measured startup/queue counts omitted | Distinguish zero from missing values |
| WSL trend | Zero memory samples dropped, obscuring VM resets | Include zero as reset anchor |
| Adaptive scoring | Personal best and target could regress or expire after daily pruning | Retain historical best when recalculating |
| Hardware | Template example counted as a real user fault | Start with empty watchlist; reject non-object rows |
| Component panel | Stale hardcoded versions presented as current; fabricated gain scores | Report measured inventory and unverified currency; no estimated gains |
| Wi-Fi | Same-channel neighbours ignored, own AP/cross-band counts incorrect | BSSID-aware peer exclusion and band-specific cochannel counts |
| Advice | DNS flush incorrectly said to close TCP connections; Wi-Fi advice on disconnected systems | Correct explanation and suppress disconnected Wi-Fi plan |
| Power advice | Localized power-plan names not recognized | Prefer plan GUID |
| WSL advice | Any .wslconfig treated as a memory cap; wrong section and fixed CPU recommendation | Check actual cap, use experimental section, do not prescribe four CPUs |
| Measurement labels | WSL service startup treated as cold start; historical app events as current launches; ACPI labelled CPU core | Stop WSL service proxy, filter app events to current boot, label startup events and ACPI thermal zone |
| PowerShell metrics | Thread collection summed Thread collections; disk time could exceed 100% | Sum Win32_Process.ThreadCount; derive activity from idle percentage |
| Installers | Python paths with spaces missed; shim broke on apostrophes; failed venv installs not retried; exit errors swallowed | Path-safe discovery/shim, dependency retries, exit checks, port validation |
| Installer lifecycle | Existing port ignored; NoAutoStart left old startup entries; uninstall process matching too broad | Use saved port, remove old autostart, exact executable matching, preserve unrelated data-dir env setting |
| Dependencies | Flask 3.0.3 and Waitress 3.0.0 pinned | Flask 3.1.3 / Waitress 3.0.2; regression tests and package build |
| Packaging | Source distribution lacked installers/docs | MANIFEST.in; wheel retains static and PowerShell assets |
| PowerShell compatibility | CI found 12 encoding/error-handler/naming warnings | UTF-8 BOM for Windows PowerShell 5.1, diagnostic catches, singular helper name |
| Quality gates | Original lint failed and Linux test runner crashed | Clean configured Ruff rules; regression suite; native script tests and explicit CI exit checks |

## Validation

- Original: 117 tests, one reproduced failure (`test_default_runner_exception`).
- Revised: 159 Python tests pass on Linux / Python 3.12, including 42 added regression cases.
- Ruff 0.6.9: clean for src and tests.
- Dashboard JavaScript: Node syntax check passes.
- WSL installer: bash syntax check passes.
- Source distribution and wheel build successfully; metadata validation and asset
  inspection are part of the local validation run.
- Dependency vulnerability scan was attempted but timed out reaching PyPI; no clean vulnerability-scan claim is made.
- Windows CI contains Python 3.9/3.12/3.13 tests, native collector/report smoke
  checks, PSScriptAnalyzer and an isolated WSL configuration merge/backup/WhatIf test.

Native Windows and WSL hardware collection, enterprise policies, Task Scheduler,
installation/reinstallation, service and power changes require Windows validation.
This Linux session does not execute those system changes. A passing Python suite
alone does not establish correctness of Windows counter availability or OEM firmware.

## Reference checks

- [Flask changelog](https://flask.palletsprojects.com/en/stable/changes/)
- [Waitress security fixes in 3.0.1/3.0.2](https://docs.pylonsproject.org/projects/waitress/en/stable/index.html)
- [Microsoft WSL configuration sections](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)

## Remaining practical limits

Wi-Fi netsh and pnputil parsing remain dependent on available output and language;
missing/permission-gated counters are not equivalent to a healthy machine. Startup
events are not general interactive launch benchmarks. Temperature is an ACPI zone,
not a verified CPU core sensor. Version currency requires vendor confirmation.
Anonymization is best-effort for known fields; local history remains private raw data.
