"""Tray auto-quit: quits a listed app only after it has been windowless for the
grace period, never young/open/unlisted/blocked apps, and honours FIX_ENABLED.
4.1: per-app consent, the busy guard and reopen (undo)."""
import pytest

from sysperf.trayquit import TrayQuitter, allowed


class FakeApi:
    def __init__(self):
        self.procs = []          # (pid, name)
        self.visible = set()
        self.start = {}
        self.mem = {}
        self.killed = []
        self.cpu = {}            # pid -> cumulative CPU seconds (None = unreadable)
        self.exe = {}
        self.launched = []
        self.launch_ok = True

    def processes(self):
        return [{"pid": p, "name": n} for p, n in self.procs if p not in self.killed]

    def visible_pids(self):
        return set(self.visible)

    def started(self, pid):
        return self.start.get(pid, 0.0)

    def mem_mb(self, pid):
        return self.mem.get(pid, 100.0)

    def terminate(self, pid):
        self.killed.append(pid)
        return True

    def cpu_s(self, pid):
        return self.cpu.get(pid, 0.0)

    def exe_path(self, pid):
        return self.exe.get(pid)

    def launch(self, path):
        self.launched.append(path)
        return self.launch_ok


def make(tmp_path, api, clock, **kw):
    return TrayQuitter(tmp_path / "s.json", tmp_path / "l.jsonl",
                       api_factory=lambda: api, clock=lambda: clock[0], **kw)


def test_quits_after_grace_only(tmp_path):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT"), (2, "ChatGPT")]
    t = make(tmp_path, api, clock)
    t.configure(consent={"name": "ChatGPT", "decision": "allow"})
    assert t.tick() == []                       # first windowless sighting
    clock[0] += 30
    assert t.tick() == [] and api.killed == []  # still inside the 60 s grace
    clock[0] += 31
    ev = t.tick()
    assert [e["name"] for e in ev] == ["ChatGPT"] and sorted(api.killed) == [1, 2]
    assert ev[0]["freed_mb"] == 200
    assert t.status()["freed_24h_mb"] == 200


def test_open_or_minimized_window_is_never_quit(tmp_path):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT"), (2, "ChatGPT")]
    api.visible = {2}
    t = make(tmp_path, api, clock)
    for _ in range(5):
        clock[0] += 120
        assert t.tick() == []
    assert api.killed == []


def test_young_process_and_unlisted_apps_are_left_alone(tmp_path):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT"), (5, "Notepad")]
    api.start = {1: 990.0}                      # started 10 s ago
    t = make(tmp_path, api, clock)
    t.tick()
    clock[0] += 30                              # 40 s old: still starting
    assert t.tick() == []
    assert 5 not in api.killed


def test_execution_disabled_only_reports(tmp_path):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT")]
    t = make(tmp_path, api, clock, fix_enabled=False)
    t.tick()
    clock[0] += 600
    assert t.tick() == [] and api.killed == []
    assert t.status()["apps_live"][0]["state"] == "in tray"


def test_blocked_and_critical_names_cannot_be_added(tmp_path):
    t = make(tmp_path, FakeApi(), [0.0])
    for bad in ("explorer", "msedge", "lsass", "pythonw", "MsMpEng", "msrdc", "bad;name"):
        assert not allowed(bad)
        try:
            t.configure(add=bad)
            raise AssertionError(f"{bad} was accepted")
        except ValueError:
            pass
    t.configure(add="Teams", grace_s=5)
    assert "Teams" in t.settings["apps"] and t.settings["grace_s"] == 15


def test_settings_persist(tmp_path):
    t = make(tmp_path, FakeApi(), [0.0])
    t.configure(enabled=False, remove="Spotify")
    t2 = make(tmp_path, FakeApi(), [0.0])
    assert t2.settings["enabled"] is False and "Spotify" not in t2.settings["apps"]


# ------------------------------------------------------------------ 4.1 --
def _tray(tmp_path, decision=None, **kw):
    api, clock, events = FakeApi(), [1000.0], []
    api.procs = [(1, "ChatGPT")]
    api.exe = {1: r"C:\Apps\ChatGPT.exe"}
    t = make(tmp_path, api, clock, on_event=lambda k, d: events.append((k, d)), **kw)
    if decision:
        t.configure(consent={"name": "ChatGPT", "decision": decision})
    t.tick()
    clock[0] += 61
    return t, api, clock, events


def test_undecided_app_waits_for_consent_and_asks_once(tmp_path):
    t, api, clock, events = _tray(tmp_path)
    assert t.tick() == [] and api.killed == []
    assert t.status()["awaiting_consent"] == ["ChatGPT"]
    clock[0] += 60
    t.tick()
    assert [k for k, _ in events] == ["consent"]          # asked once, not every tick
    assert events[0][1]["name"] == "ChatGPT"


def test_allowed_app_is_quit_with_its_exe_recorded_and_reported(tmp_path):
    t, api, clock, events = _tray(tmp_path, "allow")
    ev = t.tick()
    assert ev[0]["exe"] == r"C:\Apps\ChatGPT.exe" and api.killed == [1]
    assert events[-1][0] == "quit"


def test_never_means_never(tmp_path):
    t, api, clock, events = _tray(tmp_path, "never")
    assert t.tick() == [] and api.killed == []
    assert t.status()["apps_live"][0]["state"] == "kept (never quit)"


def test_busy_app_is_left_alone_that_round(tmp_path):
    t, api, clock, events = _tray(tmp_path, "allow")
    api.cpu[1] = 30.0                        # 30 CPU-seconds in 61 s = ~49 % of a core
    assert t.tick() == [] and api.killed == []
    assert t.status()["apps_live"][0]["state"] == "busy"
    clock[0] += 60                           # idle again -> quit
    assert t.tick() and api.killed == [1]


def test_unreadable_cpu_does_not_block(tmp_path):
    t, api, clock, events = _tray(tmp_path, "allow")
    api.cpu[1] = None
    assert t.tick() and api.killed == [1]


def test_reopen_launches_only_the_recorded_exe(tmp_path):
    t, api, clock, events = _tray(tmp_path, "allow")
    t.tick()
    r = t.reopen("chatgpt")
    assert r == {"name": "ChatGPT", "reopened": True, "exe": r"C:\Apps\ChatGPT.exe"}
    assert api.launched == [r"C:\Apps\ChatGPT.exe"] and t.recent[-1]["reopened_at"]
    api.launch_ok = False
    assert t.reopen("ChatGPT")["reopened"] is False
    with pytest.raises(ValueError, match="no recent"):
        t.reopen("Notepad")


def test_reopen_refuses_when_exe_unknown(tmp_path):
    t, api, clock, events = _tray(tmp_path, "allow")
    api.exe = {}
    t.tick()
    with pytest.raises(ValueError, match="not recorded"):
        t.reopen("ChatGPT")


def test_consent_and_busy_settings_validate_and_persist(tmp_path):
    t = make(tmp_path, FakeApi(), [0.0])
    for bad in ({"name": "ChatGPT", "decision": "maybe"}, {"decision": "allow"}, "x"):
        with pytest.raises(ValueError):
            t.configure(consent=bad)
    with pytest.raises(ValueError, match="not on the auto-quit list"):
        t.configure(consent={"name": "Notepad", "decision": "allow"})
    with pytest.raises(ValueError):
        t.configure(busy_cpu_pct=True)
    t.configure(consent={"name": "Slack", "decision": "never"}, busy_cpu_pct=99)
    assert t.consent_of("slack") == "never" and t.settings["busy_cpu_pct"] == 50.0
    t2 = make(tmp_path, FakeApi(), [0.0])
    assert t2.consent_of("Slack") == "never"
    t2.configure(remove="Slack")
    assert "slack" not in t2.settings["consent"]


def test_failing_event_hook_never_breaks_the_check(tmp_path, caplog):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT")]

    def boom(kind, details):
        raise RuntimeError("hook down")
    t = make(tmp_path, api, clock, on_event=boom)
    t.tick()
    clock[0] += 61
    t.tick()
    assert "event hook failed" in caplog.text


def test_unwritable_state_and_log_are_tolerated(tmp_path, caplog):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT")]
    t = TrayQuitter(tmp_path / "no" / "s.json", tmp_path / "no" / "l.jsonl",
                    api_factory=lambda: api, clock=lambda: clock[0])
    t.configure(consent={"name": "ChatGPT", "decision": "allow"})
    assert "could not save state" in caplog.text
    t.tick()
    clock[0] += 61
    assert t.tick() and api.killed == [1]                  # log write failed silently


def test_store_install_starts_with_auto_quit_off(tmp_path):
    t = make(tmp_path, FakeApi(), [0.0], default_enabled=False)
    assert t.settings["enabled"] is False
    t.configure(enabled=True)
    assert make(tmp_path, FakeApi(), [0.0], default_enabled=False).settings["enabled"] is True


def test_state_is_saved_atomically(tmp_path):
    # 5.5: a crash mid-write must not wipe the per-app consents
    import json
    q = make(tmp_path, FakeApi(), [0.0])
    q.state_file = tmp_path / "tq.json"
    q._save()
    assert json.loads((tmp_path / "tq.json").read_text())["recent"] == []
    assert not (tmp_path / "tq.tmp").exists()                 # replaced, not left behind
