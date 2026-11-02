"""One SysPerf per port: claim the listening socket before anything else starts.

Observed 2026-11-02 11:08 IST: at sign-in the Store app (5.4.2) and the scripted
install (5.5.2) started four seconds apart. Neither answered /api/health yet, so
the desktop shell's "attach to a running SysPerf" probe missed the race, and
waitress sets SO_REUSEADDR, which on Windows lets a second process bind a port
that is already listening. Both bound 127.0.0.1:5056. The result was two
collectors (two PowerShell probes, ~700 MB including the second WebView2
window), both writing the same data directory, and a browser that reached the
older build at random.

The fix: bind the port ourselves, exclusively (SO_EXCLUSIVEADDRUSE on Windows;
a plain bind elsewhere, where a second listener is refused anyway), BEFORE the
collector and the other loops start, and hand the bound socket to waitress. A
second instance gets "address in use", starts nothing, and exits with
ALREADY_SERVING_CODE; the supervisor treats that as standby, not as a crash.
"""
from __future__ import annotations

import socket

ALREADY_SERVING_CODE = 76         # exit code: another server already owns the port


def claim_port(host: str, port: int, *, windows: bool | None = None,
               sock_factory=socket.socket):
    """Return a bound socket that owns ``host:port`` exclusively, or None.

    None means another process already holds the port (or it cannot be bound).
    The socket is bound but not listening; waitress calls listen() itself.
    """
    if windows is None:
        windows = hasattr(socket, "SO_EXCLUSIVEADDRUSE")
    s = sock_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if windows:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        s.bind((host, port))
    except OSError:
        s.close()
        return None
    return s
