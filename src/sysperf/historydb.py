"""Sample history in SQLite: raw samples for recent days, hourly rollups for long.

Before 4.1 history was a JSONL file trimmed back to the last 240 samples once
it passed ~12 MB, so the dashboard forgot everything older than a few hours of
the trimmed tail and every write appended to a growing text file. This store
keeps:

* ``samples`` — every raw sample (the full JSON) for ``raw_days`` days, which
  is what the collector reloads at start-up and what the scorer replays;
* ``hourly`` — count / sum / min / max per numeric metric per hour for
  ``rollup_days`` days, which is what long-range charts read.

Writes are a single transaction per sample, in WAL mode, so a crash mid-write
can never leave a half-written line the way an append to a text file can.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

HOUR = 3600


class HistoryStore:
    def __init__(self, path: Path, rollup_keys: Iterable[str], *, raw_days: int = 14,
                 rollup_days: int = 400, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.rollup_keys = tuple(dict.fromkeys(rollup_keys))
        self.raw_days = raw_days
        self.rollup_days = rollup_days
        self.clock = clock
        self._lock = threading.Lock()
        self._writes = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (t REAL PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hourly (
                hour INTEGER NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL,
                sum REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL,
                PRIMARY KEY (hour, key));
        """)
        self._db.commit()

    # ---------------------------------------------------------------- write --
    def append(self, sample: dict) -> None:
        t = sample.get("_t")
        if not isinstance(t, (int, float)):
            return
        with self._lock:
            self._insert(sample, t)
            self._db.commit()
            self._writes += 1
            if self._writes % 500 == 0:
                self._prune()

    def _insert(self, sample: dict, t: float) -> None:
        self._db.execute("INSERT OR REPLACE INTO samples (t, data) VALUES (?, ?)",
                         (t, json.dumps(sample)))
        hour = int(t // HOUR) * HOUR
        for key in self.rollup_keys:
            v = sample.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            self._db.execute(
                "INSERT INTO hourly (hour, key, n, sum, min, max) VALUES (?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(hour, key) DO UPDATE SET n = n + 1, sum = sum + excluded.sum, "
                "min = MIN(min, excluded.min), max = MAX(max, excluded.max)",
                (hour, key, float(v), float(v), float(v)))

    def prune(self) -> dict:
        with self._lock:
            return self._prune()

    def _prune(self) -> dict:
        now = self.clock()
        raw = self._db.execute("DELETE FROM samples WHERE t < ?",
                               (now - self.raw_days * 86400,)).rowcount
        roll = self._db.execute("DELETE FROM hourly WHERE hour < ?",
                                (now - self.rollup_days * 86400,)).rowcount
        self._db.commit()
        return {"samples": raw, "hourly": roll}

    def migrate_jsonl(self, path: Path) -> int:
        """Import a pre-4.1 history.jsonl once, then rename it ``.migrated``."""
        path = Path(path)
        if not path.exists():
            return 0
        n = 0
        with self._lock:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if isinstance(s, dict) and isinstance(s.get("_t"), (int, float)):
                    self._insert(s, s["_t"])
                    n += 1
            self._db.commit()
        path.replace(path.with_name(path.name + ".migrated"))
        log.info("migrated %d samples from %s into SQLite", n, path.name)
        return n

    # ----------------------------------------------------------------- read --
    def recent(self, n: int) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM samples ORDER BY t DESC LIMIT ?", (n,)).fetchall()
        out = []
        for (data,) in reversed(rows):
            try:
                out.append(json.loads(data))
            except ValueError:
                continue
        return out

    def series(self, key: str, hours: int) -> list:
        """Hourly ``{t, avg, min, max, n}`` points for ``key`` over ``hours``."""
        since = int((self.clock() - hours * HOUR) // HOUR) * HOUR
        with self._lock:
            rows = self._db.execute(
                "SELECT hour, n, sum, min, max FROM hourly WHERE key = ? AND hour >= ? "
                "ORDER BY hour", (key, since)).fetchall()
        return [{"t": h, "avg": round(s / n, 3), "min": mn, "max": mx, "n": n}
                for h, n, s, mn, mx in rows]

    def stats(self) -> dict:
        with self._lock:
            raw_n, oldest = self._db.execute("SELECT COUNT(*), MIN(t) FROM samples").fetchone()
            hours, first_hour = self._db.execute(
                "SELECT COUNT(DISTINCT hour), MIN(hour) FROM hourly").fetchone()
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*")
                   if p.is_file())
        return {"raw_samples": raw_n, "oldest_sample": oldest, "hours": hours,
                "oldest_hour": first_hour, "raw_days": self.raw_days,
                "rollup_days": self.rollup_days, "bytes": size}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_store(path: Path, rollup_keys: Iterable[str], legacy_jsonl: Path | None = None,
               **kw) -> HistoryStore:
    """Open the store and fold in a legacy JSONL history the first time."""
    store = HistoryStore(path, rollup_keys, **kw)
    if legacy_jsonl is not None:
        try:
            store.migrate_jsonl(legacy_jsonl)
        except OSError as exc:
            log.warning("could not migrate %s: %s", legacy_jsonl, exc)
    return store
