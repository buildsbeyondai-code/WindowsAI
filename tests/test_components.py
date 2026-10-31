"""Full-coverage tests for components.py."""
import json
import re
import subprocess
from datetime import datetime

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
    monkeypatch.setattr(components, "lock_packages", lambda: [])   # this catalog only
    latest = {"gpu_driver": "31.0", "gpu_driver_date": "2020-01-01"}
    out = build_components(latest)
    by = {r["key"]: r for r in out["rows"]}
    assert by["gpu-driver"]["outdated"] is True         # date, very old
    assert by["node"]["outdated"] is True               # 20 < 22
    assert by["git"]["outdated"] is False               # 2.40 > 2.0
    assert by["ghost"]["missing"] and by["ghost"]["outdated"] is False   # listed, not counted
    assert out["rows"][-1]["key"] == "ghost"            # not-installed rows sort last
    assert out["upgrades"] == 2 and out["avg_uplift"] > 0
    assert out["total"] == 3 and out["not_installed"] == 1


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
                    'chromium=/home/u/.cache/ms-playwright/chromium-1248\n'
                    'kernel=6.18.54.1-microsoft-standard-WSL2\npython3=Python 3.12.3\n'
                    'git=git version 2.43.0\ndocker=\n')
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
    # The Linux-side rows are read inside the distro, never from the Windows PATH.
    assert v["kernel"] == "6.18.54.1" and v["python3"] == "3.12.3" and v["git"] == "2.43.0"
    assert "docker" not in v                                   # not installed in WSL
    assert not any(c[0] in ("uname", "docker", "lsb_release") for c in calls)  # no host probes
    probe = next(c[-1] for c in calls if c[:3] == ["/x/wsl.exe", "-e", "sh"])
    assert "command -v docker" in probe
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


def test_components_this_machine_lacks_are_listed_as_not_installed(tmp_path, monkeypatch):
    cat = {"catalog": [
        {"key": "playwright", "name": "Playwright", "compare": "version",
         "latest": "latest (pip install playwright)", "perf": 2},
        {"key": "flask", "name": "Flask", "compare": "version", "latest": "3.0.3",
         "perf": 3},
        {"key": "ghost", "name": "Ghost", "compare": "version", "latest": "1.0"}]}
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cat))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "_linux_versions", lambda: {"flask": "3.0.3"})
    by = {r["key"]: r for r in build_components({})["rows"]}
    assert by["playwright"]["current"] == "not installed" and by["playwright"]["missing"]
    assert by["playwright"]["outdated"] is False and by["flask"]["outdated"] is False
    assert by["ghost"]["current"] == "not installed" and by["ghost"]["optional"] is False


def test_shipped_catalog_keeps_only_what_sysperf_uses():
    cat = {c["key"]: c for c in json.loads(components.COMPONENTS_FILE.read_text())["catalog"]}
    assert not {"node", "npm", "nginx", "gunicorn", "kiro-cli"} & set(cat)
    assert cat["docker"]["optional"] is True
    assert [k for k, c in cat.items() if c.get("optional")] == ["docker"]


def test_shipped_catalog_lists_the_toolchain():
    keys = {c["key"] for c in json.loads(components.COMPONENTS_FILE.read_text())["catalog"]}
    assert {"python", "flask", "waitress", "pytest", "playwright", "chromium",
            "git-windows", "claude-code", "bubblewrap"} <= keys


def test_every_sbom_package_is_a_component_with_a_live_feed(monkeypatch):
    """The CI SBOM is a venv of requirements.lock (plus pip); each of those
    packages must be a Components row, read live and checked against PyPI."""
    from sysperf import version_check
    lock = (components.BASE.parents[1] / "requirements.lock").read_text(encoding="utf-8")
    sbom = set(re.findall(r"^([a-z0-9][a-z0-9._-]*)==", lock, re.M)) | {"pip"}
    assert {"flask", "waitress", "werkzeug"} <= sbom
    keys = {c["key"] for c in components.catalog(json.loads(
        components.COMPONENTS_FILE.read_text()))}
    assert sbom <= keys, sbom - keys
    assert sbom <= set(version_check.SOURCES), sbom - set(version_check.SOURCES)
    monkeypatch.setattr(components, "_run", lambda cmd, timeout=6.0: "")   # metadata only
    found = components._toolchain_versions({})
    assert sbom <= set(found), sbom - set(found)          # installed with SysPerf


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
    components._probe_linux()
    assert ["/mnt/c/WINDOWS/System32/wsl.exe", "--version"] in seen


def test_wsl_probe_prefers_path(monkeypatch):
    seen = []
    monkeypatch.setattr(components.shutil, "which",
                        lambda name: "/usr/bin/wsl.exe" if name == "wsl.exe" else None)
    monkeypatch.setattr(components, "_run",
                        lambda argv, timeout=6: seen.append(argv) or "")
    components._probe_linux()
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
         "perf": 3}]}
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
    assert out["assessment_date"] == out["catalog_date"] == "2026-10-21"   # 1970 check is older
    live.write_text("[1]")
    assert build_components({"pwsh_version": "7.6.8"})["latest_checked_at"] is None


def test_assessment_date_follows_the_daily_live_check(tmp_path, monkeypatch):
    # 5.4 live: the card read "Dated 2026-10-25" days after the daily re-check
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"assessment_date": "2026-10-25", "catalog": []}))
    live = tmp_path / "live.json"
    ts = datetime(2026, 10, 31, 12).timestamp()
    live.write_text(json.dumps({"checked_at": ts, "latest": {}}))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "LATEST_FILE", live)
    monkeypatch.setattr(components, "_linux_versions", lambda: {})
    out = build_components({})
    assert out["assessment_date"] == "2026-10-31" and out["catalog_date"] == "2026-10-25"
    live.write_text(json.dumps({"checked_at": "never"}))
    assert build_components({})["assessment_date"] == "2026-10-25"


def test_a_component_that_ships_in_a_current_carrier_waits_for_it(tmp_path, monkeypatch):
    # 5.4 live: the WSL kernel read "upgrade" for 6.18.58.1 while the newest WSL
    # (installed) still ships 6.18.54.1, so nothing could ever upgrade it
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"catalog": [
        {"key": "kernel", "name": "WSL kernel", "latest": "6.18.58.1", "perf": 20,
         "upgrade": {"via": "follows", "of": "wsl"}},
        {"key": "wsl", "name": "WSL", "latest": "3.0.3", "perf": 25,
         "upgrade": {"via": "wsl"}}]}))
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "LATEST_FILE", tmp_path / "none.json")
    monkeypatch.setattr(components, "_linux_versions",
                        lambda: {"kernel": "6.18.54.1", "wsl": "3.0.3"})
    out = build_components({})
    k = {r["key"]: r for r in out["rows"]}["kernel"]
    assert k["outdated"] is False and k["waiting_on"] == "WSL" and out["upgrades"] == 0
    monkeypatch.setattr(components, "_linux_versions",          # WSL behind: both upgrade
                        lambda: {"kernel": "6.18.54.1", "wsl": "3.0.2"})
    out = build_components({})
    assert out["upgrades"] == 2 and "waiting_on" not in out["rows"][0]


def test_a_package_the_resolver_added_becomes_a_component(tmp_path):
    # 5.4.4: an upgrade can bring a new dependency into the lock; it is tracked
    # and upgraded like the rest instead of failing the SBOM check
    rows = components.catalog({"catalog": [{"key": "flask"}]}, lock=["colorama", "flask"])
    assert [r["key"] for r in rows] == ["flask", "colorama"]
    assert rows[1]["upgrade"] == {"via": "pip-lock"} and rows[1]["compare"] == "version"
    from sysperf import config, version_check
    lock = tmp_path / "requirements.lock"
    lock.write_text("# h\nColorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash=sha256:x\n")
    assert config.lock_packages(lock) == ["colorama"]
    assert config.lock_packages(tmp_path / "none.lock") == []
    assert "flask" in config.lock_packages() and "flask" in version_check.SOURCES


def test_a_stale_version_cache_is_served_while_one_thread_refreshes(monkeypatch):
    # 5.4.4 live: the hourly wsl.exe probe ran on web requests ("Task queue depth")
    import threading
    calls = []
    gate = threading.Event()

    def probe():
        calls.append(1)
        gate.wait(5)
        components._cache.update(linux={"python": "new"}, t=components.time.time())
    monkeypatch.setattr(components, "_probe_linux", probe)
    components._cache.update(linux={"python": "old"}, t=0.0)
    assert components._linux_versions() == {"python": "old"}     # served at once
    assert components._linux_versions() == {"python": "old"}     # no second refresh
    gate.set()
    for t in threading.enumerate():
        if t.name == "components-refresh":
            t.join(5)
    assert calls == [1] and components._linux_versions() == {"python": "new"}

    def boom():
        raise OSError("wsl busy")
    monkeypatch.setattr(components, "_probe_linux", boom)
    components._cache["t"] = 0.0
    components._linux_versions()
    for t in threading.enumerate():
        if t.name == "components-refresh":
            t.join(5)
    assert not components._refresh_lock.locked()                  # released after a failure
    components._cache.update(linux=None, t=0.0)


def test_a_driver_is_an_upgrade_only_when_windows_update_offers_one(tmp_path, monkeypatch):
    # 5.4.5 live: the Realtek driver read "upgrade" for its age although it was the
    # newest HP and Windows Update had
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"catalog": [
        {"key": "net-driver", "name": "Wi-Fi", "compare": "date", "date_key": "net_driver_date",
         "stale_months": 9, "latest": "manufacturer's latest", "perf": 30},
        {"key": "gpu-driver", "name": "GPU", "compare": "date", "date_key": "gpu_driver_date",
         "stale_months": 9, "latest": "manufacturer's latest", "perf": 22}]}))
    live = tmp_path / "live.json"
    monkeypatch.setattr(components, "COMPONENTS_FILE", f)
    monkeypatch.setattr(components, "LATEST_FILE", live)
    monkeypatch.setattr(components, "_linux_versions", lambda: {})
    monkeypatch.setattr(components, "lock_packages", lambda: [])
    snap = {"net_driver_ver": "6001.15.163.0", "net_driver_date": "2020-01-27",
            "net_driver_name": "Realtek RTL8852BE-VS WiFi 6 802.11ax PCIe Adapter",
            "gpu_driver": "32.0", "gpu_driver_date": "2020-09-03",
            "gpu_name": "Intel(R) UHD Graphics"}
    by = lambda: {r["key"]: r for r in build_components(snap)["rows"]}   # noqa: E731
    assert by()["net-driver"]["outdated"] is True                        # never checked: by age
    offered = [{"title": "Intel - Bluetooth - 23.1", "model": "Intel Wireless Bluetooth",
                "cls": "Bluetooth"}]                                      # other class: ignored
    live.write_text(json.dumps({"drivers": {"checked_at": 9.0, "offered": offered}}))
    rows = by()
    assert rows["net-driver"]["outdated"] is False and rows["net-driver"]["latest"] == "newest offered"
    assert rows["gpu-driver"]["outdated"] is False and rows["gpu-driver"]["checked_at"] == 9.0
    offered.append({"title": "Realtek Semiconductor Corp. - Net - 6001.16.2.0",
                    "model": "Realtek RTL8852BE WiFi 6 802.11ax PCIe Adapter", "cls": "Net"})
    live.write_text(json.dumps({"drivers": {"checked_at": 9.0, "offered": offered}}))
    net = by()["net-driver"]
    assert net["outdated"] is True and "6001.16.2.0" in net["latest"]
    assert net["latest_source"] == "windows-update"
    assert components._offered_for("", offered) is None
