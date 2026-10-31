# Master engineering audit (SysPerf 5.4.9 → 5.5.0, 31 October 2026)

Method: read the code paths named below, measured the live install (Windows 11
x64, WSL 2, Python 3.15.2), ran the full suites, and checked each claim against
evidence. **Confirmed** = reproduced or read in the code with a failing case;
**risk** = plausible, not reproduced. Earlier findings of the day are in
`AUDIT-2026-10-31.md` (41 issues, 22 fixed in 5.4.5) and the 5.4.1–5.4.9 entries
of the CHANGELOG.

## 1. Architecture

One Python process (Flask on waitress, 127.0.0.1) serving one self-contained
page, 53 modules in layers with one-way dependencies (see `ARCHITECTURE.md`):

| Layer | What it does | Key modules |
|---|---|---|
| Measure | warm PowerShell collector each minute; Win32 reads every 3 s | `collector`, `pwsh_session`, `collect_win.ps1`, `livestats` |
| Understand | scoring vs this PC's own baseline, trends, root cause | `scoring`, `adaptive`, `machine_profile`, `trends`, `rca`, `explain` |
| Act | allow-listed reversible fixes, autonomy levels, verify and learn | `fixes`, `optactions`, `autoimprove`, `evolve` |
| Improve itself | sandboxed AI patches, component and dependency upgrades | `codevolve`, `upgrader`, `depupgrade`, `llm` |
| Remember | SQLite history, hash-chained audit, action ledger | `historydb`, `evodb`, `actions` |
| Operate | supervisor restart/rollback, health, self-test gates | `supervisor`, `health`, `selftest` |
| Ship | MSIX (x64, arm64), self-update handling, Store mode | `desktop`, `selfupdate`, `store_mode` |

## 2. Strengths (evidence)

- **Test discipline**: 1,256 test functions; 100% line coverage enforced on every
  module by an in-app gate; the suite runs on Windows, on Linux (CI), and inside
  the bubblewrap sandbox that gates self-evolution; a pre-push hook runs it all.
- **Nothing shown is invented**: values carry their source and state
  (measuring / stale / unavailable); tests check documented values against code.
- **Change control**: every fix is allow-listed, risk-classed and reversible or
  marked otherwise; autonomy levels gate unattended action; self-evolution has
  protected files, a sandbox, A/B benchmarks with noise thresholds, an audit
  chain, health-checked deploys and automatic rollback.
- **Local-only service**: Host/Origin guard, per-process CSRF token on all 28+
  POST routes, CSP, `Permissions-Policy`, COOP, no CORS, loopback bind.

## 3. Confirmed defects found in this audit (all fixed in 5.5.0)

| # | Area | Defect | Evidence | Fix |
|---|---|---|---|---|
| M1 | Security | Six of seven upgrader downloads were executed with no integrity check beyond HTTPS: Python installers, the NuGet interpreter, PowerShell zip and MSIX, PortableGit | `Host.download` was never passed a hash; only the WSL MSI was signature-checked | `Host.verify_signed` (Authenticode valid + expected publisher) on every executable before it runs; GitHub assets also checked against GitHub's published SHA-256 |
| M2 | Reliability | Tray auto-quit state (per-app consents) was written in place; a crash mid-write reset every consent | `trayquit._save` used `write_text` on the live file; every other state file uses temp + replace | Temp file + `replace` |
| M3 | Testing | `test_winner_is_not_kept_when_auto_retain_is_off` failed under load (the 5.4.9 gate run) | It needed the benchmark to show a win; under load the benchmark was inconclusive | The benchmark verdict is fixed in that test; it tests the switch |
| M4 | AI / UX | Recommendations did not state expected effect, risk class or how success is measured together | Findings had cause and confidence; fixes had risk; the measured-effect ledger was never shown next to them | `explain.py` + Auto-Improve "Recommendations" (section 5) |
| M5 | UX | One "Simple view" switch; no level between hiding everything and showing everything | Home had a single checkbox | Beginner / Advanced / Expert (section 6) |
| M6 | Release | The ARM64 package was built and its PE header checked, but never executed | `msix.yml` had no run step | `SysPerf.exe --smoke` takes one real sample on both runners |

## 4. Risks (not reproduced, documented)

| Risk | Why it is a risk | Mitigation / status |
|---|---|---|
| Windows 10 | The manifest allows 10.0.19041+; nothing was tested on Windows 10 | Unverified; see FINAL-QUALITY-AUDIT |
| Multi-user | One per-user data folder; a second account gets its own install of the scripted service | Not tested |
| WSL busy | The collector timed out (150–300 s) during an Ubuntu export and a Python install | One-shot fallback works; web requests no longer wait on WSL probes (5.4.5) |
| Paged pool | +42% over baseline on this PC; ExpressVPN (3 kernel drivers) and Intel XTU are loaded | A machine finding, not a SysPerf defect |
| Benchmark-sensitive tests | Other sandboxed A/B tests could share M3's weakness | Only M3 failed across ~15 full runs today |

## 5. AI recommendations (Phase 4)

Each finding now answers: what is wrong, the evidence (typical vs target, cause,
share of the gap, confidence), each fix, the **measured** effect of that fix on
this PC (runs, average change, wins/losses — or "not measured on this PC yet"),
its risk class (Safe / Moderate / Advanced / Experimental), whether it reverses,
and how success is measured (re-measured after the settle time). Ranked by
share of the gap × confidence. No number is estimated.

## 6. User modes (Phase 7)

Beginner: health, next steps, plain explanations; expert tabs hidden;
descriptions shown. Advanced: every tab. Expert: every tab with diagnostics and
per-process detail open. A 5.4 "Simple view" setting becomes Beginner.

## 7. Prioritised roadmap (not done in 5.5.0)

1. Run the release on a clean Windows 11 x64 VM and an ARM64 device (install,
   upgrade, uninstall) — needs hardware/VMs this environment does not have.
2. Decide Windows 10: test it, or raise `MinVersion` to Windows 11.
3. Audit the remaining sandboxed A/B tests for M3-style load sensitivity.
4. Keyboard-only and screen-reader pass of the new Recommendations card and
   mode selector (automated a11y tests pass; no human screen-reader run).
5. Multi-account test of the scripted install and the Store app side by side.
