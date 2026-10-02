"""采购单审批库主入口。

并发模型:所有写操作(提交/改报/审批)在单个 BEGIN IMMEDIATE 事务内完成
"查幂等 -> 校验 -> 改状态 -> 写回执",SQLite 写锁把并发写串行化,
因此并发结果必然等价于某个串行执行顺序。读操作在单个读事务内完成,
保证一次查看看到同一个已提交时刻的快照。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Callable, List, Optional, Set

from .store import open_connection
from .types import (
    ApprovalRecord,
    BudgetView,
    Notification,
    POStatus,
    PurchaseOrderView,
    Receipt,
    Role,
)

_AUTO_LIMIT = 500_000          # 不足 5000.00 美元自动批准
_SINGLE_LIMIT = 1_000_000      # 5000.00 ~ 10000.00 单人(采购负责人或财务经理)
_FM_ONLY_LIMIT = 2_500_000     # 10000.00 ~ 25000.00 仅财务经理

_WRITE_RETRIES = 50


def _required_levels(amount_cents: int) -> List[Set[Role]]:
    """每个层级允许的角色集合;空列表表示自动批准。"""
    if amount_cents < _AUTO_LIMIT:
        return []
    if amount_cents <= _SINGLE_LIMIT:
        return [{Role.PURCHASING_LEAD, Role.FINANCE_MANAGER}]
    if amount_cents <= _FM_ONLY_LIMIT:
        return [{Role.FINANCE_MANAGER}]
    return [{Role.PURCHASING_LEAD}, {Role.FINANCE_MANAGER}]


class ProcureLibrary:
    """上层直接调用的库对象;每个线程持有一个实例,共享同一 SQLite 文件。"""

    def __init__(self, db_path: str):
        self._conn = open_connection(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProcureLibrary":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 事务与幂等基础设施
    # ------------------------------------------------------------------
    def _write_txn(self, fn: Callable[[], Receipt]) -> Receipt:
        for attempt in range(_WRITE_RETRIES):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn()
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
                return result
            except sqlite3.OperationalError as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if "locked" in str(exc).lower() and attempt + 1 < _WRITE_RETRIES:
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")

    def _idempotent(self, caller: str, idem_key: str, op: str,
                    params: dict, fn: Callable[[], Receipt]) -> Receipt:
        """同事务内:命中同键同参 -> 回放原回执;同键不同参 -> 拒绝(不落库)。"""
        request_hash = hashlib.sha256(
            json.dumps(params, sort_keys=True).encode("utf-8")).hexdigest()

        def run() -> Receipt:
            row = self._conn.execute(
                "SELECT request_hash, receipt_json FROM idempotency "
                "WHERE caller=? AND idem_key=?",
                (caller, idem_key)).fetchone()
            if row is not None:
                if row["request_hash"] == request_hash:
                    return Receipt.from_json(row["receipt_json"])
                return Receipt(ok=False, op=op, po_id=params.get("po_id", ""),
                               revision=0, status="REJECTED",
                               reason="idempotency_key_conflict")
            receipt = fn()
            self._conn.execute(
                "INSERT INTO idempotency (caller, idem_key, request_hash, receipt_json) "
                "VALUES (?,?,?,?)",
                (caller, idem_key, request_hash, receipt.to_json()))
            return receipt

        return self._write_txn(run)

    # ------------------------------------------------------------------
    # 预算定义(预先给定,不修改)
    # ------------------------------------------------------------------
    def define_budget(self, category: str, quarter: str, limit_cents: int) -> None:
        if not isinstance(limit_cents, int) or limit_cents <= 0:
            raise ValueError("limit_cents must be a positive integer")

        def run() -> Receipt:
            row = self._conn.execute(
                "SELECT limit_cents FROM budgets WHERE category=? AND quarter=?",
                (category, quarter)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO budgets (category, quarter, limit_cents) VALUES (?,?,?)",
                    (category, quarter, limit_cents))
            elif row["limit_cents"] != limit_cents:
                raise ValueError("budget already defined with a different limit")
            return Receipt(ok=True, op="define_budget", po_id="", revision=0,
                           status="OK")

        self._write_txn(run)

    # ------------------------------------------------------------------
    # 提交 / 改报
    # ------------------------------------------------------------------
    def submit(self, *, caller: str, idem_key: str, po_id: str,
               amount_cents: int, category: str, quarter: str) -> Receipt:
        params = {"op": "submit", "po_id": po_id, "amount_cents": amount_cents,
                  "category": category, "quarter": quarter}
        return self._idempotent(
            caller, idem_key, "submit", params,
            lambda: self._do_submit(caller, po_id, amount_cents, category, quarter))

    def resubmit(self, *, caller: str, idem_key: str, po_id: str,
                 expected_revision: int, amount_cents: int,
                 category: str, quarter: str) -> Receipt:
        params = {"op": "resubmit", "po_id": po_id,
                  "expected_revision": expected_revision,
                  "amount_cents": amount_cents, "category": category,
                  "quarter": quarter}
        return self._idempotent(
            caller, idem_key, "resubmit", params,
            lambda: self._do_resubmit(caller, po_id, expected_revision,
                                      amount_cents, category, quarter))

    def _do_submit(self, caller: str, po_id: str, amount_cents: int,
                   category: str, quarter: str) -> Receipt:
        reject = self._validate_new_revision(po_id, amount_cents, category, quarter)
        if reject is not None:
            return reject
        if self._conn.execute(
                "SELECT 1 FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone():
            return self._reject("submit", po_id, 0, "po_already_exists")
        return self._create_revision(caller, po_id, None, amount_cents,
                                     category, quarter)

    def _do_resubmit(self, caller: str, po_id: str, expected_revision: int,
                     amount_cents: int, category: str, quarter: str) -> Receipt:
        reject = self._validate_new_revision(po_id, amount_cents, category, quarter)
        if reject is not None:
            return reject
        po = self._conn.execute(
            "SELECT current_revision, status FROM purchase_orders WHERE po_id=?",
            (po_id,)).fetchone()
        if po is None:
            return self._reject("resubmit", po_id, 0, "po_not_found")
        if po["status"] == POStatus.APPROVED.value:
            return self._reject("resubmit", po_id, po["current_revision"],
                                "already_approved")
        if po["current_revision"] != expected_revision:
            return self._reject("resubmit", po_id, po["current_revision"],
                                "revision_mismatch")
        return self._create_revision(caller, po_id, expected_revision,
                                     amount_cents, category, quarter)

    def _validate_new_revision(self, po_id: str, amount_cents: int,
                               category: str, quarter: str) -> Optional[Receipt]:
        if (not po_id or not category or not quarter
                or not isinstance(amount_cents, int) or isinstance(amount_cents, bool)
                or amount_cents <= 0):
            return self._reject("submit", po_id, 0, "invalid_params")
        if self._conn.execute(
                "SELECT 1 FROM budgets WHERE category=? AND quarter=?",
                (category, quarter)).fetchone() is None:
            return self._reject("submit", po_id, 0, "budget_not_found")
        return None

    def _create_revision(self, caller: str, po_id: str,
                         expected_revision: Optional[int], amount_cents: int,
                         category: str, quarter: str) -> Receipt:
        op = "submit" if expected_revision is None else "resubmit"
        if expected_revision is None:
            revision = 1
            self._conn.execute(
                "INSERT INTO purchase_orders "
                "(po_id, current_revision, status, amount_cents, category, quarter, created_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (po_id, revision, POStatus.PENDING.value, amount_cents,
                 category, quarter, caller))
        else:
            revision = expected_revision + 1
            self._conn.execute(
                "UPDATE purchase_orders SET current_revision=?, amount_cents=?, "
                "category=?, quarter=? WHERE po_id=?",
                (revision, amount_cents, category, quarter, po_id))
        self._conn.execute(
            "INSERT INTO revisions (po_id, revision, amount_cents, category, quarter) "
            "VALUES (?,?,?,?,?)",
            (po_id, revision, amount_cents, category, quarter))
        if not _required_levels(amount_cents):
            self._finalize(po_id, revision, amount_cents, category, quarter)
        status = self._conn.execute(
            "SELECT status FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()["status"]
        return Receipt(ok=True, op=op, po_id=po_id, revision=revision, status=status)

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------
    def approve(self, *, caller: str, idem_key: str, po_id: str,
                revision: int, level: int, approver: str, role: Role) -> Receipt:
        try:
            role = Role(role)
        except ValueError:
            role = None
        params = {"op": "approve", "po_id": po_id, "revision": revision,
                  "level": level, "approver": approver,
                  "role": role.value if role else str(role)}
        return self._idempotent(
            caller, idem_key, "approve", params,
            lambda: self._do_approve(po_id, revision, level, approver, role))

    def _do_approve(self, po_id: str, revision: int, level: int,
                    approver: str, role: Optional[Role]) -> Receipt:
        if role is None or not approver:
            return self._reject("approve", po_id, revision, "invalid_params")
        po = self._conn.execute(
            "SELECT current_revision, status, amount_cents, category, quarter "
            "FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
        if po is None:
            return self._reject("approve", po_id, revision, "po_not_found")
        if revision != po["current_revision"]:
            return self._reject("approve", po_id, revision, "stale_revision")
        if po["status"] == POStatus.APPROVED.value:
            return self._reject("approve", po_id, revision, "already_approved")

        required = _required_levels(po["amount_cents"])
        if not required:
            return self._reject("approve", po_id, revision, "not_approvable")
        if level < 1 or level > len(required):
            return self._reject("approve", po_id, revision, "invalid_level")

        done = self._conn.execute(
            "SELECT level, approver FROM approvals WHERE po_id=? AND revision=? "
            "ORDER BY level", (po_id, revision)).fetchall()
        done_levels = {row["level"] for row in done}
        if level in done_levels:
            return self._reject("approve", po_id, revision, "level_already_approved")
        if level != len(done_levels) + 1:
            return self._reject("approve", po_id, revision, "level_not_ready")
        if role not in required[level - 1]:
            return self._reject("approve", po_id, revision, "role_not_allowed")
        if any(row["approver"] == approver for row in done):
            return self._reject("approve", po_id, revision, "approver_must_differ")

        self._conn.execute(
            "INSERT INTO approvals (po_id, revision, level, approver, role) "
            "VALUES (?,?,?,?,?)",
            (po_id, revision, level, approver, role.value))
        if level == len(required):
            self._finalize(po_id, revision, po["amount_cents"],
                           po["category"], po["quarter"])
        status = self._conn.execute(
            "SELECT status FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()["status"]
        return Receipt(ok=True, op="approve", po_id=po_id, revision=revision,
                       status=status)

    # ------------------------------------------------------------------
    # 完全批准:预算入账 + 阈值通知(与状态变更同事务)
    # ------------------------------------------------------------------
    def _finalize(self, po_id: str, revision: int, amount_cents: int,
                  category: str, quarter: str) -> None:
        self._conn.execute(
            "UPDATE purchase_orders SET status=? WHERE po_id=?",
            (POStatus.APPROVED.value, po_id))
        self._conn.execute(
            "INSERT INTO budget_ledger (category, quarter, spent_cents) VALUES (?,?,0) "
            "ON CONFLICT (category, quarter) DO NOTHING",
            (category, quarter))
        ledger = self._conn.execute(
            "SELECT spent_cents, notified_80, notified_100 FROM budget_ledger "
            "WHERE category=? AND quarter=?", (category, quarter)).fetchone()
        limit = self._conn.execute(
            "SELECT limit_cents FROM budgets WHERE category=? AND quarter=?",
            (category, quarter)).fetchone()["limit_cents"]

        old_spent = ledger["spent_cents"]
        new_spent = old_spent + amount_cents
        self._conn.execute(
            "UPDATE budget_ledger SET spent_cents=? WHERE category=? AND quarter=?",
            (new_spent, category, quarter))

        for threshold, flag_col, notified in (
                (80, "notified_80", ledger["notified_80"]),
                (100, "notified_100", ledger["notified_100"])):
            crossed = (old_spent * 100 < limit * threshold
                       <= new_spent * 100)
            if crossed and not notified:
                self._conn.execute(
                    f"UPDATE budget_ledger SET {flag_col}=1 "
                    "WHERE category=? AND quarter=?", (category, quarter))
                self._conn.execute(
                    "INSERT INTO notifications "
                    "(category, quarter, threshold, spent_cents, limit_cents, po_id, revision) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (category, quarter, threshold, new_spent, limit,
                     po_id, revision))

    # ------------------------------------------------------------------
    # 一致性读
    # ------------------------------------------------------------------
    def get_purchase_order(self, po_id: str) -> Optional[PurchaseOrderView]:
        self._conn.execute("BEGIN")
        try:
            po = self._conn.execute(
                "SELECT current_revision, status, amount_cents, category, quarter "
                "FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
            if po is None:
                return None
            revision = po["current_revision"]
            approvals = [
                ApprovalRecord(level=row["level"], approver=row["approver"],
                               role=row["role"])
                for row in self._conn.execute(
                    "SELECT level, approver, role FROM approvals "
                    "WHERE po_id=? AND revision=? ORDER BY level",
                    (po_id, revision))
            ]
            budget = None
            brow = self._conn.execute(
                "SELECT b.limit_cents, "
                "COALESCE(l.spent_cents, 0) AS spent_cents "
                "FROM budgets b LEFT JOIN budget_ledger l "
                "ON l.category=b.category AND l.quarter=b.quarter "
                "WHERE b.category=? AND b.quarter=?",
                (po["category"], po["quarter"])).fetchone()
            if brow is not None:
                budget = BudgetView(category=po["category"], quarter=po["quarter"],
                                    limit_cents=brow["limit_cents"],
                                    spent_cents=brow["spent_cents"])
            notifications = [
                Notification(id=row["id"], category=row["category"],
                             quarter=row["quarter"], threshold=row["threshold"],
                             spent_cents=row["spent_cents"],
                             limit_cents=row["limit_cents"], po_id=row["po_id"],
                             revision=row["revision"])
                for row in self._conn.execute(
                    "SELECT * FROM notifications WHERE category=? AND quarter=? "
                    "ORDER BY id", (po["category"], po["quarter"]))
            ]
            return PurchaseOrderView(
                po_id=po_id, revision=revision, status=po["status"],
                amount_cents=po["amount_cents"], category=po["category"],
                quarter=po["quarter"], approvals=approvals, budget=budget,
                notifications=notifications)
        finally:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    @staticmethod
    def _reject(op: str, po_id: str, revision: int, reason: str) -> Receipt:
        return Receipt(ok=False, op=op, po_id=po_id, revision=revision,
                       status="REJECTED", reason=reason)
