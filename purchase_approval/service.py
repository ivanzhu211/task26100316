"""业务层：提交/改报/审批/查询，含幂等与审批层级规则。"""
from __future__ import annotations

import json
from typing import Iterable, Optional, Tuple

from .models import (
    ApprovalRecord,
    BudgetView,
    Decision,
    Notification,
    OrderSnapshot,
    OrderStatus,
    PurchaseOrderView,
    Receipt,
    Role,
)
from .store import Store

# 金额门槛（美分）
TIER1_MAX = 500_000      # < 5000.00 自动批准
TIER2_MAX = 1_000_000    # <= 10000.00 一人批准（采购负责人或财务经理）
TIER3_MAX = 2_500_000    # <= 25000.00 仅财务经理一人批准
# > 25000.00 采购负责人首审 + 财务经理二审，且两人身份必须不同

_THRESHOLDS = (80, 100)


def _fingerprint(operation: str, params: dict) -> str:
    return json.dumps({"op": operation, "params": params}, sort_keys=True)


def _required_levels(amount_cents: int) -> int:
    """该金额完全批准所需的审批层数；0 表示自动批准。"""
    if amount_cents < TIER1_MAX:
        return 0
    if amount_cents <= TIER3_MAX:
        return 1
    return 2


class ApprovalService:
    """上层直接调用的库入口。每个线程可持有独立实例，指向同一 SQLite 文件。"""

    def __init__(self, db_path: str):
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "ApprovalService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def define_budget(self, category: str, quarter: str, limit_cents: int) -> None:
        """预置分类季度预算（幂等，不修改已存在的预算）。"""
        if not isinstance(limit_cents, int) or limit_cents <= 0:
            raise ValueError("limit_cents must be a positive integer")
        self._store.init_budget(category, quarter, limit_cents)

    # ------------------------------------------------------------------
    # 幂等框架：回放优先于当前状态检查
    # ------------------------------------------------------------------
    def _run_idempotent(self, caller_id: str, idem_key: str, fingerprint: str,
                        executor) -> Receipt:
        store = self._store
        with store.transaction(immediate=True) as conn:
            found = store.find_receipt(conn, caller_id, idem_key)
            if found is not None:
                saved_fingerprint, saved_receipt = found
                if saved_fingerprint == fingerprint:
                    return saved_receipt  # 回放：原样返回，不推进状态
                return Receipt(  # 同键不同参数：拒绝且不改变原结果
                    ok=False, operation=saved_receipt.operation, po_id=saved_receipt.po_id,
                    revision=saved_receipt.revision, decision=Decision.REJECTED,
                    reason="idempotency_key_conflict",
                )
            receipt = executor(conn)
            store.save_receipt(conn, caller_id, idem_key, fingerprint, receipt)
            return receipt

    # ------------------------------------------------------------------
    # 提交 / 改报
    # ------------------------------------------------------------------
    def submit(self, caller_id: str, idem_key: str, po_id: str,
               amount_cents: int, category: str, quarter: str) -> Receipt:
        """首次提交采购单，产生修订1。"""
        fingerprint = _fingerprint("submit", {
            "po_id": po_id, "amount_cents": amount_cents,
            "category": category, "quarter": quarter,
        })

        def execute(conn) -> Receipt:
            error = self._validate_amount(amount_cents)
            if error is None and self._store.get_order_row(conn, po_id) is not None:
                error = "duplicate_po_id"
            if error is None and self._store.get_budget(conn, category, quarter) is None:
                error = "unknown_budget"
            if error is not None:
                return Receipt(ok=False, operation="submit", po_id=po_id, revision=1,
                               decision=Decision.REJECTED, reason=error)
            return self._create_revision(conn, "submit", po_id, 1, amount_cents, category, quarter)

        return self._run_idempotent(caller_id, idem_key, fingerprint, execute)

    def resubmit(self, caller_id: str, idem_key: str, po_id: str,
                 expected_revision: int, amount_cents: int,
                 category: str, quarter: str) -> Receipt:
        """以当前修订号为前提改报；成功后修订号递增，原修订审批失效。"""
        fingerprint = _fingerprint("resubmit", {
            "po_id": po_id, "expected_revision": expected_revision,
            "amount_cents": amount_cents, "category": category, "quarter": quarter,
        })

        def execute(conn) -> Receipt:
            def reject(reason: str) -> Receipt:
                return Receipt(ok=False, operation="resubmit", po_id=po_id,
                               revision=expected_revision, decision=Decision.REJECTED,
                               reason=reason)

            error = self._validate_amount(amount_cents)
            if error is not None:
                return reject(error)
            row = self._store.get_order_row(conn, po_id)
            if row is None:
                return reject("po_not_found")
            current_revision, status = row
            if status == OrderStatus.APPROVED.value:
                return reject("already_approved")
            if expected_revision != current_revision:
                return reject("revision_mismatch")
            if self._store.get_budget(conn, category, quarter) is None:
                return reject("unknown_budget")
            return self._create_revision(conn, "resubmit", po_id, current_revision + 1,
                                         amount_cents, category, quarter)

        return self._run_idempotent(caller_id, idem_key, fingerprint, execute)

    def _create_revision(self, conn, operation: str, po_id: str, revision: int,
                         amount_cents: int, category: str, quarter: str) -> Receipt:
        store = self._store
        if revision == 1:
            conn.execute(
                "INSERT INTO purchase_orders (po_id, current_revision, status) VALUES (?, ?, ?)",
                (po_id, 1, OrderStatus.PENDING.value),
            )
        else:
            conn.execute(
                "UPDATE purchase_orders SET current_revision=? WHERE po_id=?",
                (revision, po_id),
            )
        conn.execute(
            "INSERT INTO revisions (po_id, revision, amount_cents, category, quarter) "
            "VALUES (?, ?, ?, ?, ?)",
            (po_id, revision, amount_cents, category, quarter),
        )
        if _required_levels(amount_cents) == 0:
            # 自动批准：立即入账
            conn.execute("UPDATE purchase_orders SET status=? WHERE po_id=?",
                         (OrderStatus.APPROVED.value, po_id))
            self._consume_budget(conn, category, quarter, amount_cents)
            return Receipt(ok=True, operation=operation, po_id=po_id, revision=revision,
                           decision=Decision.AUTO_APPROVED, status=OrderStatus.APPROVED)
        return Receipt(ok=True, operation=operation, po_id=po_id, revision=revision,
                       status=OrderStatus.PENDING)

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------
    def approve(self, caller_id: str, idem_key: str, po_id: str,
                revision: int, level: int, roles: Iterable[Role]) -> Receipt:
        """针对明确修订与层级审批；不自动转用于当前修订。"""
        role_set = {Role(r) for r in roles}
        fingerprint = _fingerprint("approve", {
            "po_id": po_id, "revision": revision, "level": level,
            "roles": sorted(r.value for r in role_set),
        })

        def execute(conn) -> Receipt:
            def reject(reason: str) -> Receipt:
                return Receipt(ok=False, operation="approve", po_id=po_id, revision=revision,
                               level=level, decision=Decision.REJECTED, reason=reason)

            store = self._store
            row = store.get_order_row(conn, po_id)
            if row is None:
                return reject("po_not_found")
            current_revision, status = row
            if status == OrderStatus.APPROVED.value:
                return reject("already_approved")
            if revision != current_revision:
                return reject("stale_revision")
            rev = store.get_revision_row(conn, po_id, revision)
            amount_cents = rev[0]
            total_levels = _required_levels(amount_cents)
            if total_levels == 0:
                return reject("no_approval_required")
            if level < 1 or level > total_levels:
                return reject("invalid_level")

            existing = {a.level: a for a in store.get_approvals(conn, po_id, revision)}
            if level in existing:
                return reject("level_already_approved")
            if level > 1 and (level - 1) not in existing:
                return reject("previous_level_pending")

            # 角色与身份校验
            if total_levels == 1:
                if amount_cents <= TIER2_MAX:
                    allowed = {Role.PROCUREMENT_LEAD, Role.FINANCE_MANAGER}
                else:
                    allowed = {Role.FINANCE_MANAGER}
                if not (role_set & allowed):
                    return reject("insufficient_role")
            else:
                if level == 1:
                    if Role.PROCUREMENT_LEAD not in role_set:
                        return reject("insufficient_role")
                else:
                    if Role.FINANCE_MANAGER not in role_set:
                        return reject("insufficient_role")
                    first = existing[1]
                    if first.approver_id == caller_id:
                        return reject("same_approver_identity")

            conn.execute(
                "INSERT INTO approvals (po_id, revision, level, approver_id) VALUES (?, ?, ?, ?)",
                (po_id, revision, level, caller_id),
            )
            if level == total_levels:
                conn.execute("UPDATE purchase_orders SET status=? WHERE po_id=?",
                             (OrderStatus.APPROVED.value, po_id))
                self._consume_budget(conn, rev[1], rev[2], amount_cents)
                final_status = OrderStatus.APPROVED
            else:
                final_status = OrderStatus.PENDING
            return Receipt(ok=True, operation="approve", po_id=po_id, revision=revision,
                           level=level, decision=Decision.APPROVED, status=final_status)

        return self._run_idempotent(caller_id, idem_key, fingerprint, execute)

    # ------------------------------------------------------------------
    # 预算入账与阈值通知
    # ------------------------------------------------------------------
    def _consume_budget(self, conn, category: str, quarter: str, amount_cents: int) -> None:
        row = conn.execute(
            "SELECT limit_cents, used_cents, notified_80, notified_100 FROM budgets "
            "WHERE category=? AND quarter=?",
            (category, quarter),
        ).fetchone()
        limit_cents, used_before, notified_80, notified_100 = row
        used_after = used_before + amount_cents
        conn.execute(
            "UPDATE budgets SET used_cents=? WHERE category=? AND quarter=?",
            (used_after, category, quarter),
        )
        flags = {80: notified_80, 100: notified_100}
        for threshold in _THRESHOLDS:
            crossed = (used_before * 100 < threshold * limit_cents
                       <= used_after * 100)
            if crossed and not flags[threshold]:
                conn.execute(
                    "INSERT INTO notifications (category, quarter, threshold_percent, "
                    "used_cents, limit_cents) VALUES (?, ?, ?, ?, ?)",
                    (category, quarter, threshold, used_after, limit_cents),
                )
                column = "notified_80" if threshold == 80 else "notified_100"
                conn.execute(
                    f"UPDATE budgets SET {column}=1 WHERE category=? AND quarter=?",
                    (category, quarter),
                )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_snapshot(self, po_id: str) -> Optional[OrderSnapshot]:
        """一致视图：单据 + 对应预算 + 通知，来自同一已提交时刻。"""
        store = self._store
        with store.transaction(immediate=False) as conn:
            row = store.get_order_row(conn, po_id)
            if row is None:
                return None
            current_revision, status = row
            amount_cents, category, quarter = store.get_revision_row(conn, po_id, current_revision)
            approvals = store.get_approvals(conn, po_id, current_revision)
            budget = store.get_budget(conn, category, quarter)
            notifications = store.get_notifications(conn, category, quarter)
        order = PurchaseOrderView(
            po_id=po_id, revision=current_revision, status=OrderStatus(status),
            amount_cents=amount_cents, category=category, quarter=quarter,
            approvals=approvals,
        )
        return OrderSnapshot(order=order, budget=budget, notifications=notifications)

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_amount(amount_cents: int) -> Optional[str]:
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            return "invalid_amount"
        return None
