#!/usr/bin/env python3
"""
SysPerf as a Windows desktop app — the entry point of the MSIX / Microsoft
Store package.

The dashboard is a local web page, so the app is a native window (WebView2, via
pywebview) onto that page rather than a second UI. Start-up order:

1. If a SysPerf is already answering on the configured port (the scheduled-task
   install, or a second launch of the app), the window attaches to it. Two
   collectors sampling the same machine would double the overhead and fight over
   the same remediations.
2. Otherwise the dashboard is started in-process on a background thread, on the
   configured port if it is free and on a free loopback port if something else
   holds it, and the window opens once ``/api/health`` answers.

Closing the window ends an in-process dashboard with it; an attached one keeps
running, because this window did not start it.

The WebView2 profile lives under the per-user data directory: a packaged app's
install folder (WindowsApps) is read-only, and WebView2 refuses to start when
its default profile next to the executable cannot be created.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request

TITLE = "SysPerf"
DEFAULT_PORT = 5056
START_TIMEOUT_S = 60.0


def is_sysperf(port: int, opener=urllib.request.urlopen, timeout: float = 2.0) -> bool:
    """Whether a SysPerf dashboard answers on 127.0.0.1:port.

    Checked by shape, not just status: another local server on 5056 must not be
    mistaken for the dashboard and shown in a window titled SysPerf.
    """
    try:
        with opener(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    return isinstance(body, dict) and "version" in body and "data_dir" in body


def port_free(port: int, sock_factory=socket.socket) -> bool:
    s = sock_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_port(sock_factory=socket.socket) -> int:
    """A port the OS reports free right now (bind to 0, read it back)."""
    s = sock_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def configured_port(env) -> int:
    try:
        return int(env.get("PERFDASH_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def choose(env, probe=is_sysperf, free=port_free, pick=free_port) -> tuple[int, bool]:
    """(port, attach): attach to a running SysPerf, else the port to serve on."""
    port = configured_port(env)
    if probe(port):
        return port, True
    return (port if free(port) else pick()), False


def start_server(port: int, env, cli_main=None, thread_cls=threading.Thread):
    """Run the dashboard on a daemon thread so it dies with the window."""
    if cli_main is None:                                # pragma: no cover - real import
        from .cli import main as cli_main
    # The window is the UI: the dashboard must not also open a browser tab.
    env["SYSPERF_NO_BROWSER"] = "1"
    t = thread_cls(target=cli_main, args=(["--port", str(port)],), name="sysperf-server",
                   daemon=True)
    t.start()
    return t


def wait_ready(port: int, probe=is_sysperf, clock=time.monotonic, sleep=time.sleep,
               timeout: float = START_TIMEOUT_S) -> bool:
    end = clock() + timeout
    while clock() < end:
        if probe(port):
            return True
        sleep(0.5)
    return False


def profile_dir(env) -> str:
    root = env.get("SYSPERF_DATA_DIR") or os.path.join(
        env.get("LOCALAPPDATA") or os.path.expanduser("~"), "SysPerf")
    return os.path.join(root, "webview")


def error_page(port: int) -> str:
    return ("<html><body style='font:15px Segoe UI,sans-serif;padding:32px'>"
            "<h2>SysPerf could not start</h2>"
            f"<p>The dashboard did not answer on 127.0.0.1:{port} within "
            f"{int(START_TIMEOUT_S)} seconds.</p><p>Close this window and open SysPerf "
            "again. If it keeps happening, see the log in the SysPerf data folder.</p>"
            "</body></html>")


def wait_released(port: int, free=port_free, clock=time.monotonic, sleep=time.sleep,
                  timeout: float = 20.0) -> bool:
    """After a restart: wait for the previous instance to let go of the port."""
    end = clock() + timeout
    while clock() < end:
        if free(port):
            return True
        sleep(0.25)
    return False


def main(argv=None, env=None, webview=None, probe=is_sysperf, free=port_free,
         pick=free_port, server=start_server, ready=wait_ready,
         released=wait_released) -> int:
    """Open the SysPerf window. Collaborators are injectable for testing."""
    env = os.environ if env is None else env
    if webview is None:                                 # pragma: no cover - real import
        import webview
    if "--relaunch" in (argv or []):
        # Started by the restart button: attaching to the instance that is about
        # to exit would leave a window onto nothing.
        released(configured_port(env), free)
    port, attach = choose(env, probe, free, pick)
    if not attach:
        server(port, env)
    kw = {"width": 1400, "height": 900, "min_size": (960, 640)}
    if attach or ready(port, probe):
        webview.create_window(TITLE, f"http://127.0.0.1:{port}/#home", **kw)
    else:
        webview.create_window(TITLE, html=error_page(port), **kw)
    path = profile_dir(env)
    os.makedirs(path, exist_ok=True)
    webview.start(private_mode=False, storage_path=path)
    return 0


if __name__ == "__main__":                              # pragma: no cover
    code = main(sys.argv[1:])
    # Background loops (collector, auto-improve) are daemon threads, but waitress
    # and PowerShell children can hold the interpreter open; the window closing
    # is the user's "quit".
    os._exit(code)
