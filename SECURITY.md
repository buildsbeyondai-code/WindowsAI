# Security Policy

## Design posture

SysPerf is deliberately narrow, which removes most of the attack surface a tool
like this would otherwise have:

- **Read-only.** Every system call is a query. Nothing is written, started,
  stopped or reconfigured on the host.
- **Loopback by default.** The HTTP server binds `127.0.0.1`.
- **Token required off loopback.** A non-loopback bind causes SysPerf to generate
  a token, persist it to the config file, and require it on every request. It will
  not serve unauthenticated telemetry to a network.
- **No outbound traffic.** Nothing is uploaded and the front end loads no
  third-party resources. The one exception is opt-in: the scheduled
  `refresh_components.py` job queries public version endpoints (apt, PyPI,
  nodejs.org, GitHub) to refresh the upgrade catalog. It sends no telemetry.
- **Container mode (3.2.0+).** The optional container runs with no
  capabilities and a read-only root; host execution goes through an
  allowlisting broker that logs every command. Its `-Command` passthrough is
  arbitrary PowerShell on the host by design (remediations) and can be
  disabled in `container/broker/policy.json`.
- **No injection surface.** The collector script path is fixed in code; no HTTP
  input reaches a shell or a query string.
- **No elevation.** Installation and operation run as a standard user.

## 4.0 autonomy and self-evolution

- **Autonomy defaults to `observe`.** Only low-risk, reversible, individually
  enabled actions ever run unattended. Defender, the firewall, Windows Update,
  event logging, backup/VSS, BitLocker, UAC, recursive deletes and critical
  processes are refused at every level.
- **Model output is untrusted data.** It is schema-validated, and its edits may
  touch only eligible, unprotected files. A diff that adds subprocess, network,
  dynamic-code or security-sensitive constructs is rejected.
- **Generated code runs only in isolation.** That means bubblewrap or a
  `--network none` read-only container. With neither, validation is blocked.
- **Nothing is deployed automatically.** Candidates are review branches, and
  approval is recorded in a hash-chained (tamper-evident) audit log.
- **API keys never leave their file.** They come from the environment or a
  0600 key file, and are never logged or returned.

Details and stated limits are in `docs/AI_EVOLUTION.md` §4.

## What the API exposes

Treat `/api/metrics` as sensitive. It includes running process names, top memory
consumers, installed third-party drivers, enabled startup entries, connection
counts and the machine's hostname and model. That is a useful reconnaissance
picture, which is why the default bind is loopback.

Set `"anonymize": true` to replace application and device names with stable
hashes before sharing a screenshot or a payload in a bug report.

## Deployment guidance

| Scenario | Recommendation |
|---|---|
| Single desktop, default install | Nothing to do — loopback only. |
| Reachable from a trusted LAN | Keep the generated token; front it with a reverse proxy that terminates TLS. |
| Anything internet-facing | Not a supported configuration. There is no TLS, no rate limiting and no account model. |

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |

## Mutating surface

Telemetry is read-only. Exactly four endpoints can change anything, each requiring
the per-process CSRF token issued by `GET /api/fixes`:

- `POST /api/recalibrate` — rewrites SysPerf's own baseline file only.
- `POST /api/fix` — runs a reversible, non-disruptive fix. Requires
  `fixes.enabled`; `manual` and session-killing fixes are never executed.
- `POST /api/autoimprove/run` — runs one diagnose/remediate cycle under the same
  constraints as `/api/fix`.
- `POST /api/autoimprove/mode` — switches the autonomous loop between `off`,
  `observe` and `auto`.

The autonomous loop is limited further: `safe` risk only unless
`autoimprove.allow_caution` is set, at most two actions per cycle, a 30-minute
per-action cooldown, and no ability to edit targets, criteria or scoring.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Open a private [GitHub security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on the repository. Include:

- the version (`sysperf --version`) and Windows build;
- what an attacker gains, and what access they need to start with;
- a reproduction, ideally the smallest one that works.

Expect an acknowledgement within five working days and an assessment within ten.
Fixes for confirmed issues ship in a patch release, with credit unless you prefer
otherwise.

### In scope

Anything that lets a caller read data they should not, bypass the token check,
reach a shell through the collector, escalate privilege, or cause SysPerf to write
outside its own data directory.

### Out of scope

- The unauthenticated loopback endpoint on a default install. That is the
  documented design; an attacker who can bind to your loopback interface already
  has code execution as you.
- Absence of TLS on the local bind.
- Findings that require administrator rights you already hold.
