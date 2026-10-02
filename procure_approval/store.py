"""SQLite 连接与 schema 管理。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    category    TEXT NOT NULL,
    quarter     TEXT NOT NULL,
    limit_cents INTEGER NOT NULL CHECK (limit_cents > 0),
    PRIMARY KEY (category, quarter)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id            TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    status           TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,
    category         TEXT NOT NULL,
    quarter          TEXT NOT NULL,
    created_by       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    po_id        TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    category     TEXT NOT NULL,
    quarter      TEXT NOT NULL,
    PRIMARY KEY (po_id, revision)
);

CREATE TABLE IF NOT EXISTS approvals (
    po_id    TEXT NOT NULL,
    revision INTEGER NOT NULL,
    level    INTEGER NOT NULL,
    approver TEXT NOT NULL,
    role     TEXT NOT NULL,
    PRIMARY KEY (po_id, revision, level)
);

CREATE TABLE IF NOT EXISTS budget_ledger (
    category     TEXT NOT NULL,
    quarter      TEXT NOT NULL,
    spent_cents  INTEGER NOT NULL DEFAULT 0,
    notified_80  INTEGER NOT NULL DEFAULT 0,
    notified_100 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (category, quarter)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,
    quarter     TEXT NOT NULL,
    threshold   INTEGER NOT NULL,
    spent_cents INTEGER NOT NULL,
    limit_cents INTEGER NOT NULL,
    po_id       TEXT NOT NULL,
    revision    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    caller       TEXT NOT NULL,
    idem_key     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (caller, idem_key)
);
"""


def open_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
