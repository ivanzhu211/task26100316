"""金额门槛、审批层级与兼任角色的规则测试。"""
from purchase_approval import Decision, OrderStatus, Role


def test_auto_approve_below_5000(budgeted):
    r = budgeted.submit("u1", "k1", "PO-A", 499_999, "it", "2026Q4")
    assert r.ok and r.decision == Decision.AUTO_APPROVED
    assert r.status == OrderStatus.APPROVED
    snap = budgeted.get_snapshot("PO-A")
    assert snap.budget.used_cents == 499_999


def test_no_auto_approve_at_5000(budgeted):
    r = budgeted.submit("u1", "k1", "PO-B", 500_000, "it", "2026Q4")
    assert r.ok and r.status == OrderStatus.PENDING
    snap = budgeted.get_snapshot("PO-B")
    assert snap.budget.used_cents == 0  # 未完全批准不入账


def test_tier2_either_role_single_approval(budgeted):
    budgeted.submit("u1", "k1", "PO-C", 750_000, "it", "2026Q4")
    r = budgeted.approve("lead", "k2", "PO-C", 1, 1, [Role.PROCUREMENT_LEAD])
    assert r.ok and r.status == OrderStatus.APPROVED

    budgeted.submit("u1", "k3", "PO-D", 1_000_000, "it", "2026Q4")
    r = budgeted.approve("fin", "k4", "PO-D", 1, 1, [Role.FINANCE_MANAGER])
    assert r.ok and r.status == OrderStatus.APPROVED


def test_tier3_finance_manager_only(budgeted):
    budgeted.submit("u1", "k1", "PO-E", 1_000_001, "it", "2026Q4")
    r = budgeted.approve("lead", "k2", "PO-E", 1, 1, [Role.PROCUREMENT_LEAD])
    assert not r.ok and r.reason == "insufficient_role"
    r = budgeted.approve("fin", "k3", "PO-E", 1, 1, [Role.FINANCE_MANAGER])
    assert r.ok and r.status == OrderStatus.APPROVED

    budgeted.submit("u1", "k4", "PO-F", 2_500_000, "it", "2026Q4")
    r = budgeted.approve("fin", "k5", "PO-F", 1, 1, [Role.FINANCE_MANAGER])
    assert r.ok and r.status == OrderStatus.APPROVED


def test_tier4_two_levels_distinct_people(budgeted):
    budgeted.submit("u1", "k1", "PO-G", 2_500_001, "it", "2026Q4")
    # 首审必须是采购负责人
    r = budgeted.approve("fin", "k2", "PO-G", 1, 1, [Role.FINANCE_MANAGER])
    assert not r.ok and r.reason == "insufficient_role"
    # 二审在首审前必须拒绝
    r = budgeted.approve("fin", "k3", "PO-G", 1, 2, [Role.FINANCE_MANAGER])
    assert not r.ok and r.reason == "previous_level_pending"
    # 首审完成不算完全批准，无预算消耗
    r = budgeted.approve("lead", "k4", "PO-G", 1, 1, [Role.PROCUREMENT_LEAD])
    assert r.ok and r.status == OrderStatus.PENDING
    assert budgeted.get_snapshot("PO-G").budget.used_cents == 0
    # 二审必须是财务经理
    r = budgeted.approve("lead2", "k5", "PO-G", 1, 2, [Role.PROCUREMENT_LEAD])
    assert not r.ok and r.reason == "insufficient_role"
    # 同一身份兼任两角色也不能自审自批
    r = budgeted.approve("lead", "k6", "PO-G", 1, 2,
                         [Role.PROCUREMENT_LEAD, Role.FINANCE_MANAGER])
    assert not r.ok and r.reason == "same_approver_identity"
    # 不同身份的财务经理二审成功
    r = budgeted.approve("fin", "k7", "PO-G", 1, 2, [Role.FINANCE_MANAGER])
    assert r.ok and r.status == OrderStatus.APPROVED
    assert budgeted.get_snapshot("PO-G").budget.used_cents == 2_500_001


def test_dual_role_person_can_approve_either_tier(budgeted):
    """兼任两角色的人，在单层级区间可用任一角色完成审批。"""
    budgeted.submit("u1", "k1", "PO-H", 600_000, "it", "2026Q4")
    r = budgeted.approve("both", "k2", "PO-H", 1, 1,
                         [Role.PROCUREMENT_LEAD, Role.FINANCE_MANAGER])
    assert r.ok and r.status == OrderStatus.APPROVED


def test_level_cannot_be_overwritten(budgeted):
    budgeted.submit("u1", "k1", "PO-I", 600_000, "it", "2026Q4")
    assert budgeted.approve("lead", "k2", "PO-I", 1, 1, [Role.PROCUREMENT_LEAD]).ok
    r = budgeted.approve("fin", "k3", "PO-I", 1, 1, [Role.FINANCE_MANAGER])
    assert not r.ok and r.reason in ("level_already_approved", "already_approved")


def test_invalid_amount_rejected(budgeted):
    for bad in (0, -1, 1.5, "100"):
        r = budgeted.submit("u1", f"k-{bad}", f"PO-{bad}", bad, "it", "2026Q4")
        assert not r.ok and r.reason == "invalid_amount"
