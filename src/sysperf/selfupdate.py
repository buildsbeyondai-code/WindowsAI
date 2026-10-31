"""Close the Store app before Windows updates it, then reopen it on the new version.

Windows will not replace a package while one of its processes runs: the
Store's first attempt fails with 0x80073D02 ("the following apps need to be
closed") and only a retry, which force-closes the app, succeeds. Store
auto-updates never force it, so a dashboard left open stayed on its old
version (5.0.0 to 5.4.2: every update failed once on this PC).

Windows stages the new version about ten seconds before it swaps packages,
and logs that to the AppXDeploymentServer/Operational channel, which a
standard user can read. The desktop window subscribes to that channel; when it
sees its own package at a newer version it starts a relauncher outside the
package (through WMI, so the relauncher is not one of the package's
processes), and exits. The relauncher waits until the new version is
registered and opens it.
"""
from __future__ import annotations

import base64
import ctypes
import os
import re
import subprocess
import threading

CHANNEL = "Microsoft-Windows-AppXDeploymentServer/Operational"
# 603 started, 607 de-queued, 10002 resiliency file: all name the target package.
QUERY = "*[System[(EventID=603 or EventID=607 or EventID=10002)]]"
APP_ID = "SysPerf"
RELAUNCH_WAIT_S = 300


def _key(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


def parse_full_name(full: str) -> dict | None:
    """Name_Version_Arch_ResourceId_PublisherId -> its parts, or None."""
    m = re.fullmatch(r"([^_]+)_(\d+\.\d+\.\d+\.\d+)_([^_]*)_([^_]*)_([a-z0-9]+)", full or "")
    if not m:
        return None
    name, version, _, _, pub = m.groups()
    return {"name": name, "version": version, "family": f"{name}_{pub}"}


def newer_version(event_xml: str, me: dict) -> str | None:
    """The newer version of this package an AppX deployment event is about."""
    best = None
    for v in re.findall(rf"{re.escape(me['name'])}_(\d+\.\d+\.\d+\.\d+)_", event_xml or ""):
        if _key(v) > _key(me["version"]) and (best is None or _key(v) > _key(best)):
            best = v
    return best


def relaunch_script(me: dict, version: str, wait_s: int = RELAUNCH_WAIT_S) -> str:
    """PowerShell that waits for ``version`` to be registered, then opens it."""
    return (f"$end=(Get-Date).AddSeconds({int(wait_s)}); while((Get-Date) -lt $end){{"
            f"$p=Get-AppxPackage -Name '{me['name']}'; "
            f"if($p -and [version]$p.Version -ge [version]'{version}'){{"
            f"Start-Process 'shell:AppsFolder\\{me['family']}!{APP_ID}'; exit}}; "
            "Start-Sleep -Seconds 2}")


def encoded(script: str) -> str:
    """PowerShell -EncodedCommand form: no quoting to get wrong."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def start_relauncher(me: dict, version: str, run=subprocess.run) -> bool:
    """Start the relauncher through WMI, so it is not a process of the package."""
    cmd = ("powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand "
           + encoded(relaunch_script(me, version)))
    create = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{CommandLine = '{cmd}'}}; exit [int]$r.ReturnValue")
    try:
        r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 encoded(create)], capture_output=True, timeout=30, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def on_event(event_xml: str, me: dict, quit_app, relaunch=start_relauncher,
             state: dict | None = None) -> bool:
    """Handle one deployment event; True when the app is closing for an update."""
    state = {} if state is None else state
    v = newer_version(event_xml, me)
    if not v or state.get("closing"):
        return False
    state["closing"] = v
    relaunch(me, v)               # best effort: without it the user reopens the app
    quit_app()
    return True


def current_package() -> dict | None:                  # pragma: no cover - Windows API
    """This process's package, or None when it is not running from one."""
    if os.name != "nt":
        return None
    k32 = ctypes.WinDLL("kernel32")
    n = ctypes.c_uint32(0)
    if k32.GetCurrentPackageFullName(ctypes.byref(n), None) != 122:   # INSUFFICIENT_BUFFER
        return None                                    # 15700: not packaged
    buf = ctypes.create_unicode_buffer(n.value)
    if k32.GetCurrentPackageFullName(ctypes.byref(n), buf) != 0:
        return None
    return parse_full_name(buf.value)


def subscribe(handler, channel: str = CHANNEL, query: str = QUERY):  # pragma: no cover
    """Call ``handler(xml)`` for each future event on ``channel``; the handle or None."""
    wevt = ctypes.WinDLL("wevtapi")
    cb_type = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p,
                                 ctypes.c_void_p)

    def render(event) -> str:
        used, props = ctypes.c_uint32(0), ctypes.c_uint32(0)
        wevt.EvtRender(None, ctypes.c_void_p(event), 1, 0, None, ctypes.byref(used),
                       ctypes.byref(props))
        buf = ctypes.create_unicode_buffer(used.value // 2 + 1)
        if not wevt.EvtRender(None, ctypes.c_void_p(event), 1, used, buf, ctypes.byref(used),
                              ctypes.byref(props)):
            return ""
        return buf.value

    def callback(action, _context, event):
        if action == 1:                               # EvtSubscribeActionDeliver
            try:
                handler(render(event))
            except Exception:                         # a bad event must not end the watch
                pass
        return 0

    cb = cb_type(callback)
    wevt.EvtSubscribe.restype = ctypes.c_void_p
    h = wevt.EvtSubscribe(None, None, channel, query, None, None, cb, 1)  # future events
    if not h:
        return None
    subscribe._keep = (cb, h)                          # the callback must outlive this frame
    return h


def watch(quit_app, package=current_package, sub=subscribe,
          relaunch=start_relauncher) -> bool:
    """Start watching for an update of this package; False when not packaged."""
    me = package()
    if not me:
        return False
    state: dict = {}
    lock = threading.Lock()

    def handler(xml: str) -> None:
        with lock:
            on_event(xml, me, quit_app, relaunch, state)
    return sub(handler) is not None
