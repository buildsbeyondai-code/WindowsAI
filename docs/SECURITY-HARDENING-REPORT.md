# Security hardening report (SysPerf 5.5.0, 31 October 2026)

Scope: the local service, the Store app, the self-evolution and upgrade paths,
and everything SysPerf downloads or runs. `SECURITY.md` is the policy; this is
the evidence.

## Threat model

| Asset | Threat | Entry point | Control | Status |
|---|---|---|---|---|
| The PC (system changes) | A web page drives the local API | Browser → 127.0.0.1:5056 | Host/Origin guard (DNS rebinding), per-process CSRF token on every POST, no CORS, loopback bind | Tested: `test_hostguard.py`, `test_security_doc.py`; live gate probes a foreign Host and cross-origin POSTs on every run |
| The page | Script injection | Values from the PC (process names, SSIDs) | Everything escaped (`esc`), CSP `default-src 'self'`, `frame-ancestors 'self'`, `Permissions-Policy`, COOP | Frontend gate checks escaping |
| The PC | A malicious or tampered download is run | Upgrader downloads | **5.5:** Authenticode signature valid and from the expected publisher before anything runs; GitHub assets match GitHub's SHA-256; PyPI wheels match PyPI's SHA-256; requirements hash-locked | Tested: `test_downloads_must_be_signed_by_the_expected_publisher`, `test_a_github_asset_must_match_its_published_sha256`; real check run against a PSF-signed installer (accepted), wrong publisher and unsigned file (refused) |
| The PC | An AI-written patch does harm | Model output | Repository content passed as data; schema validation; protected files (`codevolve.py`, `llm.py`, `evodb.py`, `optactions.py`, tests, CI, packaging, every `.ps1`/`.json`/`.toml`); static and security scan of diffs; runs only in the bubblewrap sandbox (no network, read-only); A/B benchmark; deploy is health-checked and rolled back | `test_codevolve*.py` (bad patches rejected per rule) |
| Self-evolution boundary | The AI weakens its own checks | A patch to the evolution engine or tests | Those files are protected; a patch touching them is rejected before it runs | Tested (protected-path cases) |
| Secrets | API key disclosure | Logs, API, disk | DPAPI-encrypted on Windows (0600 elsewhere); never returned, logged or echoed; log redaction | `test_llm*.py` |
| The PC | Silent destructive change | Fixes and upgrades | Allow-listed fixes with risk classes; autonomy levels; restore point before changes; reversible-only unattended; registry fix exports `.reg` backups first; nothing disables Defender, Firewall or other security features | `test_fixes.py`, `test_optactions_evodb.py`, `test_maintenance.py` |
| Integrity of history | Tampering with the record of changes | `sysperf.db` | Hash-chained audit log, verified on every status read | `verify_audit` tests; the live page shows "Audit chain intact" |

## Changes in 5.5.0

1. **Signed downloads only** (`upgrader.Host.verify_signed`): the Python
   installers and the NuGet interpreter (Python Software Foundation),
   PowerShell zip and MSIX (Microsoft Corporation), PortableGit (Johannes
   Schindelin), the WSL MSI (Microsoft Corporation, now through the same check).
   Before, only the WSL MSI was checked; the other six relied on HTTPS.
2. **GitHub digests**: `Host.github_asset` records the asset's published
   SHA-256 and `Host.download` refuses a file that does not match.
3. **Atomic consent storage**: tray auto-quit state is written temp + replace.

Earlier on 31 Oct: `Permissions-Policy` and `Cross-Origin-Opener-Policy`
headers (5.4.5).

## Checked and not vulnerable

- `shell="pwsh"` (17 bandit hits) is a field naming the shell, not
  `subprocess(shell=True)`; commands are fixed strings from the registry.
- f-string SQL (9 hits) interpolates column names from code; values are bound.
- No `0.0.0.0` bind; the Host guard compares against it.
- Winget ids are matched against `^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$` before use.

## Remaining risks

| Risk | Severity | Note |
|---|---|---|
| A publisher's signing key is compromised | Low | Outside SysPerf's control; the publisher check narrows it to that publisher |
| NuGet package hash not checked | Low | The interpreter inside is signature-checked before it runs |
| Admin approval for machine-wide installs relies on the UAC prompt | By design | SysPerf never stores or types an administrator password |
| No external penetration test | — | Reviews and automated tests only |
