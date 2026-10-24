"""Full-coverage tests for components.py."""
import json
import subprocess

import sysperf.components as components
from sysperf.components import _run, _ver, _cmp, _months_old, build_components


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


def test_windows_skips_linux_side_probes(monkeypatch):
    monkeypatch.setattr(components, "PLATFORM", "windows")
    components._cache["linux"] = None
    components._cache["t"] = 0.0
    calls = []
    monkeypatch.setattr(components, "_run",
                        lambda cmd, timeout=6.0: calls.append(cmd) or "WSL version: 2.6.2.0")
    v = components._linux_versions()
    assert v == {"wsl": "2.6.2.0"} and len(calls) == 1
    components._cache["linux"] = None


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
