"""SQLite history: raw samples, hourly rollups, retention and JSONL migration."""
import json

from sysperf.historydb import HOUR, HistoryStore, open_store


def _store(tmp_path, clock, **kw):
    return HistoryStore(tmp_path / "h.db", ["cpu_pct", "available_mb"],
                        clock=lambda: clock[0], **kw)


def test_append_recent_and_hourly_rollup(tmp_path):
    clock = [10 * HOUR]
    s = _store(tmp_path, clock)
    base = 9 * HOUR
    for i, cpu in enumerate([10, 30, 20]):
        s.append({"_t": base + i * 60, "cpu_pct": cpu, "available_mb": 8000,
                  "flag": True, "note": "x"})
    s.append({"_t": base + HOUR, "cpu_pct": 50, "available_mb": True})   # bool ignored
    s.append({"cpu_pct": 1})                                             # no time: dropped
    assert [r["cpu_pct"] for r in s.recent(10)] == [10, 30, 20, 50]
    assert [r["cpu_pct"] for r in s.recent(2)] == [20, 50]
    pts = s.series("cpu_pct", 24)
    assert pts[0] == {"t": base, "avg": 20.0, "min": 10.0, "max": 30.0, "n": 3}
    assert pts[1]["avg"] == 50.0
    assert len(s.series("available_mb", 24)) == 1            # True was not counted
    st = s.stats()
    assert st["raw_samples"] == 4 and st["hours"] == 2 and st["bytes"] > 0


def test_retention_prunes_old_raw_and_rollups(tmp_path):
    clock = [100 * 86400.0]
    s = _store(tmp_path, clock, raw_days=1, rollup_days=10)
    s.append({"_t": clock[0] - 2 * 86400, "cpu_pct": 1})
    s.append({"_t": clock[0] - 20 * 86400, "cpu_pct": 2})
    s.append({"_t": clock[0], "cpu_pct": 3})
    assert s.prune() == {"samples": 2, "hourly": 1}
    assert [r["cpu_pct"] for r in s.recent(5)] == [3]


def test_prune_runs_automatically_every_500_writes(tmp_path):
    clock = [100 * 86400.0]
    s = _store(tmp_path, clock, raw_days=1)
    s.append({"_t": 1.0, "cpu_pct": 1})                      # ancient
    for i in range(499):
        s.append({"_t": clock[0] + i, "cpu_pct": 2})
    assert all(r["cpu_pct"] == 2 for r in s.recent(1000))


def test_corrupt_row_is_skipped(tmp_path):
    s = _store(tmp_path, [0.0])
    s.append({"_t": 1.0, "cpu_pct": 1})
    s._db.execute("INSERT INTO samples (t, data) VALUES (2.0, 'not json')")
    s._db.commit()
    assert [r["cpu_pct"] for r in s.recent(5)] == [1]


def test_migrates_jsonl_once_and_renames_it(tmp_path):
    legacy = tmp_path / "history.jsonl"
    legacy.write_text("\n".join([json.dumps({"_t": 5.0, "cpu_pct": 9}), "garbage",
                                 json.dumps([1, 2]), json.dumps({"no": "time"})]))
    s = open_store(tmp_path / "h.db", ["cpu_pct"], legacy_jsonl=legacy)
    assert [r["cpu_pct"] for r in s.recent(5)] == [9]
    assert not legacy.exists() and (tmp_path / "history.jsonl.migrated").exists()
    assert s.migrate_jsonl(legacy) == 0                      # nothing left to do
    s.close()
    again = open_store(tmp_path / "h.db", ["cpu_pct"], legacy_jsonl=legacy)
    assert len(again.recent(5)) == 1                         # data persisted


def test_migration_failure_is_logged_not_fatal(tmp_path, caplog, monkeypatch):
    legacy = tmp_path / "history.jsonl"
    legacy.write_text(json.dumps({"_t": 1.0, "cpu_pct": 1}))

    def boom(self, path):
        raise OSError("locked")
    monkeypatch.setattr(HistoryStore, "migrate_jsonl", boom)
    s = open_store(tmp_path / "h.db", ["cpu_pct"], legacy_jsonl=legacy)
    assert s.recent(5) == [] and "could not migrate" in caplog.text
