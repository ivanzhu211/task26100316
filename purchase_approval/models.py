"""类型定义：采购单、修订、审批、回执、预算与通知。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Role(str, Enum):
    PROCUREMENT_LEAD = "procurement_lead"
    FINANCE_MANAGER = "finance_manager"


class OrderStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"


class Decision(str, Enum):
    AUTO_APPROVED = "auto_approved"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Receipt:
    """提交/改报/审批的幂等回执（含业务拒绝）。"""
    ok: bool
    operation: str            # submit / resubmit / approve
    po_id: str
    revision: int
    decision: Optional[Decision] = None
    reason: Optional[str] = None
    status: Optional[OrderStatus] = None
    level: Optional[int] = None


@dataclass(frozen=True)
class ApprovalRecord:
    po_id: str
    revision: int
    level: int
    approver_id: str


@dataclass(frozen=True)
class PurchaseOrderView:
    po_id: str
    revision: int
    status: OrderStatus
    amount_cents: int
    category: str
    quarter: str
    approvals: tuple[ApprovalRecord, ...] = ()


@dataclass(frozen=True)
class BudgetView:
    category: str
    quarter: str
    limit_cents: int
    used_cents: int


@dataclass(frozen=True)
class Notification:
    category: str
    quarter: str
    threshold_percent: int   # 80 或 100
    used_cents: int
    limit_cents: int


@dataclass(frozen=True)
class OrderSnapshot:
    """同一已提交时刻的一致视图：单据 + 预算 + 通知。"""
    order: PurchaseOrderView
    budget: BudgetView
    notifications: tuple[Notification, ...] = ()
