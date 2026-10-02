"""幂等回放、改报使旧审批失效、拒绝回执不可翻案。"""
import pytest

from procure_approval import ProcureLibrary, Role

PL = Role.PURCHASING_LEAD
FM = Role.FINANCE_MANAGER


@pytest.fixture()
def lib(tmp_path):
    library = ProcureLibrary(str(tmp_path / "test.db"))
    library.define_budget("HW", "2026Q4", 100_000_000)
    library.define_budget("SW", "2026Q4", 100_000_000)
    yield library
    library.close()


def test_same_key_same_params_replays_receipt(lib):
    r1 = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
                    category="HW", quarter="2026Q4")
    r2 = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
                    category="HW", quarter="2026Q4")
    assert r1 == r2 and r2.ok
    # 状态只推进了一次
    assert lib.get_purchase_order("p1").budget.spent_cents == 100


def test_same_key_different_params_rejected(lib):
    lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
               category="HW", quarter="2026Q4")
    r = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=200,
                   category="HW", quarter="2026Q4")
    assert not r.ok and r.reason == "idempotency_key_conflict"
    # 原结果不变
    assert lib.get_purchase_order("p1").amount_cents == 100
    # 再次用原参数回放仍返回原回执
    r = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
                   category="HW", quarter="2026Q4")
    assert r.ok


def test_rejection_receipt_is_stored(lib):
    """业务拒绝也是回执:同键重试保持原拒绝,即使状态后来允许成功。"""
    lib.submit(caller="u", idem_key="s1", po_id="p1", amount_cents=3_000_000,
               category="HW", quarter="2026Q4")
    # 提前二审 -> 拒绝
    r1 = lib.approve(caller="fm", idem_key="early-l2", po_id="p1", revision=1,
                     level=2, approver="fin1", role=FM)
    assert not r1.ok and r1.reason == "level_not_ready"
    # 完成首审
    lib.approve(caller="pl", idem_key="l1", po_id="p1", revision=1,
                level=1, approver="leader1", role=PL)
    # 同键重试仍返回原拒绝,不翻案、不推进
    r2 = lib.approve(caller="fm", idem_key="early-l2", po_id="p1", revision=1,
                     level=2, approver="fin1", role=FM)
    assert r2 == r1
    view = lib.get_purchase_order("p1")
    assert view.status == "PENDING" and len(view.approvals) == 1


def test_resubmit_invalidates_old_approvals(lib):
    """空账本:3万首审后改报4万换分类;旧首审回放只给原回执;新版待首审无预算消耗。"""
    lib.submit(caller="u", idem_key="s1", po_id="p1", amount_cents=3_000_000,
               category="HW", quarter="2026Q4")
    first = lib.approve(caller="pl", idem_key="l1", po_id="p1", revision=1,
                        level=1, approver="leader1", role=PL)
    assert first.ok and first.status == "PENDING"
    r = lib.resubmit(caller="u", idem_key="re1", po_id="p1",
                     expected_revision=1, amount_cents=4_000_000,
                     category="SW", quarter="2026Q4")
    assert r.ok and r.revision == 2

    # 重放旧首审:同键同参 -> 原成功回执,不再次推进
    replay = lib.approve(caller="pl", idem_key="l1", po_id="p1", revision=1,
                         level=1, approver="leader1", role=PL)
    assert replay == first
    # 旧修订 + 新键 -> 业务拒绝
    stale = lib.approve(caller="pl", idem_key="l1-new", po_id="p1", revision=1,
                        level=1, approver="leader2", role=PL)
    assert not stale.ok and stale.reason == "stale_revision"

    view = lib.get_purchase_order("p1")
    assert view.revision == 2 and view.status == "PENDING"
    assert view.approvals == []  # 原修订审批全部失效
    assert view.category == "SW"
    assert view.budget.category == "SW" and view.budget.spent_cents == 0

    # 新修订重新走两级审批
    lib.approve(caller="pl", idem_key="l1-r2", po_id="p1", revision=2,
                level=1, approver="leader1", role=PL)
    fin = lib.approve(caller="fm", idem_key="l2-r2", po_id="p1", revision=2,
                      level=2, approver="fin1", role=FM)
    assert fin.ok and fin.status == "APPROVED"
    view = lib.get_purchase_order("p1")
    assert view.budget.spent_cents == 4_000_000  # 只对最终批准金额入账一次


def test_receipts_survive_reopen(tmp_path):
    db = str(tmp_path / "test.db")
    lib = ProcureLibrary(db)
    lib.define_budget("HW", "2026Q4", 100_000_000)
    r1 = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
                    category="HW", quarter="2026Q4")
    lib.close()
    lib = ProcureLibrary(db)
    r2 = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
                    category="HW", quarter="2026Q4")
    assert r2 == r1
    assert lib.get_purchase_order("p1").budget.spent_cents == 100
    lib.close()


def test_unknown_budget_rejected(lib):
    r = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=100,
                   category="NOPE", quarter="2026Q4")
    assert not r.ok and r.reason == "budget_not_found"


def test_budget_not_a_hard_cap(lib):
    """超额仍可批准。"""
    lib.define_budget("TINY", "2026Q4", 10)
    r = lib.submit(caller="u", idem_key="k", po_id="p1", amount_cents=600_000,
                   category="TINY", quarter="2026Q4")
    assert r.ok
    r = lib.approve(caller="fm", idem_key="a", po_id="p1", revision=1,
                    level=1, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED"
    assert lib.get_purchase_order("p1").budget.spent_cents == 600_000
