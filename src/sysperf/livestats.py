"""Real-time headline metrics read in-process from Win32 APIs.

The PowerShell probe runs every SAMPLE_SECONDS (60 s) because spawning it more
often made the dashboard a top CPU consumer. That left the "live" headline
values up to ~80 s old (60 s sample + 20 s page poll). These few values are
read here directly via ctypes — microseconds, no process spawn — so the page
can show them in real time. Semantics match collect_win.ps1:
  cpu_pct       share of all cores busy since the previous call (GetSystemTimes)
  available_mb  free physical memory (Win32_OperatingSystem.FreePhysicalMemory)
  committed_mb  commit charge ('Committed Bytes' counter == CommitTotal)
  wifi          the Wi-Fi link right now (5.4.7): negotiated Rx/Tx rate and signal
                from the WLAN service, and real traffic (down/up Mbps) from the
                adapter's byte counters between two reads
READ-ONLY. Used for display only; scoring and the trend series still come from
the probe so their sample-count windows are unchanged.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from .config import PLATFORM

_lock = threading.Lock()
_prev: tuple[int, int, int] | None = None   # (idle, kernel, user) 100-ns ticks
_last_cpu: float | None = None


class _PerfInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD),
                ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD),
                ("ThreadCount", wintypes.DWORD)]


def _ft(f: wintypes.FILETIME) -> int:
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


def _cpu_pct() -> float | None:
    global _prev, _last_cpu
    idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                                 ctypes.byref(user)):
        return _last_cpu
    cur = (_ft(idle), _ft(kern), _ft(user))
    if _prev is not None:
        d_idle, d_kern, d_user = (c - p for c, p in zip(cur, _prev))
        total = d_kern + d_user              # kernel time includes idle time
        if total > 0:
            _last_cpu = round(max(0.0, min(100.0, 100.0 * (total - d_idle) / total)), 1)
    _prev = cur
    return _last_cpu


# ---- Wi-Fi (wlanapi + iphlpapi) ----------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _WlanIfInfo(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _GUID), ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", wintypes.DWORD)]


class _WlanIfList(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _WlanIfInfo * 1)]


class _Assoc(ctypes.Structure):
    _fields_ = [("uSSIDLength", wintypes.ULONG), ("ucSSID", ctypes.c_ubyte * 32),
                ("dot11BssType", wintypes.DWORD), ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11PhyType", wintypes.DWORD), ("uDot11PhyIndex", wintypes.ULONG),
                ("wlanSignalQuality", wintypes.ULONG), ("ulRxRate", wintypes.ULONG),
                ("ulTxRate", wintypes.ULONG)]


class _Conn(ctypes.Structure):
    _fields_ = [("isState", wintypes.DWORD), ("wlanConnectionMode", wintypes.DWORD),
                ("strProfileName", ctypes.c_wchar * 256), ("assoc", _Assoc),
                ("bSecurityEnabled", wintypes.BOOL), ("bOneXEnabled", wintypes.BOOL),
                ("dot11AuthAlgorithm", wintypes.DWORD), ("dot11CipherAlgorithm", wintypes.DWORD)]


_U64 = ctypes.c_ulonglong


class _IfRow2(ctypes.Structure):                       # MIB_IF_ROW2, 1352 bytes
    _fields_ = [("InterfaceLuid", _U64), ("InterfaceIndex", wintypes.ULONG),
                ("InterfaceGuid", _GUID), ("Alias", ctypes.c_wchar * 257),
                ("Description", ctypes.c_wchar * 257), ("PhysicalAddressLength", wintypes.ULONG),
                ("PhysicalAddress", ctypes.c_ubyte * 32),
                ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", wintypes.ULONG),
                ("Type", wintypes.ULONG), ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int),
                ("PhysicalMediumType", ctypes.c_int), ("AccessType", ctypes.c_int),
                ("DirectionType", ctypes.c_int), ("Flags", ctypes.c_ubyte),
                ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int),
                ("MediaConnectState", ctypes.c_int), ("NetworkGuid", _GUID),
                ("ConnectionType", ctypes.c_int), ("TransmitLinkSpeed", _U64),
                ("ReceiveLinkSpeed", _U64), ("InOctets", _U64), ("InUcastPkts", _U64),
                ("InNUcastPkts", _U64), ("InDiscards", _U64), ("InErrors", _U64),
                ("InUnknownProtos", _U64), ("InUcastOctets", _U64), ("InMulticastOctets", _U64),
                ("InBroadcastOctets", _U64), ("OutOctets", _U64), ("OutUcastPkts", _U64),
                ("OutNUcastPkts", _U64), ("OutDiscards", _U64), ("OutErrors", _U64),
                ("OutUcastOctets", _U64), ("OutMulticastOctets", _U64),
                ("OutBroadcastOctets", _U64), ("OutQLen", _U64)]


_wlan = {"handle": None}
_prev_octets: tuple | None = None                      # (t, in_octets, out_octets)


def throughput(prev: tuple | None, cur: tuple) -> tuple:
    """(down, up) Mbps between two (t, in_octets, out_octets) reads; None at first."""
    if not prev or cur[0] <= prev[0] or cur[1] < prev[1] or cur[2] < prev[2]:
        return None, None                              # first read, or counters reset
    dt = cur[0] - prev[0]
    return (round((cur[1] - prev[1]) * 8 / dt / 1e6, 2),
            round((cur[2] - prev[2]) * 8 / dt / 1e6, 2))


def _wifi() -> dict:                                   # pragma: no cover - Win32 only
    """The connected Wi-Fi interface right now; {} when none."""
    global _prev_octets
    wl = ctypes.windll.wlanapi
    if _wlan["handle"] is None:
        ver, h = wintypes.DWORD(), wintypes.HANDLE()
        if wl.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h)) != 0:
            return {}
        _wlan["handle"] = h
    h = _wlan["handle"]
    lst = ctypes.POINTER(_WlanIfList)()
    if wl.WlanEnumInterfaces(h, None, ctypes.byref(lst)) != 0:
        return {}
    try:
        n = lst.contents.dwNumberOfItems
        infos = ctypes.cast(ctypes.byref(lst.contents.InterfaceInfo),
                            ctypes.POINTER(_WlanIfInfo * n)).contents if n else []
        for info in infos:
            size, data = wintypes.DWORD(), ctypes.c_void_p()
            if wl.WlanQueryInterface(h, ctypes.byref(info.InterfaceGuid), 7, None,
                                     ctypes.byref(size), ctypes.byref(data), None) != 0:
                continue                               # 7 = current_connection; not connected
            try:
                a = ctypes.cast(data, ctypes.POINTER(_Conn)).contents.assoc
                out = {"rx_mbps": round(a.ulRxRate / 1000, 1),
                       "tx_mbps": round(a.ulTxRate / 1000, 1),
                       "signal": int(a.wlanSignalQuality)}
            finally:
                wl.WlanFreeMemory(data)
            row = _IfRow2()
            ip = ctypes.windll.iphlpapi
            # The LUID goes straight into the row's first field, as GetIfEntry2 expects.
            if ip.ConvertInterfaceGuidToLuid(ctypes.byref(info.InterfaceGuid),
                                             ctypes.byref(row, 0)) == 0 \
                    and ip.GetIfEntry2(ctypes.byref(row)) == 0:
                cur = (time.time(), row.InOctets, row.OutOctets)
                out["down_mbps"], out["up_mbps"] = throughput(_prev_octets, cur)
                _prev_octets = cur
            return out
        return {}
    finally:
        wl.WlanFreeMemory(lst)


def _wifi_safe() -> dict:
    """Wi-Fi values, or {} when there is no WLAN service, adapter or connection."""
    try:
        return _wifi()
    except (OSError, AttributeError, ValueError):
        return {}


def read() -> dict:
    """Current headline values, or {} off Windows / on API failure."""
    if PLATFORM != "windows":
        return {}
    with _lock:
        try:
            pi = _PerfInfo()
            pi.cb = ctypes.sizeof(pi)
            if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
                return {}
            mb = pi.PageSize / (1024 * 1024)
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return {
                "t": time.time(),
                "cpu_pct": _cpu_pct(),
                "available_mb": round(pi.PhysicalAvailable * mb),
                "total_ram_mb": round(pi.PhysicalTotal * mb),
                "committed_mb": round(pi.CommitTotal * mb),
                "commit_limit_mb": round(pi.CommitLimit * mb),
                "processes": int(pi.ProcessCount),
                "handles_total": int(pi.HandleCount),
                "uptime_h": round(ctypes.windll.kernel32.GetTickCount64() / 3.6e6, 2),
                "wifi": _wifi_safe(),
            }
        except (OSError, AttributeError):
            return {}
