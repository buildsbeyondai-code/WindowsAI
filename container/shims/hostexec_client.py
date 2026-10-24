#!/usr/bin/env python3
"""
Container-side client for the SysPerf host-exec broker.

Installed in the image under several names and dispatched on argv[0], so the
application needs no knowledge of the broker at all:

    /usr/local/bin/powershell.exe   <- SYSPERF_PS_EXE points here
    /usr/local/bin/wslpath          <- platform_paths.to_host_path finds it
    /usr/local/bin/docker           <- collector's container count
    /usr/local/bin/hostexec         <- explicit prefix form: hostexec uname -r

Exit codes follow the broker: 124 timeout, 126 refused by policy, 127 not
found on the host. A broker that is not running exits 127 with a message on
stderr, which the callers already treat as "probe unavailable" rather than
crashing.
"""
from __future__ import annotations

import json
import os
import socket
import sys

SOCKET_PATH = os.environ.get("SYSPERF_HOSTEXEC_SOCK", "/run/sysperf-hostexec/sock")
DEFAULT_TIMEOUT = float(os.environ.get("SYSPERF_HOSTEXEC_TIMEOUT", "300"))


def _read_stdin() -> str | None:
    """Forward piped stdin, or None when there is none.

    The test suite's frontend gate runs ``node --check -`` with the page's
    inline script on stdin. Without this the gate would syntax-check an empty
    string and report a false pass, which is worse than failing.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        data = sys.stdin.read()
    except (OSError, ValueError):
        return None
    return data or None


def main(argv: list[str]) -> int:
    name = os.path.basename(argv[0])
    if name in ("hostexec", "hostexec-client", "hostexec_client.py"):
        request_argv = argv[1:]
        if not request_argv:
            print("usage: hostexec <program> [args...]", file=sys.stderr)
            return 2
    else:
        # Invoked under the shimmed program's own name: forward it unchanged and
        # let the broker's allowlist decide.
        request_argv = [name] + argv[1:]

    payload = json.dumps({"argv": request_argv,
                          "timeout": DEFAULT_TIMEOUT,
                          "cwd": None,
                          "stdin": _read_stdin()}) + "\n"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(DEFAULT_TIMEOUT + 30)
        sock.connect(SOCKET_PATH)
    except OSError as exc:
        print(f"hostexec: broker unavailable at {SOCKET_PATH}: {exc}",
              file=sys.stderr)
        return 127

    try:
        with sock:
            sock.sendall(payload.encode())
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunks[-1].endswith(b"\n"):
                    break
            raw = b"".join(chunks)
    except OSError as exc:
        print(f"hostexec: broker call failed: {exc}", file=sys.stderr)
        return 127

    if not raw.strip():
        print("hostexec: empty reply from broker", file=sys.stderr)
        return 127
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        print(f"hostexec: malformed reply: {exc}", file=sys.stderr)
        return 127

    sys.stdout.write(reply.get("stdout") or "")
    sys.stderr.write(reply.get("stderr") or "")
    if reply.get("error"):
        print(f"hostexec: {reply['error']}", file=sys.stderr)
    return int(reply.get("rc", 127))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
