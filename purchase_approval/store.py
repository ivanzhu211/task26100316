"""SQLite 持久化层：schema、事务与原子读写。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterator, Optional, Tuple

from .models import (
    ApprovalRecord,
    BudgetView,
    Decision,
    Notification,
    OrderStatus,
    PurchaseOrderView,
    Receipt,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    po_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    category TEXT NOT NULL,
    quarter TEXT NOT NULL,
    PRIMARY KEY (po_id, revision)
);
CREATE TABLE IF NOT EXISTS approvals (
    po_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    level INTEGER NOT NULL,
    approver_id TEXT NOT NULL,
    PRIMARY KEY (po_id, revision, level)
);
CREATE TABLE IF NOT EXISTS budgets (
    category TEXT NOT NULL,
    quarter TEXT NOT NULL,
    limit_cents INTEGER NOT NULL,
    used_cents INTEGER NOT NULL DEFAULT 0,
    notified_80 INTEGER NOT NULL DEFAULT 0,
    notified_100 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (category, quarter)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    quarter TEXT NOT NULL,
    threshold_percent INTEGER NOT NULL,
    used_cents INTEGER NOT NULL,
    limit_cents INTEGER NOT NULL,
    UNIQUE (category, quarter, threshold_percent)
);
CREATE TABLE IF NOT EXISTS idempotency (
    caller_id TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (caller_id, idem_key)
);
"""


def receipt_to_json(receipt: Receipt) -> str:
    data = asdict(receipt)
    if data["decision"] is not None:
        data["decision"] = data["decision"].value
    if data["status"] is not None:
        data["status"] = data["status"].value
    return json.dumps(data, sort_keys=True)


def receipt_from_json(payload: str) -> Receipt:
    data = json.loads(payload)
    if data["decision"] is not None:
        data["decision"] = Decision(data["decision"])
    if data["status"] is not None:
        data["status"] = OrderStatus(data["status"])
    return Receipt(**data)


class Store:
    """对单个 SQLite 文件的访问；每个实例持有自己的连接，可跨线程各建实例。"""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """写事务默认 BEGIN IMMEDIATE，保证串行化写；读事务用普通 BEGIN。"""
        self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- 预算 ----
    def init_budget(self, category: str, quarter: str, limit_cents: int) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO budgets (category, quarter, limit_cents) VALUES (?, ?, ?)",
                (category, quarter, limit_cents),
            )

    def get_budget(self, conn: sqlite3.Connection, category: str, quarter: str) -> Optional[BudgetView]:
        row = conn.execute(
            "SELECT limit_cents, used_cents FROM budgets WHERE category=? AND quarter=?",
            (category, quarter),
        ).fetchone()
        if row is None:
            return None
        return BudgetView(category=category, quarter=quarter, limit_cents=row[0], used_cents=row[1])

    # ---- 采购单与修订 ----
    def get_order_row(self, conn: sqlite3.Connection, po_id: str):
        return conn.execute(
            "SELECT current_revision, status FROM purchase_orders WHERE po_id=?", (po_id,)
        ).fetchone()

    def get_revision_row(self, conn: sqlite3.Connection, po_id: str, revision: int):
        return conn.execute(
            "SELECT amount_cents, category, quarter FROM revisions WHERE po_id=? AND revision=?",
            (po_id, revision),
        ).fetchone()

    def get_approvals(self, conn: sqlite3.Connection, po_id: str, revision: int) -> Tuple[ApprovalRecord, ...]:
        rows = conn.execute(
            "SELECT level, approver_id FROM approvals WHERE po_id=? AND revision=? ORDER BY level",
            (po_id, revision),
        ).fetchall()
        return tuple(ApprovalRecord(po_id=po_id, revision=revision, level=r[0], approver_id=r[1]) for r in rows)

    # ---- 通知 ----
    def get_notifications(self, conn: sqlite3.Connection, category: str, quarter: str) -> Tuple[Notification, ...]:
        rows = conn.execute(
            "SELECT threshold_percent, used_cents, limit_cents FROM notifications "
            "WHERE category=? AND quarter=? ORDER BY threshold_percent",
            (category, quarter),
        ).fetchall()
        return tuple(
            Notification(category=category, quarter=quarter, threshold_percent=r[0],
                         used_cents=r[1], limit_cents=r[2])
            for r in rows
        )

    # ---- 幂等 ----
    def find_receipt(self, conn: sqlite3.Connection, caller_id: str, idem_key: str):
        row = conn.execute(
            "SELECT request_fingerprint, receipt_json FROM idempotency WHERE caller_id=? AND idem_key=?",
            (caller_id, idem_key),
        ).fetchone()
        if row is None:
            return None
        return row[0], receipt_from_json(row[1])

    def save_receipt(self, conn: sqlite3.Connection, caller_id: str, idem_key: str,
                     fingerprint: str, receipt: Receipt) -> None:
        conn.execute(
            "INSERT INTO idempotency (caller_id, idem_key, request_fingerprint, receipt_json) "
            "VALUES (?, ?, ?, ?)",
            (caller_id, idem_key, fingerprint, receipt_to_json(receipt)),
        )
