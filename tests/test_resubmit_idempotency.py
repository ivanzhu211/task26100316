"""改报、幂等回放与拒绝保持的验收场景。"""
from purchase_approval import Decision, OrderStatus, Role


def test_resubmit_after_first_approval_replay_and_rejections(budgeted):
    """空账本：3万首审后改报4万换分类；重放旧首审返回原回执；新版待首审无预算消耗。"""
    svc = budgeted
    r = svc.submit("buyer", "s1", "PO-1", 3_000_000, "it", "2026Q4")
    assert r.ok and r.revision == 1 and r.status == OrderStatus.PENDING

    # 首审完成前提前二审被拒绝
    early = svc.approve("fin", "a2", "PO-1", 1, 2, [Role.FINANCE_MANAGER])
    assert not early.ok and early.reason == "previous_level_pending"

    first = svc.approve("lead", "a1", "PO-1", 1, 1, [Role.PROCUREMENT_LEAD])
    assert first.ok and first.status == OrderStatus.PENDING

    # 改报为 4 万并换分类
    r = svc.resubmit("buyer", "s2", "PO-1", 1, 4_000_000, "ops", "2026Q4")
    assert r.ok and r.revision == 2

    # 重放旧首审：返回原回执，不推进状态
    replay = svc.approve("lead", "a1", "PO-1", 1, 1, [Role.PROCUREMENT_LEAD])
    assert replay == first
    snap = svc.get_snapshot("PO-1")
    assert snap.order.revision == 2 and snap.order.status == OrderStatus.PENDING
    assert len(snap.order.approvals) == 0  # 旧审批已失效
    assert snap.budget.used_cents == 0     # 没有预算消耗

    # 完成新版首审后，同键重试曾被拒绝的二审仍保持原拒绝
    assert svc.approve("lead", "a3", "PO-1", 2, 1, [Role.PROCUREMENT_LEAD]).ok
    retry = svc.approve("fin", "a2", "PO-1", 1, 2, [Role.FINANCE_MANAGER])
    assert retry == early and not retry.ok

    # 新版二审成功，预算按最终批准的 4 万入账一次
    final = svc.approve("fin", "a4", "PO-1", 2, 2, [Role.FINANCE_MANAGER])
    assert final.ok and final.status == OrderStatus.APPROVED
    snap = svc.get_snapshot("PO-1")
    assert snap.budget.category == "ops" and snap.budget.used_cents == 4_000_000


def test_resubmit_requires_current_revision(budgeted):
    budgeted.submit("u", "s1", "PO-2", 600_000, "it", "2026Q4")
    r = budgeted.resubmit("u", "s2", "PO-2", 5, 700_000, "it", "2026Q4")
    assert not r.ok and r.reason == "revision_mismatch"
    assert budgeted.get_snapshot("PO-2").order.revision == 1


def test_approved_order_cannot_resubmit(budgeted):
    budgeted.submit("u", "s1", "PO-3", 100, "it", "2026Q4")  # 自动批准
    r = budgeted.resubmit("u", "s2", "PO-3", 1, 200, "it", "2026Q4")
    assert not r.ok and r.reason == "already_approved"


def test_idempotent_replay_of_submit_and_resubmit(budgeted):
    r1 = budgeted.submit("u", "s1", "PO-4", 600_000, "it", "2026Q4")
    assert budgeted.submit("u", "s1", "PO-4", 600_000, "it", "2026Q4") == r1
    # 同键不同参数：拒绝且不改变原结果
    r = budgeted.submit("u", "s1", "PO-4", 700_000, "it", "2026Q4")
    assert not r.ok and r.reason == "idempotency_key_conflict"
    assert budgeted.get_snapshot("PO-4").order.amount_cents == 600_000

    r2 = budgeted.resubmit("u", "s2", "PO-4", 1, 800_000, "it", "2026Q4")
    assert r2.ok and r2.revision == 2
    assert budgeted.resubmit("u", "s2", "PO-4", 1, 800_000, "it", "2026Q4") == r2
    assert budgeted.get_snapshot("PO-4").order.revision == 2


def test_rejected_submit_replays_rejection(budgeted):
    """业务拒绝也入幂等表：同键重放返回同一拒绝回执。"""
    r1 = budgeted.submit("u", "s1", "PO-5", 600_000, "it", "2026Q4")
    assert r1.ok
    # 另一个调用者用不同键提交同 po_id -> 拒绝
    r2 = budgeted.submit("v", "s1", "PO-5", 600_000, "it", "2026Q4")
    assert not r2.ok and r2.reason == "duplicate_po_id"
    # 重放仍是同一拒绝
    assert budgeted.submit("v", "s1", "PO-5", 600_000, "it", "2026Q4") == r2


def test_budget_charged_once_on_final_amount(budgeted):
    """预算只对最终批准金额记账一次。"""
    budgeted.submit("u", "s1", "PO-6", 600_000, "it", "2026Q4")
    budgeted.resubmit("u", "s2", "PO-6", 1, 900_000, "it", "2026Q4")
    assert budgeted.approve("fin", "a1", "PO-6", 2, 1, [Role.FINANCE_MANAGER]).ok
    snap = budgeted.get_snapshot("PO-6")
    assert snap.budget.used_cents == 900_000
