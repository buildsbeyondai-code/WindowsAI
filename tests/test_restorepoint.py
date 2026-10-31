"""Full-coverage tests for restorepoint.py (automatic System Restore points)."""
import json
import threading

import sysperf.restorepoint as rpm

NOW = 1_800_000_000.0


class Run:
    """Scripted runner: answers by the first matching fragment of the argv."""

    def __init__(self, answers=None, default=(0, "")):
        self.answers, self.default, self.calls = list(answers or []), default, []

    def __call__(self, argv, timeout):
        joined = " ".join(map(str, argv))
        self.calls.append(joined)
        for frag, ans in self.answers:
            if frag in joined:
                if isinstance(ans, list):
                    return ans.pop(0) if len(ans) > 1 else ans[0]
                return ans
        return self.default

    def ran(self, frag):
        return [c for c in self.calls if frag in c]


def make(tmp_path, run=None, status=None, auto=True, ps="pwsh"):
    d = tmp_path / "pd"
    d.mkdir(parents=True, exist_ok=True)
    if status is not None:
        (d / "status.json").write_text(json.dumps(status))
    return rpm.RestorePoints(tmp_path / "rp.json", ps, run or Run(), default_auto=auto,
                             status_dir=d, clock=lambda: NOW)


def test_status_auto_and_describe(tmp_path, monkeypatch):
    r = make(tmp_path, Run([("/query", (1, ""))]))
    assert r.status() == {} and r.newest() is None and r.auto
    (tmp_path / "pd" / "status.json").write_text("[1]")
    assert r.status() == {}
    d = r.describe()
    assert d["installed"] is False and d["age_s"] is None and d["declined"] == 0
    assert r.set_auto(False) is False
    assert make(tmp_path).auto is False                       # persisted
    (tmp_path / "rp.json").write_text("[]")
    assert make(tmp_path, auto=True).auto is True             # a junk file is ignored
    ok = make(tmp_path, status={"newest": NOW - 3600, "count": 2, "ok": True, "ts": NOW})
    assert ok.describe()["age_s"] == 3600 and ok.describe()["count"] == 2
    monkeypatch.setenv("PROGRAMDATA", "D:\\PD")
    assert str(rpm.default_dir()).replace("/", "\\").endswith("PD\\SysPerf\\restore")


def test_installed_is_cached_for_a_minute(tmp_path):
    run = Run([("/query", (0, ""))])
    r = make(tmp_path, run)
    t = [NOW]
    r.clock = lambda: t[0]
    assert r.installed() and r.installed() and len(run.ran("/query")) == 1
    t[0] += 61
    assert r.installed() and len(run.ran("/query")) == 2
    assert make(tmp_path, ps=None).installed() is False


def test_setup_outcomes(tmp_path, monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "PC")
    monkeypatch.setenv("USERNAME", "Jo Smith")
    run = Run([("RunAs", (0, "")), ("/query", (0, ""))])
    r = make(tmp_path, run)
    assert r.setup() == {"ok": True, "reason": "set up", "by": "user"}
    cmd = run.ran("RunAs")[0]
    assert '"PC\\Jo Smith"' in cmd and "-Mode install" in cmd     # quoted for Start-Process
    assert str(rpm.SCRIPT) in cmd and r.state["last_prompt"] == NOW
    r = make(tmp_path, Run([("RunAs", (rpm.DECLINED, "cancelled")), ("/query", (1, ""))]))
    assert "declined" in r.setup()["reason"] and r.state["declined"] == 1
    r = make(tmp_path, Run([("RunAs", (5, "access denied")), ("/query", (1, ""))]))
    assert r.setup()["reason"] == "set-up failed (rc 5): access denied"
    assert make(tmp_path, ps=None).setup()["ok"] is False


def test_remove(tmp_path):
    r = make(tmp_path, Run([("RunAs", (0, ""))]))
    assert r.remove() == {"ok": True, "reason": "removed"} and r.auto is False
    r = make(tmp_path, Run([("RunAs", (rpm.DECLINED, ""))]))
    assert "declined" in r.remove()["reason"]
    r = make(tmp_path, Run([("RunAs", (2, "nope"))]))
    assert r.remove()["reason"] == "rc 2: nope"


def test_create_reads_the_helpers_answer(tmp_path):
    line = json.dumps({"ok": True, "created": True, "message": "created", "newest": NOW})
    r = make(tmp_path, Run([("-Mode request", (0, "noise\n" + line))]))
    res = r.create("the registry fix")
    assert res["ok"] and res["created"] and res["newest"] == NOW
    r = make(tmp_path, Run([("-Mode request", (2, "the restore-point helper is not set up"))]))
    res = r.create("x")
    assert not res["ok"] and "not set up" in res["message"]
    r = make(tmp_path, Run([("-Mode request", (1, "{bad"))]))
    assert r.create("x")["message"] == "{bad"
    r = make(tmp_path, Run([("-Mode request", (1, ""))]))
    assert r.create("x")["message"] == "rc 1"


def test_ensure_states(tmp_path):
    assert make(tmp_path, auto=False).ensure("x")["state"] == "off"
    recent = make(tmp_path, status={"newest": NOW - 3600})
    assert recent.ensure("x")["state"] == "recent" and recent.last["state"] == "recent"
    line = json.dumps({"ok": True, "created": True, "message": "created"})
    run = Run([("/query", (0, "")), ("-Mode request", (0, line))])
    r = make(tmp_path, run, status={"newest": NOW - 3 * 86400})
    assert r.ensure("the registry fix")["state"] == "created"
    run = Run([("/query", (0, "")), ("-Mode request", (1, json.dumps({"ok": False})))])
    assert make(tmp_path, run)._ensure("x", True)["state"] == "failed"


def test_ensure_sets_the_helper_up_when_it_can(tmp_path):
    line = json.dumps({"ok": True, "created": True})
    run = Run([("/query", [(1, ""), (0, "")]), ("RunAs", (0, "")), ("-Mode request", (0, line))])
    r = make(tmp_path, run)
    assert r.ensure("an app update")["state"] == "created" and run.ran("RunAs")
    # declined, then not asked again within a day
    run = Run([("/query", (1, "")), ("RunAs", (rpm.DECLINED, ""))])
    r = make(tmp_path / "declined", run)
    assert r.ensure("x")["state"] == "not_set_up"
    assert r.ensure("x")["state"] == "not_set_up" and len(run.ran("RunAs")) == 1
    # nobody at the PC: no prompt at all
    run = Run([("/query", (1, ""))])
    assert make(tmp_path / "n", run).ensure("x", may_prompt=False)["state"] == "not_set_up"
    assert not run.ran("RunAs")


def test_ensure_never_raises(tmp_path):
    r = make(tmp_path)

    def boom(*a):
        raise OSError("schtasks missing")
    r.newest = boom
    res = r.ensure("x")
    assert res["state"] == "failed" and "schtasks missing" in res["message"]


def test_loop_makes_the_weekly_one_and_stops(tmp_path):
    r = make(tmp_path, status={"newest": NOW - 8 * 86400})
    stop, seen = threading.Event(), []
    r.ensure = lambda reason, may_prompt=True: (seen.append((reason, may_prompt)), stop.set())
    r.loop(present=lambda: False, stop=stop, tick_s=0.01)
    assert seen == [("the weekly safety net", False)]
    fresh = make(tmp_path, status={"newest": NOW - 3600})
    fresh.ensure = lambda *a, **k: seen.append("again")
    stop2 = threading.Event()
    stop2.set()
    fresh.loop(stop=stop2, tick_s=0.01)
    assert "again" not in seen
