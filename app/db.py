"""SQLite persistence. One connection, one re-entrant lock, nestable transactions.

Single-process by design (see README "Limitations"); the lock serialises all workflow
mutations, which is what gives us idempotent callback handling without row locks.
"""
import contextlib
import sqlite3
import threading
from pathlib import Path

from . import config

LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None
_depth = 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  created_at REAL, updated_at REAL, state_entered_at REAL,
  form_enc TEXT NOT NULL,            -- encrypted JSON (PAN, GSTIN, bank a/c ...)
  simulate TEXT NOT NULL DEFAULT '{}',
  flags TEXT NOT NULL DEFAULT '{}',
  issues TEXT NOT NULL DEFAULT '{}', -- what we are asking the merchant for
  pan_fp TEXT, gstin_fp TEXT, bank_fp TEXT, name_norm TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, app_id TEXT NOT NULL, doc_type TEXT NOT NULL,
  filename TEXT, content_type TEXT, sha256 TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | SUPERSEDED
  extraction_enc TEXT, extraction_view TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS verifications (
  id TEXT PRIMARY KEY, app_id TEXT NOT NULL, provider TEXT NOT NULL,
  input_hash TEXT NOT NULL, attempt INTEGER, seq INTEGER,
  status TEXT NOT NULL,   -- PENDING|SUCCEEDED|FAILED|TIMED_OUT|ERROR|STALE
  result TEXT, deadline REAL, created_at REAL, resolved_at REAL, late INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ver_app ON verifications(app_id, provider);
CREATE TABLE IF NOT EXISTS callback_events (
  event_id TEXT PRIMARY KEY, request_id TEXT, provider TEXT,
  received_at REAL, dup_count INTEGER DEFAULT 0, outcome TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, kind TEXT, key TEXT,
  payload TEXT, run_at REAL, status TEXT DEFAULT 'PENDING'
);
CREATE TABLE IF NOT EXISTS review_tasks (
  id TEXT PRIMARY KEY, app_id TEXT, reasons TEXT, status TEXT DEFAULT 'OPEN',
  created_at REAL, resolved_at REAL, decision TEXT, reviewer TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS dead_letters (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, app_id TEXT, payload TEXT,
  reason TEXT, created_at REAL, status TEXT DEFAULT 'OPEN'
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, ts REAL, actor TEXT,
  event TEXT, from_state TEXT, to_state TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_app ON audit(app_id);
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id TEXT PRIMARY KEY, app_id TEXT UNIQUE, created_at REAL, payout_account_masked TEXT
);
CREATE TABLE IF NOT EXISTS sim_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, request_id TEXT,
  body TEXT, signature TEXT, due_at REAL, delivered INTEGER DEFAULT 0
);
"""


def init(path=None) -> None:
    global _conn, _depth
    with LOCK:
        if _conn is not None:
            _conn.close()
        p = str(path or config.DB_PATH)
        if p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _depth = 0


@contextlib.contextmanager
def tx():
    """Nestable transaction: only the outermost level BEGINs/COMMITs; any exception rolls back."""
    global _depth
    with LOCK:
        if _depth == 0:
            _conn.execute("BEGIN IMMEDIATE")
        _depth += 1
        try:
            yield
        except BaseException:
            _depth -= 1
            if _depth == 0:
                _conn.execute("ROLLBACK")
            raise
        else:
            _depth -= 1
            if _depth == 0:
                _conn.execute("COMMIT")


def q(sql: str, args=()) -> list[dict]:
    with LOCK:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql: str, args=()):
    with LOCK:
        return _conn.execute(sql, args)
