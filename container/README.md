# SysPerf — containerised (optional, WSL 2 + Docker)

New in 3.2.0. The dashboard web/API tier runs in an unprivileged container.
Everything that has to touch the Windows host leaves through a host-exec broker
on a unix socket, allowlisted and logged.

```
browser ──► 127.0.0.1:5056 ──► container (sysperf-dashboard)
                                   │
                 unix socket /run/sysperf-hostexec/sock
                                   ▼
                 sysperf-hostexec.service (WSL host, your user)
                                   │
                 powershell.exe ──► Windows host
```

This is an alternative to `install-wsl.sh`, not a replacement for the native
Windows install. Use it if you already run your local services as containers
under WSL and want SysPerf packaged the same way.

## Install

```bash
bash container/install-container.sh           # broker + image + container
bash container/install-container.sh --timer   # also the daily catalog refresh
```

It checks for WSL, Docker + Compose v2 and interop, seeds the state directory
(`$SYSPERF_DATA_DIR`, default `~/.local/share/sysperf` — the same one
`install-wsl.sh` uses) with `components.json` and `hw_watchlist.json`, renders
`broker/policy.json` and the systemd units from their `.in` templates, installs
the broker unit (the one `sudo` step), builds the image and starts it.

## Why a broker instead of a privileged container

The collector's only data source is `powershell.exe` executed on the Windows
host through WSL interop. A container cannot do that by default: its
`binfmt_misc` instance is empty, so there is no `WSLInterop` handler and the
`.exe` fails to exec.

Measured:

| Configuration | Result |
|---|---|
| no caps, `/mnt/c` mounted | `powershell.exe: not found` — no binfmt handler |
| bind-mount host `/proc/sys/fs/binfmt_misc` | refused by runc proc-safety check |
| `--cap-add SYS_ADMIN` | registration visible, then `UtilBindVsockAnyPort: socket failed` |
| `--cap-add SYS_ADMIN --security-opt seccomp=unconfined` | works |
| `--privileged` | works |

So running the collector *inside* the container costs `CAP_SYS_ADMIN` plus a
disabled seccomp profile — the default profile blocks the `AF_VSOCK` socket
interop needs. That is effectively host root, which forfeits most of the reason
to use a container.

The broker moves that one capability out of the container. Posture, verifiable
with `docker inspect sysperf-dashboard`:

- `Privileged: false`, `CapDrop: [ALL]`, `CapAdd: []`
- default seccomp profile active
- `ReadonlyRootfs: true`, `no-new-privileges`, `tmpfs /tmp` noexec/nosuid
- runs as your uid, not root
- 1 CPU, 512 MB, 256 pids — it must not make a pegged host worse
- published on `127.0.0.1` only

## Honest limits

- **No login.** Like every SysPerf install the API is unauthenticated (POSTs
  need the per-process CSRF token). It is published on loopback only. If you
  put a reverse proxy in front of it, the proxy must add authentication.
- **This does not survive `wsl --shutdown`.** If `dockerd` runs inside the WSL
  distro, the VM stopping takes the container with it. Containerising is
  packaging, not availability.
- **`-Command` passthrough is arbitrary execution on the Windows host.**
  `fixes.py` builds remediation command strings at runtime, so allowlisting
  command *content* would break the feature. Every call is logged to
  `broker/broker.log`. Set `allow_arbitrary_pwsh: false` in `broker/policy.json`
  for a read-only dashboard: sampling and version probes keep working, applying
  fixes stops.
- The broker runs as your user on the host. The gain is that host execution is
  one auditable process with an allowlist, instead of implicit in 8 call sites.
- **No browser gate.** The image has no Chromium, so the integrated test suite's
  `browser` gate reports *skip* in the container. Adding it means
  `playwright==1.58.0` plus Chromium's shared libraries (~700 MB image instead
  of ~194 MB).

## Things that had to be solved for parity

| Problem | Cause | Fix |
|---|---|---|
| Components panel empty | `components.json` resolved next to the code, absent from a read-only image | symlinked into `/data`, seeded by the installer |
| Hardware watchlist empty | same, for `hw_watchlist.json` | same |
| Test suite refused to run | the gate runner pointed at a read-only layer with nowhere to write | `PERFDASH_SELFTEST_ROOT=/dist` (the bind-mounted distribution) |
| `wsl --version` denied | the broker's systemd PATH lacks the `/mnt/c` interop entries | `program_paths` in `policy.json` |
| Frontend gate false pass | `node --check -` got empty stdin across the hop | the client forwards stdin |
| Dashboard lost the broker after a broker restart | `RuntimeDirectory` deleted the socket dir under the bind mount | `RuntimeDirectoryPreserve=yes` |
| `-File /app/...` not found by PowerShell | Windows cannot see into the container | broker rewrites `/app`, `/data`, `/dist` to host paths |

## Operations

```bash
cd container
bash build.sh                    # stage src/sysperf, pin base image digest, build
docker compose up -d
docker compose ps
docker compose logs -f

systemctl status sysperf-hostexec
journalctl -u sysperf-hostexec -f
tail -f broker/broker.log        # every host command executed

curl -s http://127.0.0.1:5056/api/health | python3 -m json.tool

python3 tools/probe_parity.py    # every GET endpoint, health signals only
python3 tools/probe_writes.py    # POST plumbing; runs real, non-disruptive actions
```

`tools/probe_writes.py` really executes the non-disruptive remediations it
exercises (for example `ipconfig /flushdns`) on the Windows host. Read it first.

## Switching to the systemd user service

State is shared, so no history is lost either way:

```bash
(cd container && docker compose down)
bash install-wsl.sh
```

## Layout

| Path | Purpose |
|---|---|
| `Dockerfile` | image; base pinned by digest via `build.sh` |
| `Dockerfile.resolved` | generated — the digest-pinned file actually built |
| `docker-compose.yml` | runtime: mounts, limits, hardening, healthcheck |
| `build.sh` | stages `../src/sysperf` into `app/`, resolves digest, builds |
| `install-container.sh` | one-shot install: seed state, render, broker, build, start |
| `broker/hostexec_broker.py` | host-side allowlisting exec broker |
| `broker/policy.json.in` | allowlist, path map, script roots, `-Command` switch |
| `broker/sysperf-hostexec.service.in` | systemd unit for the broker |
| `shims/hostexec_client.py` | in-container client, installed under 13 names |
| `systemd/sysperf-refresh-components.*` | optional daily catalog refresh |
| `tools/probe_*.py` | parity probes for the running container |

In the container `/app/sysperf` is the image's code, `/data` is your state
directory and `/dist` is this unpacked distribution. The broker rewrites all
three prefixes to host paths before executing, because PowerShell on the
Windows side cannot see into a container's filesystem — that rewrite is what
makes `-File /app/sysperf/collect_win.ps1` work at all.
