#!/usr/bin/env bash
# =============================================================================
#  SysPerf — containerised installation (WSL 2 + Docker)
# =============================================================================
#  Runs the dashboard's web/API tier in an unprivileged container and installs
#  the host-exec broker that carries its PowerShell calls to Windows.
#
#    bash container/install-container.sh            # broker + image + start
#    bash container/install-container.sh --timer    # also the daily catalog refresh
#
#  Environment:
#    SYSPERF_DATA_DIR  state directory   (default ~/.local/share/sysperf,
#                                         the same one install-wsl.sh uses)
#    SYSPERF_PORT      loopback port     (default 5056)
#
#  Needs sudo once, to install the broker's system unit (it owns
#  /run/sysperf-hostexec). The container itself runs as you, with no
#  capabilities.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$(cd "$HERE/.." && pwd)"
DATA="${SYSPERF_DATA_DIR:-${HOME}/.local/share/sysperf}"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
WITH_TIMER=0
[ "${1:-}" = "--timer" ] && WITH_TIMER=1

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
die()  { printf '    \033[31m%s\033[0m\n' "$1" >&2; exit 1; }

render() {   # render <template> <output>
    sed -e "s|@SYSPERF_DIST@|${DIST}|g" -e "s|@SYSPERF_DATA@|${DATA}|g" \
        -e "s|@SYSPERF_USER@|${USER_NAME}|g" -e "s|@SYSPERF_GROUP@|${GROUP_NAME}|g" \
        "$1" > "$2"
}

step "Checking prerequisites"
grep -qi microsoft /proc/version 2>/dev/null \
    || die "Not running under WSL. The broker reaches Windows through WSL interop."
command -v docker >/dev/null || die "docker not found."
docker compose version >/dev/null 2>&1 || die "docker compose (v2) not found."
command -v systemctl >/dev/null || die "systemd is required for the broker unit."
ls /mnt/c/W*/System32/WindowsPowerShell/v1.0/powershell.exe >/dev/null 2>&1 \
    || die "powershell.exe not reachable. Enable interop in /etc/wsl.conf."
ok "WSL, Docker, Compose and interop present"

step "Seeding state directory ${DATA}"
mkdir -p "${DATA}"
for f in components.json hw_watchlist.json; do
    if [ -f "${DATA}/$f" ]; then
        ok "Keeping existing $f"
    else
        cp "${DIST}/src/sysperf/$f" "${DATA}/$f"
        ok "Seeded $f"
    fi
done
touch "${DATA}/refresh_components.log"

step "Rendering broker policy and units"
render "$HERE/broker/policy.json.in" "$HERE/broker/policy.json"
render "$HERE/broker/sysperf-hostexec.service.in" "$HERE/broker/sysperf-hostexec.service"
render "$HERE/systemd/sysperf-refresh-components.service.in" \
       "$HERE/systemd/sysperf-refresh-components.service"
ok "policy.json and units written under $HERE"

step "Installing the host-exec broker (sudo)"
sudo install -m 0644 "$HERE/broker/sysperf-hostexec.service" \
    /etc/systemd/system/sysperf-hostexec.service
if [ "$WITH_TIMER" -eq 1 ]; then
    sudo install -m 0644 "$HERE/systemd/sysperf-refresh-components.service" \
        /etc/systemd/system/sysperf-refresh-components.service
    sudo install -m 0644 "$HERE/systemd/sysperf-refresh-components.timer" \
        /etc/systemd/system/sysperf-refresh-components.timer
fi
sudo systemctl daemon-reload
sudo systemctl enable --now sysperf-hostexec.service
[ "$WITH_TIMER" -eq 1 ] && sudo systemctl enable --now sysperf-refresh-components.timer
for _ in 1 2 3 4 5; do [ -S /run/sysperf-hostexec/sock ] && break; sleep 1; done
[ -S /run/sysperf-hostexec/sock ] || die "broker socket did not appear; see journalctl -u sysperf-hostexec"
ok "Broker listening on /run/sysperf-hostexec/sock"

step "Building the image"
bash "$HERE/build.sh"

step "Starting the container"
( cd "$HERE" && SYSPERF_DATA_DIR="$DATA" SYSPERF_DIST="$DIST" docker compose up -d )
ok "Started"

cat <<EOF

  SysPerf installed (container mode).

  Dashboard   http://127.0.0.1:${SYSPERF_PORT:-5056}/   (loopback only, no login)
  Health      curl -s http://127.0.0.1:${SYSPERF_PORT:-5056}/api/health
  Logs        (cd $HERE && docker compose logs -f)
  Broker log  $HERE/broker/broker.log   (every host command executed)
  State       $DATA

  Stop        (cd $HERE && docker compose down)
  Uninstall   (cd $HERE && docker compose down) && \\
              sudo systemctl disable --now sysperf-hostexec && \\
              sudo rm /etc/systemd/system/sysperf-hostexec.service

EOF
