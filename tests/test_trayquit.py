"""Tray auto-quit: quits a listed app only after it has been windowless for the
grace period, never young/open/unlisted/blocked apps, and honours FIX_ENABLED."""
from sysperf.trayquit import TrayQuitter, allowed


class FakeApi:
    def __init__(self):
        self.procs = []          # (pid, name)
        self.visible = set()
        self.start = {}
        self.mem = {}
        self.killed = []

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


def make(tmp_path, api, clock, **kw):
    return TrayQuitter(tmp_path / "s.json", tmp_path / "l.jsonl",
                       api_factory=lambda: api, clock=lambda: clock[0], **kw)


def test_quits_after_grace_only(tmp_path):
    api, clock = FakeApi(), [1000.0]
    api.procs = [(1, "ChatGPT"), (2, "ChatGPT")]
    t = make(tmp_path, api, clock)
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
