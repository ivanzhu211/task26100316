"""预算阈值通知与关闭重开后状态保持的测试。"""
from purchase_approval import ApprovalService, OrderStatus, Role

LIMIT = 1_000_000  # 10000.00 美元


def _auto(svc, caller, key, po_id, amount, category="it", quarter="2026Q4"):
    r = svc.submit(caller, key, po_id, amount, category, quarter)
    assert r.ok
    return r


def test_crossing_80_then_100_generates_one_notification_each(budgeted):
    budgeted.define_budget("cap", "2026Q4", LIMIT)
    _auto(budgeted, "u", "k1", "PO-1", 499_999, "cap")   # ~50%
    snap = budgeted.get_snapshot("PO-1")
    assert snap.notifications == ()

    _auto(budgeted, "u", "k2", "PO-2", 300_001, "cap")   # 80% 恰好达到
    snap = budgeted.get_snapshot("PO-2")
    assert [n.threshold_percent for n in snap.notifications] == [80]
    assert snap.notifications[0].used_cents == 800_000

    _auto(budgeted, "u", "k3", "PO-3", 200_000, "cap")   # 100% 恰好达到
    snap = budgeted.get_snapshot("PO-3")
    assert [n.threshold_percent for n in snap.notifications] == [80, 100]

    # 阈值只通知一次：继续超额不再有新通知
    _auto(budgeted, "u", "k4", "PO-4", 499_999, "cap")   # ~150%
    snap = budgeted.get_snapshot("PO-4")
    assert [n.threshold_percent for n in snap.notifications] == [80, 100]
    assert snap.budget.used_cents == 1_499_999  # 预算非硬上限


def test_single_approval_crossing_both_thresholds_generates_two(budgeted):
    budgeted.define_budget("cap", "2026Q4", 500_000)
    budgeted.submit("u", "k1", "PO-1", 600_000, "cap", "2026Q4")
    budgeted.approve("fin", "k2", "PO-1", 1, 1, [Role.FINANCE_MANAGER])  # 一次跨过 80% 与 100%
    snap = budgeted.get_snapshot("PO-1")
    assert [n.threshold_percent for n in snap.notifications] == [80, 100]


def test_notifications_are_per_category_quarter(budgeted):
    budgeted.define_budget("cap", "2026Q4", LIMIT)
    budgeted.define_budget("cap", "2027Q1", LIMIT)
    _auto(budgeted, "u", "k1", "PO-1", 400_000, "cap", "2026Q4")
    _auto(budgeted, "u", "k2", "PO-2", 400_000, "cap", "2026Q4")  # 2026Q4 达 80%
    _auto(budgeted, "u", "k3", "PO-3", 400_000, "cap", "2027Q1")
    _auto(budgeted, "u", "k4", "PO-4", 400_000, "cap", "2027Q1")  # 2027Q1 达 80%
    s1 = budgeted.get_snapshot("PO-1")
    s2 = budgeted.get_snapshot("PO-3")
    assert [n.threshold_percent for n in s1.notifications] == [80]
    assert [n.threshold_percent for n in s2.notifications] == [80]
    assert s1.budget.used_cents == 800_000
    assert s2.budget.used_cents == 800_000


def test_notifications_only_on_full_approval(budgeted):
    budgeted.define_budget("cap", "2026Q4", LIMIT)
    budgeted.submit("u", "k1", "PO-1", 900_000, "cap", "2026Q4")  # 待审
    assert budgeted.get_snapshot("PO-1").notifications == ()
    budgeted.approve("fin", "k2", "PO-1", 1, 1, [Role.FINANCE_MANAGER])
    snap = budgeted.get_snapshot("PO-1")
    assert [n.threshold_percent for n in snap.notifications] == [80]


def test_state_and_receipts_survive_reopen(db_path):
    svc = ApprovalService(db_path)
    svc.define_budget("it", "2026Q4", LIMIT)
    svc.submit("u", "k1", "PO-1", 3_000_000, "it", "2026Q4")
    first = svc.approve("lead", "k2", "PO-1", 1, 1, [Role.PROCUREMENT_LEAD])
    svc.close()

    svc2 = ApprovalService(db_path)
    # 幂等回执仍有效
    assert svc2.approve("lead", "k2", "PO-1", 1, 1, [Role.PROCUREMENT_LEAD]) == first
    # 状态仍在：二审可继续
    r = svc2.approve("fin", "k3", "PO-1", 1, 2, [Role.FINANCE_MANAGER])
    assert r.ok and r.status == OrderStatus.APPROVED
    snap = svc2.get_snapshot("PO-1")
    assert snap.budget.used_cents == 3_000_000
    assert [n.threshold_percent for n in snap.notifications] == [80, 100]
    svc2.close()
