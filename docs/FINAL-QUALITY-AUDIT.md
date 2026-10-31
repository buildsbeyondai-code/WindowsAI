# Final quality audit (SysPerf 5.5.0, 31 October 2026)

Scores are judged from evidence in this repository and on the one PC this was
built on (Windows 11 x64, 7.8 GB RAM, WSL 2). They are not the targets; where a
target is not met the gap is stated. A score above 9 needs evidence beyond
"tests pass here": independent hardware, users, or time in the field, which
this release does not yet have.

| Category | Target | Score | Evidence | What is missing |
|---|---:|---:|---|---|
| Innovation | 9.8 | 9.0 | Self-evolution with sandbox, A/B noise-aware benchmarks, protected boundaries, audit chain, auto rollback; self-upgrading components and dependencies; measured-effect recommendations | Not yet proven over months of field use |
| Features | 9.7 | 8.8 | Monitoring, RCA, fixes, maintenance, app updates, components, Store app, three user modes | GPU utilisation is not measured (driver/name only); no per-process disk/network I/O |
| AI intelligence | 9.7 | 8.0 | Recommendations ranked by share of gap × confidence with evidence and measured effect (`explain.py`); RCA causes; personal baselines | Effect predictions exist only after a fix has run here; no cross-PC learning; the LLM is used for code, not for advice |
| System monitoring | 9.8 | 8.8 | 98-key sample each minute, 3 s live CPU/RAM/Wi-Fi, 14 d raw + 400 d hourly history, values never invented | No GPU load, no per-process I/O |
| Performance | 9.8 | 8.8 | Service: 41 MB RSS, 0.24% CPU; every endpoint < 0.3 s; WSL probe off the request path; in-process live reads | Collector times out (fallback works) when the PC is saturated; 24 h soak not rerun for 5.5 |
| Optimization engine | 9.7 | 8.5 | Allow-list, risk classes (Safe/Moderate/Advanced/Experimental), restore points, before/after verification, ledger, reversibility shown | Several fixes are "manual" (Advanced) because Windows needs admin; no compatibility pre-check per fix beyond detection |
| Reliability | 9.8 | 8.7 | Supervisor restart/rollback, sleep/resume handling, atomic state, staging sweep, health alerts without false positives, 1,330 tests | A flaky benchmark test existed until 5.5; first restart request once lost on a broken process; no clean-machine run |
| Security | 9.8 | 9.0 | Threat model and evidence in `SECURITY-HARDENING-REPORT.md`; signed-download enforcement; CSRF/Host/CSP; DPAPI keys; hash-locked deps; `pip-audit` in CI | No external penetration test |
| Privacy | 9.8 | 9.3 | Local-only service, no telemetry, no account; only version feeds and the user's chosen model provider are contacted; SSID/BSSID masked | Store analytics are Microsoft's, outside the app |
| UX/UI | 9.7 | 8.4 | Light/dark themes with depth, Beginner/Advanced/Expert, live charts with axes, explained recommendations | One very long page per tab; no user testing |
| Accessibility | 9.5 | 8.0 | Automated a11y tests (labels, roles, contrast in light theme), keyboard-reachable controls | No screen-reader session with a real user |
| Windows compatibility | 9.7 | 7.5 | Windows 11 x64 verified daily on this PC; ARM64 package built on a Windows-on-ARM runner, and from 5.5 executed there (`--smoke`, see CI) | No Windows 10 test although the manifest allows 10.0.19041; no clean-VM install test; no multi-user test |
| Code quality | 9.7 | 8.8 | ruff clean, PSScriptAnalyzer clean, 100% line coverage gate, documented modules | `app.py` (≈1,800 lines) and `index.html` (≈6,400 lines) are large |
| Testing | 9.8 | 9.1 | 1,330+ tests on Windows, Linux CI matrix (py3.10/3.13), sandbox suite, e2e browser suite with 100% UI-surface coverage, 8 release gates, pre-push hook | No installation/upgrade test in CI; no long soak in CI |
| Documentation | 9.7 | 8.8 | README, user manual, architecture, decisions, learnings, security, privacy, audits, changelog per release | Troubleshooting is spread across docs |
| Installation | 9.7 | 8.3 | MSIX via Store and GitHub, scripted install, updates no longer fail first try (from 5.4.4 onwards) | No clean-machine install test |
| Release management | 9.8 | 9.0 | Tag → CI → x64/arm64 MSIX + SHA256SUMS on GitHub; checksums verified before Store bundling; changelog per version; Store submission via API | Store package is not signed by a public CA outside the Store (by design) |
| Maintainability | 9.7 | 8.7 | Layered modules, LEARNINGS fast path, test selection, hooks | The two large files above |

**Overall (mean): 8.7 / 10.** The target of 9.6 is not met. The largest gaps
are evidence that this environment cannot produce: Windows 10, clean-machine
and multi-user installs, ARM64 beyond a CI smoke run, a human screen-reader
pass, an external security test, and field time. The code-level work listed in
`MASTER-ENGINEERING-AUDIT.md` is done and tested.
