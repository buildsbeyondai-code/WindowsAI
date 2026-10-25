"""Service self-diagnostics: error capture, liveness verdicts, footprint, rotation."""
import io
import logging
import os
import threading
from types import SimpleNamespace

from sysperf.health import ErrorBuffer, HealthMonitor, footprint, rotate_log, rss_mb


def _logger(buf):
    lg = logging.getLogger("sysperf.test.health")
    lg.handlers[:] = [buf]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    return lg


def test_buffer_keeps_warnings_collapses_repeats_and_records_exceptions():
    clock = [100.0]
    buf = ErrorBuffer(capacity=3, clock=lambda: clock[0])
    lg = _logger(buf)
    lg.info("ignored")
    lg.warning("disk slow\nsecond line")
    clock[0] = 110
    lg.error("disk slow\nsecond line")
    try:
        raise ValueError("bad value")
    except ValueError:
        lg.exception("hook failed")
    lg.warning("")
    rows = buf.items()
    slow = next(r for r in rows if r["message"] == "disk slow")
    assert slow["count"] == 2 and slow["level"] == "ERROR" and slow["first"] == 100
    assert any("ValueError: bad value" in r["message"] for r in rows)
    assert len(rows) == 3                                   # capacity respected
    assert buf.items(since=105) == [r for r in rows if r["last"] >= 105]
    buf.clear()
    assert buf.items() == []


def test_buffer_never_raises_on_a_broken_record():
    buf = ErrorBuffer()
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "%d", ("not a number",), None)
    buf.emit(rec)
    assert buf.items()[0]["message"] == "%d"


def test_monitor_verdicts():
    clock = [1000.0]
    buf = ErrorBuffer(clock=lambda: clock[0])
    mon = HealthMonitor(buf, clock=lambda: clock[0], started_at=1000.0)
    last = {"v": None}
    mon.add_probe("collector", lambda: last["v"], 600, "Data collection")
    mon.add_probe("broken", lambda: 1 / 0, 600, "Broken probe")
    alive = threading.Thread(target=lambda: None)
    mon.register_thread("dead", alive)                      # never started -> not alive
    assert mon.check()["status"] == "error"                 # dead worker
    mon._threads.clear()
    assert mon.check()["status"] == "ok"                    # nothing yet, but just started
    clock[0] += 700
    c = mon.check()
    assert c["status"] == "error" and "since start" in c["problems"][0]
    last["v"] = clock[0] - 900
    assert "no update for 15 min" in mon.check()["problems"][0]
    last["v"] = clock[0] - 10
    mon._probes.pop("broken")
    _logger(buf).error("one error")
    c = mon.check()
    assert c["status"] == "degraded" and c["errors_1h"] == 1 and c["warnings_1h"] == 0


def test_monitor_defaults_started_at_to_now():
    mon = HealthMonitor(ErrorBuffer(), clock=lambda: 42.0)
    assert mon.started_at == 42.0


def test_footprint_and_rss(tmp_path):
    fp = footprint(0.0, clock=lambda: 100.0,
                   times=lambda: SimpleNamespace(user=3.0, system=1.0), rss=lambda: 55.5)
    assert fp["cpu_s"] == 4.0 and fp["uptime_s"] == 100 and fp["rss_mb"] == 55.5
    assert fp["avg_cpu_pct"] == round(4.0 / (os.cpu_count() or 1), 2)
    statm = tmp_path / "statm"
    statm.write_text("100 256 0 0")
    if os.name != "nt":                         # Windows reads RSS from the process API
        assert rss_mb(statm) == round(256 * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
        assert rss_mb(tmp_path / "missing") is None
        statm.write_text("bad")
        assert rss_mb(statm) is None
    assert footprint(0.0)["threads"] >= 1


def test_rotate_log_in_place_and_via_stream(tmp_path):
    log = tmp_path / "sysperf-runtime.log"
    assert rotate_log(tmp_path / "missing.log") is False
    log.write_text("x" * 50)
    assert rotate_log(log, max_bytes=100) is False
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    assert rotate_log(log, max_bytes=100, keep_bytes=30) is True
    kept = log.read_text()
    assert kept.startswith("--- log rotated") and kept.rstrip().endswith("line 99")
    assert (tmp_path / "sysperf-runtime.log.1").read_text().startswith("line 0")
    with log.open("a+") as fh:
        fh.write("y" * 300)
        fh.flush()
        assert rotate_log(log, max_bytes=100, keep_bytes=10, stream=fh) is True
    assert log.read_text().startswith("--- log rotated")


def test_rotate_log_without_newline_in_tail(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("z" * 500)
    assert rotate_log(log, max_bytes=100, keep_bytes=20, stream=io.StringIO()) is True
