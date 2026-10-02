"""类型定义:角色、状态、回执、视图对象。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Role(str, Enum):
    PURCHASING_LEAD = "PURCHASING_LEAD"
    FINANCE_MANAGER = "FINANCE_MANAGER"


class POStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"


@dataclass(frozen=True)
class Receipt:
    """提交/改报/审批的幂等回执;业务拒绝也是回执的一种。"""
    ok: bool
    op: str
    po_id: str
    revision: int
    status: str
    reason: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({
            "ok": self.ok,
            "op": self.op,
            "po_id": self.po_id,
            "revision": self.revision,
            "status": self.status,
            "reason": self.reason,
        }, sort_keys=True)

    @staticmethod
    def from_json(payload: str) -> "Receipt":
        data = json.loads(payload)
        return Receipt(
            ok=data["ok"],
            op=data["op"],
            po_id=data["po_id"],
            revision=data["revision"],
            status=data["status"],
            reason=data.get("reason"),
        )


@dataclass(frozen=True)
class ApprovalRecord:
    level: int
    approver: str
    role: str


@dataclass(frozen=True)
class Notification:
    id: int
    category: str
    quarter: str
    threshold: int
    spent_cents: int
    limit_cents: int
    po_id: str
    revision: int


@dataclass(frozen=True)
class BudgetView:
    category: str
    quarter: str
    limit_cents: int
    spent_cents: int


@dataclass(frozen=True)
class PurchaseOrderView:
    po_id: str
    revision: int
    status: str
    amount_cents: int
    category: str
    quarter: str
    approvals: List[ApprovalRecord] = field(default_factory=list)
    budget: Optional[BudgetView] = None
    notifications: List[Notification] = field(default_factory=list)
