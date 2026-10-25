"""Full-coverage tests for components.py."""
import json
import subprocess

import sysperf.components as components
from sysperf.components import _cmp, _months_old, _run, _ver, build_components


# ------------------------------------------------------------------- helpers --
def test_ver():
    assert _ver("v1.24.0 stuff") == "1.24.0"
    assert _ver("no version here") == ""
    assert _ver(None) == ""


def test_cmp():
    assert _cmp("1.0.0", "2.0.0") == -1
    assert _cmp("2.0.0", "1.0.0") == 1
    assert _cmp("1.2", "1.2.0") == 0
    assert _cmp("", "1.0") is None
    assert _cmp("1.0", "abc") is None


def test_months_old():
    assert _months_old("2000-01-01") > 100
    assert _months_old("01-01-2000") > 100          # alternate format
    assert _months_old("not-a-date") is None
    assert _months_old(None) is None


def test_run_success(monkeypatch):
    class R:
        stdout = "hello"
        stderr = "world"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert _run(["echo"]) == "hello world"


def test_run_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(subprocess, "run", boom)
    assert _run(["x"]) == ""


# ------------------------------------------------------- _linux_versions ------
class _FakePath:
    def __init__(self, p):
        self.p = str(p)

    def read_text(self):
        if self.p == "/etc/os-release":
            return 'VERSION_ID="24.04"\nVERSION="24.04.3 LTS (Noble)"\n'
        raise OSError("no file")


def test_linux_versions_cache(monkeypatch):
    monkeypatch.setattr(components, "PLATFORM", "wsl")
    components._cache["linux"] = None
    components._cache["t"] = 0.0
    monkeypatch.setattr(components, "Path", _FakePath)

    def fake_run(cmd, timeout=6.0):
        if any("wsl.exe" in str(c) for c in cmd):
            return "WSL version: 2.6.2.0"
        return "1.2.3"

    monkeypatch.setattr(components, "_run", fake_run)
    v1 = components._linux_versions()
    assert v1["ubuntu"]                                 # parsed from os-release
    assert v1["wsl"] == "2.6.2.0"                        # WSL version regex matched
    # second call hits the cache branch (returns same object)
    v2 = components._linux_versions()
    assert v1 is v2


def test_linux_versions_osrelease_oserror(monkeypatch):
    monkeypatch.setattr(components, "PLATFORM", "wsl")
    components._cache["linux"] = None
    components._cache["t"] = 0.0

    class BadPath:
        def __init__(self, p):
            pass

        def read_text(self):
            raise OSError("boom")

    monkeypatch.setattr(components, "Path", BadPath)
    monkeypatch.setattr(components, "_run", lambda cmd, timeout=6.0: "9.9")
    v = components._linux_versions()
    assert "kernel" in v                                # still populated via _run


# ------------------------------------------------------- build_components -----
def test_build_components_bad_json(tmp_path, monkeypatch):
    bad = tmp_path / "components.json"
    bad.write_text("{not valid")
    monkeypatch.setattr(components, "COMPONENTS_FILE", bad)
    out = build_components({})
    assert out["error"] and out["upgrades"] == 0 and out["rows"] == []


def test_build_components_full(tmp_path, monkeypatch):
    cat = {
        "assessment_date": "2026-09-01", "note": "n",
        "catalog": [
            {"key": "gpu-driver", "name": "GPU", "compare": "date",
             "date_key": "gpu_driver_date", "stale_months": 9, "perf": 30,
             "latest": "ref"},
            {"key": "node", "name": "Node", "compare": "version",
             "latest": "22.0.0", "perf": 20},
            {"key": "git", "name": "Git", "compare": "version",
             "latest": "2.0.0", "perf": 5},
            {"key": "ghost", "name": "Ghost", "compare": "version",
             "latest": "1.0.0", "perf": 1},
        ],
    }
    f = tmp_path / "components.json"
    f.write_text(json.dumps(cat))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "_linux_versions",
                        lambda: {"node": "20.0.0", "git": "2.40.0"})
    latest = {"gpu_driver": "31.0", "gpu_driver_date": "2020-01-01"}
    out = build_components(latest)
    by = {r["key"]: r for r in out["rows"]}
    assert by["gpu-driver"]["outdated"] is True         # date, very old
    assert by["node"]["outdated"] is True               # 20 < 22
    assert by["git"]["outdated"] is False               # 2.40 > 2.0
    assert "ghost" not in by                            # not installed here: hidden
    assert out["upgrades"] == 2 and out["avg_uplift"] > 0


def test_hardware_rows_are_named_after_the_fitted_device(tmp_path, monkeypatch):
    cat = {"catalog": [{"key": "net-driver", "name": "Wi-Fi / network driver",
                        "compare": "date", "date_key": "net_driver_date"}]}
    f = tmp_path / "components.json"
    f.write_text(json.dumps(cat))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "_linux_versions", lambda: {})
    out = build_components({"net_driver_ver": "6001.16", "net_driver_date": "2024-01-01",
                            "net_driver_name": "Realtek RTL8852BE WiFi 6"})
    assert out["rows"][0]["name"] == "Realtek RTL8852BE WiFi 6 driver"


def _windows_run(calls):
    def run(cmd, timeout=6.0):
        calls.append(cmd)
        if cmd[-1] == "--version" and "wsl" in cmd[0]:
            return "WSL version: 2.6.2.0"
        if cmd[0] == "git":
            return "git version 2.47.1.windows.1"
        if cmd[:3] == ["/x/wsl.exe", "-e", "sh"]:
            return ('bwrap=bubblewrap 0.9.0\nclaude=2.1.3 (Claude Code)\n'
                    'ubuntu=VERSION="24.04.3 LTS (Noble Numbat)"\n'
                    'chromium=/home/u/.cache/ms-playwright/chromium-1248\n')
        return ""
    return run


def test_windows_skips_apt_probes_but_reads_the_toolchain(monkeypatch):
    import importlib.metadata as md
    monkeypatch.setattr(components, "PLATFORM", "windows")
    monkeypatch.setattr(components.shutil, "which", lambda n: "/x/wsl.exe" if n == "wsl.exe" else None)
    monkeypatch.setattr(md, "version", lambda d: {"flask": "3.0.3"}.get(d) or (_ for _ in ()).throw(
        md.PackageNotFoundError(d)))
    components._cache["linux"] = None
    components._cache["t"] = 0.0
    calls = []
    monkeypatch.setattr(components, "_run", _windows_run(calls))
    v = components._linux_versions()
    assert v["wsl"] == "2.6.2.0" and v["git-windows"] == "2.47.1" and v["flask"] == "3.0.3"
    assert v["bubblewrap"] == "0.9.0" and v["claude-code"] == "2.1.3"
    assert v["ubuntu"] == "24.04.3" and v["chromium"] == "build 1248"
    assert "playwright" not in v and "python" in v
    assert not any(c[0] in ("uname", "nginx", "lsb_release") for c in calls)   # no apt probes
    components._cache["linux"] = None


def test_windows_without_wsl_still_reports_local_tools(monkeypatch):
    monkeypatch.setattr(components, "PLATFORM", "windows")
    monkeypatch.setattr(components.shutil, "which", lambda n: None)
    components._cache["linux"] = None
    components._cache["t"] = 0.0
    monkeypatch.setattr(components, "_run", _windows_run([]))
    v = components._linux_versions()
    assert v["git-windows"] == "2.47.1" and "claude-code" not in v
    components._cache["linux"] = None


def test_linux_toolchain_finds_chromium_and_wsl_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(components, "PLATFORM", "wsl")
    monkeypatch.setattr(components, "_HOME", str(tmp_path))
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1200").mkdir(parents=True)
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1248").mkdir()
    monkeypatch.setattr(components, "_run", lambda cmd, timeout=6.0:
                        "bubblewrap 0.9.0" if cmd[0] == "bwrap" else
                        "2.1.3 (Claude Code)" if cmd[0].endswith("claude") else "")
    v = components._toolchain_versions({})
    assert v["chromium"] == "build 1248" and v["bubblewrap"] == "0.9.0"
    assert v["claude-code"] == "2.1.3"
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1200").rmdir()
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1248").rmdir()
    assert "chromium" not in components._toolchain_versions({})


def test_toolchain_components_show_when_missing(tmp_path, monkeypatch):
    cat = {"catalog": [
        {"key": "playwright", "name": "Playwright", "compare": "version",
         "latest": "latest (pip install playwright)", "show_missing": True, "perf": 2},
        {"key": "flask", "name": "Flask", "compare": "version", "latest": "3.0.3",
         "show_missing": True, "perf": 3},
        {"key": "ghost", "name": "Ghost", "compare": "version", "latest": "1.0"}]}
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cat))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "_linux_versions", lambda: {"flask": "3.0.3"})
    by = {r["key"]: r for r in build_components({})["rows"]}
    assert by["playwright"]["current"] == "not installed" and by["playwright"]["missing"]
    assert by["playwright"]["outdated"] is False and by["flask"]["outdated"] is False
    assert "ghost" not in by


def test_shipped_catalog_lists_the_toolchain():
    keys = {c["key"] for c in json.loads(components.COMPONENTS_FILE.read_text())["catalog"]}
    assert {"python", "flask", "waitress", "pytest", "playwright", "chromium",
            "git-windows", "claude-code", "bubblewrap"} <= keys


def test_build_components_no_upgrades(tmp_path, monkeypatch):
    cat = {"assessment_date": "", "catalog": [
        {"key": "git", "name": "Git", "compare": "version", "latest": "1.0.0", "perf": 5}]}
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cat))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "_linux_versions", lambda: {"git": "2.0.0"})
    out = build_components({})
    assert out["upgrades"] == 0 and out["avg_uplift"] == 0


def test_wsl_probe_falls_back_to_the_mount_path(monkeypatch):
    """Hardcoding /mnt/c meant this probe only ever worked from inside WSL; it is
    now the last resort, after PATH."""
    seen = []
    monkeypatch.setattr(components.shutil, "which", lambda name: None)
    monkeypatch.setattr(components, "_run",
                        lambda argv, timeout=6: seen.append(argv) or "")
    components._cache["t"] = 0
    components._linux_versions()
    assert ["/mnt/c/WINDOWS/System32/wsl.exe", "--version"] in seen


def test_wsl_probe_prefers_path(monkeypatch):
    seen = []
    monkeypatch.setattr(components.shutil, "which",
                        lambda name: "/usr/bin/wsl.exe" if name == "wsl.exe" else None)
    monkeypatch.setattr(components, "_run",
                        lambda argv, timeout=6: seen.append(argv) or "")
    components._cache["t"] = 0
    components._linux_versions()
    assert ["/usr/bin/wsl.exe", "--version"] in seen


def test_effective_latest_prefers_live_and_never_trails_the_installed_version():
    eff = components.effective_latest
    assert eff("2.6.2.0", "3.0.3", "2.6.1.0") == ("3.0.3", "live")
    assert eff("7.6.7", "", "7.6.8") == ("7.6.8", "installed")     # stale catalog
    assert eff("latest (pip install playwright)", "1.64.1", "") == ("1.64.1", "live")
    assert eff("2.6.2.0", "3.0.3", "3.1.0.0") == ("3.1.0.0", "installed")   # preview
    assert eff("26.04.1", "", "24.04") == ("26.04.1", "catalog")


def test_build_components_uses_live_versions(tmp_path, monkeypatch):
    cat = {"assessment_date": "2026-10-21", "catalog": [
        {"key": "wsl", "name": "WSL", "compare": "version", "latest": "2.6.2.0", "perf": 25},
        {"key": "powershell", "name": "PowerShell", "compare": "version", "latest": "7.6.7",
         "perf": 15},
        {"key": "flask", "name": "Flask", "compare": "version", "latest": "3.0.3",
         "show_missing": True, "perf": 3}]}
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cat))
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"checked_at": 123.0, "latest": {"wsl": "3.0.3",
                                                                "flask": "3.1.3"}}))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "LATEST_FILE", live)
    monkeypatch.setattr(components, "_linux_versions",
                        lambda: {"wsl": "3.0.3.0"})
    out = build_components({"pwsh_version": "7.6.8"})
    by = {r["key"]: r for r in out["rows"]}
    assert by["wsl"]["latest"] == "3.0.3" and by["wsl"]["outdated"] is False
    assert by["powershell"]["latest"] == "7.6.8" and by["powershell"]["latest_source"] == "installed"
    assert by["flask"]["latest"] == "3.1.3" and by["flask"]["missing"]
    assert out["latest_checked_at"] == 123.0 and out["upgrades"] == 0
    live.write_text("[1]")
    assert build_components({"pwsh_version": "7.6.8"})["latest_checked_at"] is None
