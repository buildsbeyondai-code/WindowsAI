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
running, because this window did not start it. When Windows stages an update of
the package, the window closes itself and reopens on the new version
(``selfupdate.py``), so the update does not fail with "apps need to be closed".

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
# 5.5.3: a busy port is often a SysPerf that is still starting (the scripted
# install and the Store app both start at sign-in). Give it this long to answer
# before moving aside to another port and running a second collector.
PORT_SETTLE_S = 30.0


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


def choose(env, probe=is_sysperf, free=port_free, pick=free_port,
           settle=None) -> tuple[int, bool]:
    """(port, attach): attach to a running SysPerf, else the port to serve on."""
    port = configured_port(env)
    if probe(port):
        return port, True
    if free(port):
        return port, False
    # Busy but not answering yet: most likely the other install starting up.
    if settle is None:
        settle = lambda p: wait_ready(p, probe, timeout=PORT_SETTLE_S)  # noqa: E731
    if settle(port):
        return port, True
    return pick(), False


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


# --- Title bar that follows the app's theme (5.5.3) -------------------------
# pywebview colours the native title bar from the *Windows* light/dark setting,
# so with Windows in light mode and SysPerf in dark mode the window kept a white
# caption bar (minimise / maximise / close) above a black page. The page now
# tells the shell which theme it shows (window.pywebview.api.set_theme) and the
# shell paints the caption through DWM: dark mode, caption, text and border
# colours (Windows 11, build 22000+, which is the package's MinVersion). The last
# theme is remembered so the next window opens in it without a white flash.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

TITLEBAR = {
    # Near-black with the blue tint of the dark theme's aurora, soft white text
    # and a hairline in the accent family, so the bar reads as part of the page.
    "dark": {"dark": 1, "caption": "#0b0f17", "text": "#e8edf5", "border": "#1e3a5f"},
    # The light theme's page grey, Apple ink, a quiet border.
    "light": {"dark": 0, "caption": "#f5f5f7", "text": "#1d1d1f", "border": "#d2d2d7"},
}


def colorref(hex_rgb: str) -> int:
    """'#rrggbb' -> a Win32 COLORREF (0x00BBGGRR)."""
    v = hex_rgb.lstrip("#")
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return (b << 16) | (g << 8) | r


def _dwm_set(hwnd: int, attr: int, value: int) -> bool:   # pragma: no cover - Win32
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.dwmapi.DwmSetWindowAttribute
    fn.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    fn.restype = ctypes.c_long
    v = ctypes.c_uint(value & 0xFFFFFFFF)
    return fn(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0


def paint_titlebar(hwnd: int, theme: str, set_attr=_dwm_set) -> bool:
    """Colour the window's caption for ``theme``; True when every call worked."""
    c = TITLEBAR.get(theme)
    if not hwnd or c is None:
        return False
    oks = [set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, c["dark"]),
           set_attr(hwnd, DWMWA_CAPTION_COLOR, colorref(c["caption"])),
           set_attr(hwnd, DWMWA_TEXT_COLOR, colorref(c["text"])),
           set_attr(hwnd, DWMWA_BORDER_COLOR, colorref(c["border"]))]
    return all(oks)


def window_hwnd(window) -> int | None:
    """The native handle of a pywebview (WinForms) window, if it has one yet."""
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        return None


class TitleBarApi:
    """Exposed to the page as ``window.pywebview.api``.

    Attributes are private: pywebview exposes public attributes of a js_api
    object to JavaScript, and the Window object must not be walked.
    """

    def __init__(self, state_path: str, paint=paint_titlebar, hwnd=window_hwnd):
        self._path, self._paint, self._hwnd = state_path, paint, hwnd
        self._window = None

    def bind(self, window) -> None:
        self._window = window

    def saved_theme(self) -> str | None:
        try:
            with open(self._path, encoding="utf-8") as f:
                t = json.load(f).get("theme")
        except (OSError, ValueError, AttributeError):
            return None
        return t if t in TITLEBAR else None

    def set_theme(self, theme: str) -> bool:
        if theme not in TITLEBAR:
            return False
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({"theme": theme}, f)
        except OSError:
            pass                                    # painting matters more than memory
        return self.apply(theme)

    def apply(self, theme: str | None = None) -> bool:
        theme = theme or self.saved_theme()
        if theme is None or self._window is None:
            return False
        return self._paint(self._hwnd(self._window), theme)


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
         released=wait_released, watch_update=None, settle=None) -> int:
    """Open the SysPerf window. Collaborators are injectable for testing."""
    env = os.environ if env is None else env
    if webview is None:                                 # pragma: no cover - real import
        import webview
    if "--relaunch" in (argv or []):
        # Started by the restart button: attaching to the instance that is about
        # to exit would leave a window onto nothing.
        released(configured_port(env), free)
    port, attach = choose(env, probe, free, pick, settle)
    if not attach:
        server(port, env)
    path = profile_dir(env)
    os.makedirs(path, exist_ok=True)
    titlebar = TitleBarApi(os.path.join(path, "titlebar.json"))
    kw = {"width": 1400, "height": 900, "min_size": (960, 640), "js_api": titlebar}
    if attach or ready(port, probe):
        window = webview.create_window(TITLE, f"http://127.0.0.1:{port}/#home", **kw)
    else:
        window = webview.create_window(TITLE, html=error_page(port), **kw)
    titlebar.bind(window)
    events = getattr(window, "events", None)
    if events is not None:
        # Paint the remembered theme as soon as the window exists (before the
        # page loads and reports its own), so a dark app never flashes white.
        events.shown += lambda: titlebar.apply()
    if watch_update is None:                            # pragma: no cover - real import
        from .selfupdate import watch as watch_update
    # Close before Windows swaps in a new version (else 0x80073D02), reopen after.
    watch_update(window.destroy)
    webview.start(private_mode=False, storage_path=path)
    return 0


if __name__ == "__main__":                              # pragma: no cover
    code = main(sys.argv[1:])
    # Background loops (collector, auto-improve) are daemon threads, but waitress
    # and PowerShell children can hold the interpreter open; the window closing
    # is the user's "quit".
    os._exit(code)
