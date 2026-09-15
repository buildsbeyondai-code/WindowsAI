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
  third-party resources.
- **No injection surface.** The collector script path is fixed in code; no HTTP
  input reaches a shell or a query string.
- **No elevation.** Installation and operation run as a standard user.

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
