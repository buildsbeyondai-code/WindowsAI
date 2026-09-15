"""
Configuration: defaults, user overrides, and per-machine paths.

Nothing here is specific to one computer. Every threshold that used to be a
hand-tuned constant is either

  * derived from the machine itself (see :mod:`sysperf.profile`), or
  * exposed as a key in ``sysperf.config.json`` so the user can override it.

Load order (later wins)::

    built-in DEFAULTS  ->  <data-dir>/sysperf.config.json  ->  SYSPERF_* env vars

The module has no runtime side effects beyond reading that JSON file, so it is
safe to import from anywhere.
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import secrets
from pathlib import Path
from typing import Any

from .platform_win import is_windows

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
SCRIPTS_DIR = PACKAGE_DIR / "scripts"

APP_NAME = "SysPerf"
APP_TITLE = "System Performance Agent"
VERSION = "2.1.1"

# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def default_data_dir() -> Path:
    """Per-user writable directory for state (history, baseline, config)."""
    override = os.environ.get("SYSPERF_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "sysperf"


DATA_DIR = default_data_dir()
CONFIG_FILE = Path(os.environ.get("SYSPERF_CONFIG") or (DATA_DIR / "sysperf.config.json"))
HISTORY_FILE = DATA_DIR / "history.jsonl"
BASELINE_FILE = DATA_DIR / "baseline.json"
ADAPTIVE_FILE = DATA_DIR / "adaptive_state.json"
WATCHLIST_FILE = DATA_DIR / "hw_watchlist.json"
COLLECT_SCRIPT = SCRIPTS_DIR / "collect_win.ps1"

# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #

DEFAULTS: dict[str, Any] = {
    # --- server -----------------------------------------------------------
    # Loopback by default: the API is unauthenticated read-only telemetry.
    # Setting a non-loopback host forces token auth on (see load_config).
    "host": "127.0.0.1",
    "port": 5056,
    # Optional shared secret. Empty = no auth (only allowed on loopback).
    # Sent as "X-SysPerf-Token: <token>" or "?token=<token>".
    "auth_token": "",
    "open_browser": False,

    # --- sampling ---------------------------------------------------------
    "sample_seconds": 20,        # fast-tier cadence
    "full_every": 15,            # gather slow metrics every Nth sample
    "snapshot_ttl": 3.0,         # serve a cached API snapshot for this long
    "max_samples": 720,          # in-memory ring (720 x 20s = 4 h)
    "history_max_bytes": 12_000_000,
    "collector_timeout": 120,

    # --- scoring ----------------------------------------------------------
    # Fluctuating counts are scored on a rolling median so a momentary spike
    # cannot collapse the score. 1 disables smoothing.
    "score_smooth_window": 15,
    "smooth_window_overrides": {"net_remote": 180},
    # A metric within this percentage of its baseline counts as unchanged rather
    # than regressed. Without a dead-band, every criterion reads "regress" on the
    # first day, when the baseline *is* the current value and any upward tick of
    # noise crosses it.
    "regress_band_pct": 2.0,

    # --- target derivation (fractions of the machine's own capacity) -------
    # Used once, at baseline capture, to turn "this machine's starting state"
    # into achievable goals. See sysperf.profile.derive_targets.
    "targets": {
        "commit_of_ram": 0.75,       # commit charge <= 75% of physical RAM
        "commit_improve": 0.85,      # ...and at least 15% better than baseline
        "compression_of_ram": 0.05,  # Memory Compression <= 5% of RAM
        "available_of_ram": 0.30,    # >= 30% of RAM available
        "available_improve": 1.25,
        "handles_improve": 0.85,
        "disk_free_fraction": 0.15,  # >= 15% of the system volume free
        "startup_apps_max": 10,      # absolute ceiling for auto-start entries
        "startup_apps_improve": 0.55,
        "net_remote_floor": 25,      # OS/AV/sync connections you cannot remove
        "net_remote_improve": 0.70,
        "browser_extra_headroom": 4,  # slack above the measured browser floor
        "wsl_cap_of_ram": 0.35,      # fallback vmmem ceiling when no .wslconfig
        # responsiveness / thermal / startup-launch (all lower-is-better):
        "cpu_improve": 0.75,         # CPU utilisation & run-queue: aim 25% lower
        "disk_active_improve": 0.70,  # disk active-time: aim 30% lower
        "temp_improve": 0.92,        # CPU temperature: aim ~8% cooler
        "launch_improve": 0.60,      # boot & app-launch times: aim 40% faster
    },

    # --- common-fix engine -------------------------------------------------
    # The dashboard is read-only by default. When enabled, detected recurring
    # issues can be remediated via POST /api/fix — but only reversible,
    # non-disruptive fixes ("safe"/"caution") ever execute, gated by a CSRF
    # token; disruptive/session-killing ones are only ever described. Detection
    # and dry-run preview work regardless of this flag.
    "fixes": {
        "enabled": False,
    },

    # --- trend estimator (WSL working set) ---------------------------------
    "trend": {
        "min_samples": 6,
        "min_span_seconds": 600,
        "window_seconds": 3600,
        "gap_seconds": 120,
    },

    # --- adaptive (personal-best) layer -----------------------------------
    "adaptive": {
        "enabled": True,
        "stretch": 0.05,        # stretch goal 5% beyond your personal best
        "ema_alpha": 0.1,
        "min_day_samples": 3,
        "learn_samples": 20,
    },

    # --- WebView2 / browser ownership -------------------------------------
    # Host applications whose embedded-browser processes you cannot close, and
    # those that are simply in use. Anything else counts as discretionary.
    # Names are matched case-insensitively against the host process name.
    "browser_hosts": {
        "mandatory": {
            "OneDrive": "File sync (Windows-integrated)",
            "SearchHost": "Windows Start / Search",
            "ShellExperienceHost": "Windows shell",
            "StartMenuExperienceHost": "Windows Start menu",
            "Widgets": "Windows widgets",
            "WidgetService": "Windows widgets",
            "Copilot": "Windows Copilot",
            "LockApp": "Windows lock screen",
            "TextInputHost": "Windows input / IME",
            # Common enterprise VPN / endpoint clients embed a browser view and
            # are policy-managed, so their processes are not user-closable. Add
            # or remove entries here to match your own environment.
            "PanGPA": "Enterprise VPN client",
            "Cisco Secure Client": "Enterprise VPN client",
            "vpnui": "Enterprise VPN client",
            "FortiClient": "Enterprise VPN client",
            "CompanyPortal": "Device-management portal",
        },
        "in_use": {
            "ms-teams": "Microsoft Teams",
            "Teams": "Microsoft Teams",
            "OUTLOOK": "Microsoft Outlook",
            "olk": "Microsoft Outlook (new)",
            "Code": "Visual Studio Code",
            "slack": "Slack",
            "Notion": "Notion",
            "zoom": "Zoom",
        },
    },

    # --- advisor -----------------------------------------------------------
    "advisor": {
        "enabled": True,
        "stale_driver_years": 4,
        "reboot_after_hours": 168,      # suggest a restart after a week of uptime
        "max_recommendations": 20,
    },

    # --- privacy -----------------------------------------------------------
    # Metrics never leave the machine. When true, process/host names are
    # replaced by stable hashes in the API payload so the dashboard can be
    # screen-shared without exposing application names.
    "anonymize": False,
}

# Metrics carried forward between full samples (gathered on the slow tier).
CARRY_FORWARD_KEYS: tuple[str, ...] = (
    "services_running", "services_total",
    "startup_apps", "startup_apps_total", "startup_list",
    "net_remote", "net_loopback", "net_established", "net_listen", "net_tcp_total",
    "hw_problem_devices", "hw_problem_list",
    "hw_disks", "hw_disks_unhealthy", "hw_battery",
    "hw_pointing", "hw_touchpad",
    "drv_total", "drv_unsigned", "drv_stale", "drv_stale_list",
    "drv_problem", "drv_problem_list",
    "net_adapters", "net_category", "net_ipv4_conn", "net_ipv6_conn", "wifi_scan",
    "gpu_name", "gpu_driver", "gpu_driver_date",
    "net_driver_name", "net_driver_ver", "net_driver_date", "net_driver_provider",
    "ps_version", "pwsh_version", "edge_version", "webview2_version",
    "bios_version", "bios_date",
    "power_plan", "power_plan_guid", "pagefile_current_mb", "pagefile_mb", "pagefile_peak_mb", "pagefile_automatic",
    "wsl_installed", "wsl_config_present", "wsl_cap_mb", "wsl_autoreclaim",
    "os_caption", "os_build", "os_version", "wsl_cpus_cfg", "cpu_name", "cpu_cores", "cpu_threads",
    "machine_model", "machine_vendor", "hostname", "defender_realtime",
    "bitlocker_on", "restart_pending",
    # startup & launch latency + CPU temperature (slow-tier, carried between)
    "boot_time_s", "wsl_start_s", "explorer_launch_s", "outlook_launch_s",
    "word_launch_s", "cpu_temp_c",
)

# Series exposed to the frontend charts.
SERIES_KEYS: list[str] = [
    "committed_mb", "compression_mb", "available_mb", "vmmem_mb", "handles",
    "browser_procs", "edge_procs", "wv2_procs", "pool_paged_mb",
    "pool_nonpaged_mb", "cpu_pct", "cpu_queue", "processes", "c_free_gb",
    "net_remote", "net_loopback", "net_established", "net_listen", "startup_apps",
    "wifi_signal",
    "cpu_temp_c", "disk_active_pct", "battery_wear_pct",
    "boot_time_s", "wsl_start_s", "explorer_launch_s", "outlook_launch_s", "word_launch_s",
]


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _deep_merge(base: dict, over: dict) -> dict:
    """
    Recursively merge ``over`` onto ``base``, returning a fresh structure.

    The deep copy matters: without it a nested default (``targets``,
    ``advisor``, ``browser_hosts``) that the user did not override would be the
    *same object* as the one in DEFAULTS, so any later mutation of the resolved
    config would silently rewrite the module-level defaults for the rest of the
    process.
    """
    out = copy.deepcopy(base)
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


_ENV_KEYS = {
    "SYSPERF_HOST": ("host", str),
    "SYSPERF_PORT": ("port", int),
    "SYSPERF_TOKEN": ("auth_token", str),
    "SYSPERF_SAMPLE_SECONDS": ("sample_seconds", int),
    "SYSPERF_OPEN_BROWSER": ("open_browser", lambda v: str(v).lower() in ("1", "true", "yes")),
    "SYSPERF_ANONYMIZE": ("anonymize", lambda v: str(v).lower() in ("1", "true", "yes")),
}

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class Config(dict):
    """Resolved configuration. Dict-like, with attribute access for readability."""

    def __getattr__(self, item: str) -> Any:      # pragma: no cover - sugar
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    @property
    def is_loopback(self) -> bool:
        return str(self["host"]).lower() in LOOPBACK

    @property
    def requires_token(self) -> bool:
        """Non-loopback binds must be authenticated — the API has no other guard."""
        return not self.is_loopback

    def public_view(self) -> dict:
        """Config safe to expose over HTTP (secrets removed)."""
        out = {k: v for k, v in self.items() if k != "auth_token"}
        out["auth_enabled"] = bool(self["auth_token"])
        return out


def _validate(value: dict, defaults: dict) -> dict:
    """Reject wrong JSON types, including truthy strings used as opt-in flags."""
    out = copy.deepcopy(value)
    for key, default in defaults.items():
        val = out.get(key)
        if isinstance(default, dict):
            out[key] = _validate(val if isinstance(val, dict) else {}, default)
        elif isinstance(default, bool):
            out[key] = val if isinstance(val, bool) else default
        elif isinstance(default, (int, float)):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                val = default
            out[key] = type(default)(val)
        elif isinstance(default, str):
            out[key] = val if isinstance(val, str) else default
    return out


def load_config(path: Path | None = None, *, ensure_dirs: bool = True,
                overrides: dict | None = None) -> Config:
    """
    Resolve DEFAULTS + config file + environment into a :class:`Config`.

    Security: if the bind host is not loopback, an auth token is mandatory. One
    is generated and written back to the config file rather than silently
    exposing unauthenticated telemetry on the network.
    """
    path = Path(path) if path else CONFIG_FILE
    user: dict = {}
    if path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(user, dict):
                user = {}
        except (OSError, json.JSONDecodeError):
            user = {}

    cfg = _deep_merge(DEFAULTS, user)

    for env, (key, cast) in _ENV_KEYS.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        with contextlib.suppress(TypeError, ValueError):
            cfg[key] = cast(raw)

    cfg = _deep_merge(cfg, overrides or {})
    cfg = _validate(cfg, DEFAULTS)
    cfg["host"] = cfg["host"].strip() or "127.0.0.1"
    cfg["port"] = max(1, min(65535, cfg["port"]))
    for key, minimum in (("sample_seconds", 5), ("full_every", 1),
                         ("max_samples", 30), ("score_smooth_window", 1),
                         ("collector_timeout", 1), ("history_max_bytes", 1024)):
        cfg[key] = max(minimum, cfg[key])
    cfg["snapshot_ttl"] = max(0, cfg["snapshot_ttl"])
    cfg["adaptive"]["ema_alpha"] = min(1, max(0, cfg["adaptive"]["ema_alpha"]))
    for key in ("min_day_samples", "learn_samples"):
        cfg["adaptive"][key] = max(1, cfg["adaptive"][key])

    resolved = Config(cfg)
    if resolved.requires_token and not resolved["auth_token"]:
        resolved["auth_token"] = secrets.token_urlsafe(24)
        _persist_token(path, resolved["auth_token"], ensure_dirs=ensure_dirs)
    if ensure_dirs:
        with contextlib.suppress(OSError):
            DATA_DIR.mkdir(parents=True, exist_ok=True)
    return resolved


def _persist_token(path: Path, token: str, *, ensure_dirs: bool) -> None:
    """Write a generated token back to the config file so it survives restarts."""
    try:
        if ensure_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        current: dict = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8-sig")) or {}
            except (OSError, json.JSONDecodeError):
                current = {}
        if not isinstance(current, dict):
            current = {}
        current["auth_token"] = token
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        if not is_windows():
            os.chmod(path, 0o600)
    except OSError:
        pass
