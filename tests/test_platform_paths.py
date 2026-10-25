"""Full-coverage tests for platform_paths.py.

Every function takes its environment as an argument, so the Windows and plain
Linux branches are exercised here from a Linux box. That is the whole point of
the module's shape: the WSL-only assumption it replaces survived four releases
precisely because the platform code could not be tested.
"""
from pathlib import Path

import sysperf.platform_paths as pp
from sysperf.platform_paths import (
    LINUX,
    STATE_MARKERS,
    WINDOWS,
    WSL,
    describe,
    detect_platform,
    legacy_has_state,
    powershell_candidates,
    resolve_data_dir,
    resolve_powershell,
    to_host_path,
)


# ----------------------------------------------------------------- detect ----
def test_detect_windows_from_os_name():
    assert detect_platform(os_name="nt") == WINDOWS


def test_detect_wsl_from_proc_version():
    assert detect_platform(os_name="posix",
                           proc_version="Linux 5.15 microsoft-standard-WSL2") == WSL
    # matched case-insensitively: distros differ in how they spell it
    assert detect_platform(os_name="posix", proc_version="MICROSOFT") == WSL


def test_detect_plain_linux():
    assert detect_platform(os_name="posix", proc_version="Linux 6.1 generic") == LINUX


def test_detect_reads_proc_version_when_not_supplied(monkeypatch):
    monkeypatch.setattr(pp.Path, "read_text",
                        lambda self, encoding=None: "microsoft-standard")
    assert detect_platform(os_name="posix") == WSL


def test_detect_treats_an_unreadable_proc_version_as_linux(monkeypatch):
    def boom(self, encoding=None):
        raise OSError("no /proc")

    monkeypatch.setattr(pp.Path, "read_text", boom)
    assert detect_platform(os_name="posix") == LINUX


def test_detect_defaults_to_the_running_platform():
    assert detect_platform() in (WINDOWS, WSL, LINUX)


# ------------------------------------------------------------ powershell ----
def test_windows_candidates_cover_path_and_install_locations():
    got = powershell_candidates(WINDOWS, env={"LOCALAPPDATA": r"C:\Users\x\AppData\Local"},
                                which=lambda n: rf"C:\bin\{n}.exe")
    assert got[0].endswith("pwsh.exe")
    assert any("Program Files" in p for p in got)
    assert any("WindowsPowerShell" in p for p in got)
    assert any(r"AppData\Local" in p for p in got)


def test_windows_candidates_without_localappdata():
    got = powershell_candidates(WINDOWS, env={}, which=lambda n: None)
    assert got and all("AppData" not in p for p in got)


def test_wsl_candidates_use_interop_then_the_mount():
    got = powershell_candidates(WSL, env={}, which=lambda n: f"/usr/bin/{n}")
    assert got[0] == "/usr/bin/pwsh.exe"
    assert any(p.startswith("/mnt/c/") for p in got)


def test_wsl_finds_a_store_install_of_powershell_7_by_glob():
    """A winget/Store install lands in the per-user WindowsApps directory, whose
    path contains the Windows user name. 2.7.0 and earlier hardcoded one
    developer's name there: it leaked into the open-source archive and did not
    work for anybody else. It is now found by glob."""
    found = "/mnt/c/Users/alice/AppData/Local/Microsoft/WindowsApps/pwsh.exe"
    got = powershell_candidates(WSL, env={}, which=lambda n: None,
                                glob=lambda pattern: [found])
    assert found in got
    # ...and ahead of Windows PowerShell 5.1, which cannot run parallel probes
    assert got.index(found) < got.index(
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def test_windows_candidates_include_the_store_location():
    got = powershell_candidates(WINDOWS,
                                env={"LOCALAPPDATA": r"C:\U\x\AppData\Local"},
                                which=lambda n: None)
    assert any("WindowsApps" in p for p in got)


def test_a_service_without_interop_on_path_still_finds_powershell():
    """A systemd user service has no Windows entries on PATH, so the mount paths
    are a normal case rather than a fallback for disabled interop. Missing this
    silently downgraded the live service from pwsh 7 to 5.1."""
    found = "/mnt/c/Program Files/PowerShell/7/pwsh.exe"
    got = resolve_powershell(WSL, env={}, which=lambda n: None,
                             exists=lambda p: p == found, glob=lambda p: [])
    assert got == found


def test_plain_linux_has_no_candidates():
    """There is no Windows host to measure, and pretending there is produced the
    silent failure this module exists to prevent."""
    assert powershell_candidates(LINUX, env={}, which=lambda n: None) == []


def test_an_override_wins_over_everything():
    for key in ("PERFDASH_PS_EXE", "SYSPERF_PS_EXE"):
        got = powershell_candidates(WINDOWS, env={key: r"D:\ps\pwsh.exe"},
                                    which=lambda n: None)
        assert got[0] == r"D:\ps\pwsh.exe"


def test_resolve_returns_the_first_existing_candidate():
    got = resolve_powershell(WSL, env={}, which=lambda n: None,
                             exists=lambda p: p.endswith("powershell.exe"))
    assert got.endswith("powershell.exe")


def test_resolve_accepts_a_bare_name_found_on_path():
    got = resolve_powershell(WINDOWS, env={"PERFDASH_PS_EXE": "pwsh"},
                             which=lambda n: "/usr/bin/pwsh",
                             exists=lambda p: False)
    assert got == "pwsh"


def test_resolve_returns_none_when_nothing_is_installed():
    assert resolve_powershell(LINUX, env={}, which=lambda n: None,
                              exists=lambda p: False) is None


def test_resolve_defaults_to_the_running_platform():
    resolve_powershell(env={}, which=lambda n: None, exists=lambda p: False)


# -------------------------------------------------------------- data dir ----
def test_an_explicit_data_dir_wins(tmp_path):
    for key in ("SYSPERF_DATA_DIR", "PERFDASH_DATA_DIR"):
        got = resolve_data_dir(WINDOWS, env={key: str(tmp_path / "chosen")},
                               legacy=tmp_path)
        assert got == tmp_path / "chosen"


def test_a_tilde_in_the_override_is_expanded():
    assert "~" not in str(resolve_data_dir(LINUX, env={"SYSPERF_DATA_DIR": "~/d"}))


def test_an_existing_install_keeps_its_directory(tmp_path):
    """A version bump must not orphan a user's history."""
    (tmp_path / "history.jsonl").write_text("{}", encoding="utf-8")
    assert resolve_data_dir(WINDOWS, env={}, legacy=tmp_path,
                            home=tmp_path / "home") == tmp_path


def test_every_state_marker_counts_as_an_existing_install(tmp_path):
    for marker in STATE_MARKERS:
        directory = tmp_path / marker
        directory.mkdir()
        (directory / marker).write_text("{}", encoding="utf-8")
        assert legacy_has_state(directory), marker


def test_a_directory_without_state_is_not_an_install(tmp_path):
    assert legacy_has_state(tmp_path) is False


def test_windows_default_is_localappdata(tmp_path):
    got = resolve_data_dir(WINDOWS, env={"LOCALAPPDATA": str(tmp_path / "Local")},
                           legacy=None, home=tmp_path)
    assert got == tmp_path / "Local" / "SysPerf" / "data"


def test_windows_default_without_localappdata_uses_the_profile(tmp_path):
    got = resolve_data_dir(WINDOWS, env={}, legacy=None, home=tmp_path)
    assert got == tmp_path / "AppData" / "Local" / "SysPerf" / "data"


def test_linux_default_honours_xdg(tmp_path):
    got = resolve_data_dir(LINUX, env={"XDG_DATA_HOME": str(tmp_path / "xdg")},
                           legacy=None, home=tmp_path)
    assert got == tmp_path / "xdg" / "sysperf"


def test_linux_default_without_xdg(tmp_path):
    got = resolve_data_dir(LINUX, env={}, legacy=None, home=tmp_path)
    assert got == tmp_path / ".local" / "share" / "sysperf"


def test_the_package_directory_is_never_chosen_for_a_fresh_install(tmp_path):
    """Once pip-installed the package directory is site-packages: possibly
    read-only, wiped on upgrade, and no place for a multi-megabyte history."""
    pkg = tmp_path / "site-packages" / "sysperf"
    pkg.mkdir(parents=True)
    got = resolve_data_dir(WINDOWS, env={"LOCALAPPDATA": str(tmp_path / "L")},
                           legacy=pkg, home=tmp_path)
    assert got != pkg


def test_data_dir_defaults_to_the_running_platform(tmp_path):
    assert resolve_data_dir(env={}, legacy=None, home=tmp_path)


# ------------------------------------------------------------- host path ----
def test_a_windows_path_needs_no_translation():
    assert to_host_path(r"C:\x\y.ps1", WINDOWS) == r"C:\x\y.ps1"


def test_a_linux_path_is_returned_unchanged():
    assert to_host_path("/opt/x.ps1", LINUX) == "/opt/x.ps1"


def test_a_wsl_path_is_translated_through_wslpath():
    calls = []

    class Result:
        stdout = "C:\\translated\\x.ps1\n"

    def run(argv, **kw):
        calls.append(argv)
        return Result()

    assert to_host_path("/home/u/x.ps1", WSL, run=run) == r"C:\translated\x.ps1"
    assert calls == [["wslpath", "-w", "/home/u/x.ps1"]]


def test_an_empty_wslpath_result_falls_back_to_the_input():
    class Result:
        stdout = "  \n"

    assert to_host_path("/p", WSL, run=lambda argv, **kw: Result()) == "/p"


def test_a_missing_wslpath_does_not_raise():
    """This used to run at Collector construction, so a failure took the whole
    service down before it could report why."""
    def boom(argv, **kw):
        raise FileNotFoundError("wslpath")

    assert to_host_path("/p", WSL, run=boom) == "/p"


def test_host_path_accepts_a_path_object():
    assert to_host_path(Path("/a/b"), LINUX) == str(Path("/a/b"))


def test_host_path_defaults_to_the_running_platform():
    assert to_host_path("/x")


# --------------------------------------------------------------- describe ----
def test_describe_reports_a_working_platform(monkeypatch):
    monkeypatch.setattr(pp, "resolve_powershell", lambda p, e: "/bin/pwsh")
    facts = describe(WSL, env={})
    assert facts == {"platform": WSL, "powershell": "/bin/pwsh",
                     "can_collect": True, "why_not": None}


def test_describe_explains_a_platform_that_cannot_collect(monkeypatch):
    monkeypatch.setattr(pp, "resolve_powershell", lambda p, e: None)
    facts = describe(LINUX, env={})
    assert facts["can_collect"] is False
    assert "no PowerShell found" in facts["why_not"]
    assert "SYSPERF_PS_EXE" in facts["why_not"]


def test_describe_defaults_to_the_running_platform():
    assert describe()["platform"] in (WINDOWS, WSL, LINUX)
