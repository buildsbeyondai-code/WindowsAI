# Audit remediation progress (branch `impl/5.5.x-audit-remediation`)

Resume here after any interruption. Worktree: `%LOCALAPPDATA%\SysPerf\wt-audit`.
The live install runs from `%LOCALAPPDATA%\SysPerf\app` (daily-use PC): never edit,
restart or test against it. No release tags, no publishing workflows, no Store.

## Baseline (decided 2026-11-01)

- Remote `main` = 90357aa (5.5.0 code 83d718f + your two audit documents).
- Local, unpushed: 6aeb67c "5.5.1" (ruff 0.17.1, CI failing-test annotations, smoke
  step quoting/timeout, benchmark-independent apply tests). Its tag was never
  pushed and was deleted locally to rule out an accidental release.
- Branch = 6aeb67c + d0bd0bf (your documents, taken from 90357aa). The cherry-pick
  reported "empty", so the two files were checked out from 90357aa and committed
  with the original message and author.

## Checklist

| # | Item | State | Evidence |
|---|---|---|---|
| 0 | Reproduce each audit finding on this baseline | done | F1 `--once` fresh dir: rc 0, `{}` (Windows, worktree src). F2 parser accepted `NotTrusted CN=Unrelated signed Company` and `HashMismatch\nsigned`. F3 marker write error: HEAD advanced, no marker, rollback 'nothing applied'. F3b `git_rollback` logs dependency failure and returns. F4 all planned ids credited with the batch delta (code). F5 verdict [] for empty rows. All reproduced. |
| 1 | `--once` takes a fresh, bounded sample; machine-readable failure; nonzero exit | done (local + native source run) | Windows, worktree src: fresh and stale data dirs both rc 0, `_t` 6-7 s after start, 48 keys, no PowerShell left running. Tests: `test_once_*`, `test_probe_once_*`. Frozen x64/ARM64: pending hosted run (item 5). |
| 1b | Packaging workflow runs on entry-point/collector changes | done | PR paths now include cli, collector, collect_win.ps1, desktop, selfupdate, config, platform_paths, __main__; `impl/**` branch pushes run build + smoke; release job still tag-only (checked in parsed YAML). |
| 2 | Structured signature verdict, exact signer identity, rotation policy | done (parser + OS) | Parser: 12 rejection cases incl. `NotTrusted CN=Unrelated signed Company`, substring subject, CN/O mismatch, malformed, verifier missing (`test_signature_verdict_*`). OS (Windows, real Authenticode): git-bash.exe as Johannes Schindelin accepted; python.exe as PSF accepted; wrong publisher refused; one-byte-tampered copy refused (HashMismatch); unsigned refused. Disposable test-certificate run: BLOCKED (needs admin to trust a test root). |
| 2b | Whole-package integrity (NuGet hash, PowerShell ZIP digest) | done | NuGet nupkg pinned to catalog SHA-512; PowerShell ZIP/MSIX and PortableGit pinned to GitHub sha256 digests, fail closed without one. Real network: genuine python 3.15.2 nupkg accepted, 3.15.1 against 3.15.2's hash refused. Observation: PowerShell 7.6.9 publishes no MSIX on GitHub, so the Pwsh upgrader's MSIX apply cannot succeed (now fails closed: 'no release asset'). |
| 3 | Apply/rollback transaction: durable intent, recovery, dependency failure surfaced | done (local, real git repos) | Intent marker before any change; phases intent/merged/applied/rollback_incomplete; `git_rollback` idempotent and raises RecoveryIncomplete; start-up `recover_apply`; lock serialises apply/rollback/recovery; moved HEAD refused; confirm refused while unfinished; UI states. Fault injection: marker write fails before merge (nothing moved), after merge (rolled back at start-up), dependency install fails then restore fails (incomplete, retried to completion), rollback restore fails (kept, retried), moved HEAD, failed merge, concurrent request. 206 related tests pass. Store build: frozen, no self-evolution apply path (source checkout only). |
| 4 | Per-action attribution; group observations; EMA labelled; pp vs relative | todo | |
| 6 | Soak verdict: coverage, missing data, gaps | todo | |
| 5 | Hosted Windows candidate run (branch push, no tag) | todo | |
| 7 | Release-evidence report and five-category reassessment | todo | |
