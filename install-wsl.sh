#!/usr/bin/env bash
# =============================================================================
#  SysPerf — optional WSL 2 installation
# =============================================================================
#  SysPerf normally runs natively on Windows (install.ps1). This script is for
#  the alternative arrangement: Python runs inside a WSL distribution and reaches
#  Windows through interop.
#
#  Use it when
#    * you already run other local services in WSL and want one place for them, or
#    * you cannot install Python on the Windows side.
#
#  Trade-offs versus the native install
#    * every sample pays the WSL -> Windows interop cost (a second or two);
#    * WSL must be running for the dashboard to be up;
#    * in exchange you also get Linux-guest memory metrics in the WSL panel.
#
#  Installs to ~/.local/share/sysperf with a systemd user service. No sudo.
# =============================================================================
set -euo pipefail

APP_DIR="${HOME}/.local/share/sysperf/app"
VENV_DIR="${HOME}/.local/share/sysperf/venv"
DATA_DIR="${SYSPERF_DATA_DIR:-${HOME}/.local/share/sysperf}"
UNIT_DIR="${HOME}/.config/systemd/user"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SYSPERF_PORT:-5056}"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
die()  { printf '    \033[31m%s\033[0m\n' "$1" >&2; exit 1; }

step "Checking the runtime"
grep -qi microsoft /proc/version 2>/dev/null \
  || die "Not running under WSL. On native Windows use install.ps1 instead."
command -v python3 >/dev/null || die "python3 not found. Install it with your distro's package manager."
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' \
  || die "Python 3.10+ required, found ${PYV}."
python3 -c 'import venv' 2>/dev/null || die "The venv module is missing. Install python3-venv."
ok "WSL detected, Python ${PYV}"

step "Verifying Windows interop"
PS=$(ls /mnt/c/W*/System32/WindowsPowerShell/v1.0/powershell.exe 2>/dev/null | head -1 || true)
[ -n "${PS}" ] || die "powershell.exe not reachable. Enable interop in /etc/wsl.conf and ensure C: is mounted."
ok "PowerShell at ${PS}"

step "Installing to ${APP_DIR}"
mkdir -p "${APP_DIR}" "${DATA_DIR}"
cp -r "${SRC_DIR}/src" "${SRC_DIR}/requirements.txt" "${APP_DIR}/"
[ -f "${SRC_DIR}/README.md" ] && cp "${SRC_DIR}/README.md" "${APP_DIR}/"
ok "Files copied"

step "Creating the virtual environment"
[ -x "${VENV_DIR}/bin/python" ] || python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
ok "Dependencies installed"

step "Writing configuration"
CFG="${DATA_DIR}/sysperf.config.json"
if [ -f "${CFG}" ]; then
  ok "Keeping existing ${CFG}"
else
  cat > "${CFG}" <<JSON
{
  "host": "127.0.0.1",
  "port": ${PORT},
  "sample_seconds": 20
}
JSON
  ok "Created ${CFG} (loopback only)"
fi

step "Installing the systemd user service"
mkdir -p "${UNIT_DIR}"
cat > "${UNIT_DIR}/sysperf.service" <<UNIT
[Unit]
Description=SysPerf — read-only Windows performance dashboard
Documentation=https://github.com/buildsbeyondai-code/WindowsAI
After=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH=${APP_DIR}/src
Environment=SYSPERF_DATA_DIR=${DATA_DIR}
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/python -m sysperf
Restart=always
RestartSec=5

# The collector only reads counters; it needs no elevated capability.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=default.target
UNIT

if command -v systemctl >/dev/null && systemctl --user >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now sysperf.service
  ok "Service enabled: systemctl --user status sysperf"
else
  ok "systemd user session unavailable — start manually:"
  echo "      PYTHONPATH=${APP_DIR}/src SYSPERF_DATA_DIR=${DATA_DIR} ${VENV_DIR}/bin/python -m sysperf"
fi

cat <<EOF

  SysPerf installed (WSL mode).

  Dashboard   http://localhost:${PORT}/   (WSL localhost forwarding makes this
                                           work from the Windows browser too)
  Report      PYTHONPATH=${APP_DIR}/src ${VENV_DIR}/bin/python -m sysperf --report
  Config      ${CFG}
  Uninstall   systemctl --user disable --now sysperf; rm -rf ${HOME}/.local/share/sysperf \\
                ${UNIT_DIR}/sysperf.service

  SysPerf only reads. It never changes a setting on your behalf.

EOF
