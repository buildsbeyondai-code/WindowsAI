"""Notifications: inbox, dedupe, quiet hours, settings validation and the toast."""
import base64
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from sysperf import notify
from sysperf.notify import KINDS, Notifier, send_toast, toast_argv


def _n(tmp_path, clock, capable=True, **kw):
    sent = []
    n = Notifier(tmp_path / "n.json", "http://127.0.0.1:5056", toast_capable=capable,
                 sender=lambda *a: sent.append(a), clock=lambda: clock[0],
                 background=False, **kw)
    return n, sent


def test_notify_records_toasts_and_dedupes(tmp_path):
    clock = [1_000_000.0]
    n, sent = _n(tmp_path, clock)
    a = n.notify("score", "Score 60%", "below threshold", key="score:breach")
    assert a["id"] == 1 and a["toast"] is True and not a["read"]
    assert sent == [("Score 60%", "below threshold", "http://127.0.0.1:5056/#home")]
    assert n.notify("score", "Score 59%", "again", key="score:breach") is None
    clock[0] += 1801
    assert n.notify("score", "Score 58%", "later", key="score:breach")["id"] == 2
    b = n.notify("action", "Quit X", "freed", link="javascript:alert(1)")
    assert b["link"] == "#home"                                  # only in-page links
    assert n.items()["unread"] == 3 and n.items(limit=1)["items"][0]["id"] == 3
    with pytest.raises(ValueError):
        n.notify("nope", "t", "b")


def test_inbox_persists_and_mark_read(tmp_path):
    clock = [1.0]
    n, _ = _n(tmp_path, clock)
    for i in range(3):
        n.notify("health", f"t{i}", "b", key=str(i))
    assert n.mark_read([1]) == 1 and n.mark_read([1]) == 0
    n2, _ = _n(tmp_path, clock)
    assert n2.items()["unread"] == 2 and n2._seq == 3
    assert n2.mark_read(None) == 2 and n2.items()["unread"] == 0


def test_capacity_trims_the_inbox(tmp_path):
    n, _ = _n(tmp_path, [1.0], capacity=2)
    for i in range(4):
        n.notify("health", f"t{i}", "b", key=str(i))
    assert [x["title"] for x in n.items()["items"]] == ["t3", "t2"]


def test_toast_suppressed_when_disabled_kind_off_quiet_or_incapable(tmp_path):
    def at(h, m=30):
        return datetime(2026, 10, 25, h, m).timestamp()

    clock = [at(23)]
    n, sent = _n(tmp_path, clock)
    n.configure({"quiet_start": 22, "quiet_end": 7})                 # wraps midnight
    assert n.notify("health", "a", "b", key="1")["toast"] is False
    clock[0] = at(6)
    assert n.notify("health", "a", "b", key="1b")["toast"] is False
    clock[0] = at(12)
    assert n.notify("health", "a", "b", key="1c")["toast"] is True
    n.configure({"quiet_start": 9, "quiet_end": 17})                 # same-day window
    assert n.notify("health", "a", "b", key="2")["toast"] is False
    clock[0] = at(17)                                                # end is exclusive
    assert n.notify("health", "a", "b", key="2b")["toast"] is True
    n.configure({"quiet_start": 5, "quiet_end": 5})                  # equal = off
    assert n.notify("health", "a", "b", key="2c")["toast"] is True
    n.configure({"quiet_start": None, "kinds": {"health": False}})
    assert n.notify("health", "a", "b", key="3")["toast"] is False
    n.configure({"kinds": {"health": True}, "toasts": False})
    assert n.notify("health", "a", "b", key="4")["toast"] is False
    assert len(sent) == 3
    (tmp_path / "x").mkdir()
    m, msent = _n(tmp_path / "x", clock, capable=False)
    assert m.notify("health", "a", "b")["toast"] is False and msent == []


def test_background_send_uses_a_thread(tmp_path):
    got = []
    n = Notifier(tmp_path / "n.json", "http://h/", toast_capable=True,
                 sender=lambda *a: got.append(a), clock=time.time)
    n.notify("health", "t", "b")
    for _ in range(1000):                     # generous: the suite may run on a loaded machine
        if got:
            break
        time.sleep(0.01)
    assert got and got[0][2] == "http://h/#home"


def test_configure_validates(tmp_path):
    n, _ = _n(tmp_path, [1.0])
    for bad in ({"toasts": 1}, {"min_interval_s": 5}, {"min_interval_s": True},
                {"quiet_start": 24}, {"quiet_end": True}, {"kinds": {"x": True}},
                {"kinds": {"score": "yes"}}, {"kinds": []}, {"other": 1}):
        with pytest.raises(ValueError):
            n.configure(bad)
    d = n.configure({"min_interval_s": 600, "kinds": {"score": False}})
    assert d["min_interval_s"] == 600 and d["kinds"]["score"] is False
    assert d["kinds"]["review"] is True and set(d["kind_labels"]) == set(KINDS)


def test_load_tolerates_corrupt_or_partial_state(tmp_path):
    (tmp_path / "n.json").write_text("{bad")
    n, _ = _n(tmp_path, [1.0])
    assert n.items()["items"] == []
    (tmp_path / "n.json").write_text('{"toasts": false, "kinds": {"score": false}}')
    n, _ = _n(tmp_path, [1.0])
    assert n.settings["toasts"] is False and n.settings["kinds"]["review"] is True


def test_save_failure_is_logged(tmp_path, caplog):
    n = Notifier(tmp_path / "missing-dir" / "n.json", "http://h/", toast_capable=False,
                 clock=lambda: 1.0)
    n.notify("health", "t", "b")
    assert "could not save state" in caplog.text


def test_toast_argv_encodes_the_fixed_script():
    argv = toast_argv()
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert argv[0] == "powershell.exe" and "-EncodedCommand" in argv
    assert "SYSPERF_TOAST_TITLE" in script and "SecurityElement]::Escape" in script


def test_send_toast_passes_text_via_environment_only():
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return SimpleNamespace(returncode=0)
    assert send_toast("T" * 200, "<b>body</b>", "http://127.0.0.1:5056/#home", run=run)
    assert seen["env"]["SYSPERF_TOAST_TITLE"] == "T" * 120
    assert seen["env"]["SYSPERF_TOAST_BODY"] == "<b>body</b>"
    assert "<b>body</b>" not in " ".join(seen["argv"])
    assert send_toast("t", "b", "u", run=lambda *a, **k: SimpleNamespace(returncode=1)) is False


def test_send_toast_never_raises(caplog):
    def run(*a, **k):
        raise OSError("no powershell")
    assert send_toast("t", "b", "u", run=run) is False
    assert "toast failed" in caplog.text
    assert notify.send_toast.__defaults__[0] is notify.run_hidden
