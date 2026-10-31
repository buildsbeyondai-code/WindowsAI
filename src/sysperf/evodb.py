"""
Durable store for the 4.0 learning and evolution subsystems (SQLite).

* Versioned schema with forward-only migrations (``schema_version`` table).
* Retention limits so the database cannot grow without bound.
* A **hash-chained audit log**: every row stores the SHA-256 of the previous
  row plus its own content, so editing or deleting any past entry breaks
  ``verify_audit()``. That is tamper-*evident*, not tamper-proof — someone with
  write access can rewrite the whole chain — and is documented as such.
* Crash recovery: the database runs in WAL mode; cycles left ``running`` by a
  crash are marked ``interrupted`` on open (see ``recover()``).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

MIGRATIONS = [
    # 1 — evolution cycles, candidates, audit chain
    """
    CREATE TABLE cycles (
        id TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL,
        trigger TEXT NOT NULL, state TEXT NOT NULL, problem TEXT,
        model TEXT, provider TEXT, explanation TEXT, error TEXT
    );
    CREATE TABLE candidates (
        id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES cycles(id),
        created REAL NOT NULL, target TEXT, title TEXT, rationale TEXT,
        files TEXT, diff TEXT, protected INTEGER NOT NULL DEFAULT 0,
        static_ok INTEGER, tests_ok INTEGER, security_ok INTEGER,
        bench TEXT, decision TEXT, reason TEXT, branch TEXT, commit_sha TEXT,
        review_state TEXT NOT NULL DEFAULT 'none', reviewed_by TEXT, reviewed_at REAL,
        details TEXT
    );
    CREATE INDEX idx_cand_cycle ON candidates(cycle_id);
    CREATE TABLE audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        actor TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
        prev_hash TEXT NOT NULL, hash TEXT NOT NULL
    );
    """,
    # 2 — LLM call telemetry and optimisation-action executions
    """
    CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, provider TEXT,
        model TEXT, ok INTEGER NOT NULL, error_kind TEXT, error TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, latency_s REAL
    );
    CREATE TABLE action_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action_id TEXT NOT NULL,
        mode TEXT NOT NULL, authorized INTEGER NOT NULL, reason TEXT,
        ok INTEGER, metric TEXT, before REAL, after REAL, verdict TEXT,
        rolled_back INTEGER NOT NULL DEFAULT 0, output TEXT
    );
    CREATE INDEX idx_action_runs ON action_runs(action_id, ts);
    """,
]

RETENTION = {"llm_calls": 5000, "action_runs": 20000, "cycles": 2000}
GENESIS = "0" * 64


class EvoDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = self._open()
        self.migrate()

    def _open(self) -> sqlite3.Connection:
        conn = None
        try:
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError:
            if conn is not None:
                conn.close()
            # Corrupt file: keep it for forensics, start clean. Monitoring must
            # never be taken down by the learning store.
            bad = self.path.with_suffix(f".corrupt-{int(time.time())}")
            self.path.replace(bad)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self.recovered_from = bad.name
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ migrations
    def version(self) -> int:
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
            row = self.conn.execute("SELECT MAX(v) FROM schema_version").fetchone()
            return int(row[0] or 0)

    def migrate(self) -> int:
        with self._lock:
            cur = self.version()
            for i, sql in enumerate(MIGRATIONS[cur:], start=cur + 1):
                with self.conn:
                    self.conn.executescript("BEGIN;" + sql + f"INSERT INTO schema_version VALUES ({i});COMMIT;")
            return self.version()

    # ----------------------------------------------------------------- audit
    @staticmethod
    def _hash(prev: str, ts: float, actor: str, event: str, payload: str) -> str:
        return hashlib.sha256(f"{prev}|{ts!r}|{actor}|{event}|{payload}".encode()).hexdigest()

    def audit(self, actor: str, event: str, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, default=str)
        with self._lock, self.conn:
            row = self.conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            ts = time.time()
            h = self._hash(prev, ts, actor, event, body)
            self.conn.execute("INSERT INTO audit(ts,actor,event,payload,prev_hash,hash) "
                              "VALUES (?,?,?,?,?,?)", (ts, actor, event, body, prev, h))
            return h

    def verify_audit(self) -> dict:
        prev, n = GENESIS, 0
        with self._lock:
            for r in self.conn.execute("SELECT * FROM audit ORDER BY seq"):
                if r["prev_hash"] != prev or r["hash"] != self._hash(
                        prev, r["ts"], r["actor"], r["event"], r["payload"]):
                    return {"ok": False, "broken_at": r["seq"], "checked": n}
                prev, n = r["hash"], n + 1
        return {"ok": True, "checked": n, "head": prev}

    def audit_tail(self, limit: int = 50) -> list:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT seq,ts,actor,event,payload FROM audit ORDER BY seq DESC LIMIT ?",
                (limit,))]

    # ---------------------------------------------------------------- cycles
    def start_cycle(self, cid: str, trigger: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO cycles(id,started,trigger,state) VALUES (?,?,?,?)",
                              (cid, time.time(), trigger, "analyzing"))

    def update_cycle(self, cid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self.conn:
            self.conn.execute(f"UPDATE cycles SET {cols} WHERE id=?", (*fields.values(), cid))

    def cycles(self, limit: int = 50) -> list:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM cycles ORDER BY started DESC LIMIT ?", (limit,))]

    def cycles_since(self, ts: float) -> int:
        """Code-lane cycles started since ts (what max_per_day limits).

        Component upgrades (provider 3pp) use no model and are made once per
        version, so they do not spend the daily allowance.
        """
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM cycles WHERE started>=? AND "
                                     "state!='skipped' AND IFNULL(provider,'')!='3pp'",
                                     (ts,)).fetchone()[0]

    def recover(self) -> int:
        """Mark cycles a crash left mid-flight as interrupted."""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE cycles SET state='interrupted', finished=?, explanation="
                "'process stopped mid-cycle; no candidate from it was retained' "
                "WHERE finished IS NULL", (time.time(),))
            return cur.rowcount

    # ------------------------------------------------------------ candidates
    def add_candidate(self, row: dict) -> None:
        cols = ", ".join(row)
        with self._lock, self.conn:
            self.conn.execute(f"INSERT INTO candidates({cols}) VALUES "
                              f"({', '.join('?' * len(row))})", tuple(row.values()))

    def update_candidate(self, cand_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self.conn:
            self.conn.execute(f"UPDATE candidates SET {cols} WHERE id=?",
                              (*fields.values(), cand_id))

    def candidate(self, cand_id: str) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM candidates WHERE id=?", (cand_id,)).fetchone()
            return dict(r) if r else None

    def candidates(self, limit: int = 100) -> list:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id,cycle_id,created,target,title,rationale,files,protected,static_ok,"
                "tests_ok,security_ok,bench,decision,reason,branch,commit_sha,review_state,"
                "reviewed_at FROM candidates ORDER BY created DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------- telemetry
    def record_llm(self, c, err, provider) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO llm_calls(ts,provider,model,ok,error_kind,error,input_tokens,"
                "output_tokens,cost_usd,latency_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), getattr(provider, "name", "?"),
                 getattr(c, "model", None) or getattr(provider, "model", None),
                 0 if err else 1, getattr(err, "kind", None), str(err) if err else None,
                 getattr(c, "input_tokens", None), getattr(c, "output_tokens", None),
                 getattr(c, "cost_usd", None), getattr(c, "latency_s", None)))

    def llm_summary(self, since: float) -> dict:
        with self._lock:
            r = self.conn.execute(
                "SELECT COUNT(*), SUM(ok), SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),"
                " SUM(cost_usd), SUM(cost_usd IS NULL AND ok=1) FROM llm_calls WHERE ts>=?",
                (since,)).fetchone()
            errs = [dict(x) for x in self.conn.execute(
                "SELECT ts,provider,model,error_kind,error FROM llm_calls WHERE ok=0 "
                "ORDER BY ts DESC LIMIT 5")]
        return {"calls": r[0] or 0, "ok": r[1] or 0, "tokens": r[2] or 0,
                "cost_usd": round(r[3], 4) if r[3] is not None else None,
                "calls_without_price": r[4] or 0, "recent_errors": errs}

    def record_action(self, **row) -> int:
        cols = ", ".join(row)
        with self._lock, self.conn:
            cur = self.conn.execute(f"INSERT INTO action_runs(ts,{cols}) VALUES "
                                    f"(?,{', '.join('?' * len(row))})",
                                    (time.time(), *row.values()))
            return cur.lastrowid

    def update_action(self, run_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self.conn:
            self.conn.execute(f"UPDATE action_runs SET {cols} WHERE id=?",
                              (*fields.values(), run_id))

    def action_runs(self, limit: int = 100, action_id: str | None = None) -> list:
        q, args = "SELECT * FROM action_runs", []
        if action_id:
            q, args = q + " WHERE action_id=?", [action_id]
        with self._lock:
            return [dict(r) for r in self.conn.execute(q + " ORDER BY ts DESC LIMIT ?",
                                                       (*args, limit))]

    def prune(self) -> dict:
        """Apply RETENTION. Audit rows are never pruned (it would break the chain)."""
        out = {}
        with self._lock, self.conn:
            for table, keep in RETENTION.items():
                key = "started" if table == "cycles" else "ts"
                if table == "cycles":
                    self.conn.execute(
                        f"DELETE FROM candidates WHERE cycle_id IN (SELECT id FROM cycles "
                        f"ORDER BY {key} DESC LIMIT -1 OFFSET ?)", (keep,))
                cur = self.conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} "
                    f"ORDER BY {key} DESC LIMIT -1 OFFSET ?)", (keep,))
                out[table] = cur.rowcount
        return out
