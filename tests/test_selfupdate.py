"""Full-coverage tests for selfupdate.py (close before a Store update, reopen after)."""
import base64
import subprocess

import sysperf.selfupdate as su

FULL = "JudeFinney.SysPerfPCPerformanceAgent_5.0.6.0_x64__3g5h2g8ebqydw"
ME = su.parse_full_name(FULL)
# What Windows logged on this PC at 09:26:42 on 31 Oct 2026, ~12 s before the swap.
STAGE = ("<Event><System><EventID>607</EventID></System><EventData><Data>Deployment Stage "
         "operation on package JudeFinney.SysPerfPCPerformanceAgent_5.4.2.0_neutral_~_"
         "3g5h2g8ebqydw has been de-queued</Data></EventData></Event>")


def test_parse_full_name():
    assert ME == {"name": "JudeFinney.SysPerfPCPerformanceAgent", "version": "5.0.6.0",
                  "family": "JudeFinney.SysPerfPCPerformanceAgent_3g5h2g8ebqydw"}
    assert su.parse_full_name("") is None and su.parse_full_name("not a package") is None


def test_newer_version_only_for_this_package_and_only_newer():
    assert su.newer_version(STAGE, ME) == "5.4.2.0"
    assert su.newer_version(STAGE.replace("5.4.2.0", "5.0.6.0"), ME) is None      # itself
    assert su.newer_version(STAGE.replace("5.4.2.0", "5.0.4.0"), ME) is None      # older
    assert su.newer_version(STAGE.replace("JudeFinney.SysPerf", "Other.App"), ME) is None
    both = STAGE + STAGE.replace("5.4.2.0", "5.10.0.0") + STAGE.replace("5.4.2.0", "5.5.0.0")
    assert su.newer_version(both, ME) == "5.10.0.0"                    # numeric, not text
    assert su.newer_version(None, ME) is None


def test_relaunch_script_waits_for_the_new_version_then_opens_it():
    s = su.relaunch_script(ME, "5.4.2.0", wait_s=120)
    assert "AddSeconds(120)" in s and "[version]'5.4.2.0'" in s
    assert "Get-AppxPackage -Name 'JudeFinney.SysPerfPCPerformanceAgent'" in s
    assert "shell:AppsFolder\\JudeFinney.SysPerfPCPerformanceAgent_3g5h2g8ebqydw!SysPerf" in s


def test_start_relauncher_goes_through_wmi_with_encoded_commands():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0)
    assert su.start_relauncher(ME, "5.4.2.0", run=run)
    argv, kw = calls[0]
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    outer = base64.b64decode(argv[4]).decode("utf-16-le")
    assert "Invoke-CimMethod -ClassName Win32_Process -MethodName Create" in outer
    inner_b64 = outer.split("-EncodedCommand ")[1].split("'")[0]
    assert base64.b64decode(inner_b64).decode("utf-16-le") == su.relaunch_script(ME, "5.4.2.0")
    assert kw["timeout"] == 30 and kw["creationflags"] == 0x08000000

    assert not su.start_relauncher(ME, "5.4.2.0", run=lambda a, **k: subprocess.CompletedProcess(a, 9))

    def boom(a, **k):
        raise OSError("no powershell")
    assert not su.start_relauncher(ME, "5.4.2.0", run=boom)


def test_on_event_relaunches_then_quits_once():
    order, state = [], {}

    def relaunch(me, v):
        order.append(("relaunch", v))
    assert su.on_event(STAGE, ME, lambda: order.append("quit"), relaunch, state)
    assert order == [("relaunch", "5.4.2.0"), "quit"]
    assert not su.on_event(STAGE, ME, lambda: order.append("quit"), relaunch, state)  # once
    assert not su.on_event("<Event/>", ME, lambda: order.append("quit"), relaunch)
    assert len(order) == 2


def test_watch_subscribes_only_from_a_package():
    assert not su.watch(lambda: None, package=lambda: None, sub=lambda h: 1)
    handlers, quits = [], []
    assert su.watch(lambda: quits.append(1), package=lambda: ME,
                    sub=lambda h: handlers.append(h) or 1, relaunch=lambda me, v: True)
    handlers[0]("<Event/>")
    handlers[0](STAGE)
    handlers[0](STAGE)
    assert quits == [1]
    assert not su.watch(lambda: None, package=lambda: ME, sub=lambda h: None)


def test_current_package_is_none_outside_a_package():
    assert su.current_package() is None          # tests never run from an MSIX
