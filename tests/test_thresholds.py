"""金额门槛、审批层级与双人要求。"""
import pytest

from procure_approval import ProcureLibrary, Role

PL = Role.PURCHASING_LEAD
FM = Role.FINANCE_MANAGER


@pytest.fixture()
def lib(tmp_path):
    library = ProcureLibrary(str(tmp_path / "test.db"))
    library.define_budget("HW", "2026Q4", 100_000_000)
    yield library
    library.close()


def submit(lib, po_id, amount):
    return lib.submit(caller="buyer", idem_key=f"sub-{po_id}", po_id=po_id,
                      amount_cents=amount, category="HW", quarter="2026Q4")


def test_auto_approve_below_5000(lib):
    assert submit(lib, "a1", 499_999).status == "APPROVED"
    assert submit(lib, "a2", 1).status == "APPROVED"


def test_single_either_role_5000_to_10000(lib):
    assert submit(lib, "b1", 500_000).status == "PENDING"
    assert submit(lib, "b2", 1_000_000).status == "PENDING"
    # 采购负责人可批
    r = lib.approve(caller="pl", idem_key="ap-b1", po_id="b1", revision=1,
                    level=1, approver="leader1", role=PL)
    assert r.ok and r.status == "APPROVED"
    # 财务经理也可批
    r = lib.approve(caller="fm", idem_key="ap-b2", po_id="b2", revision=1,
                    level=1, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED"


def test_fm_only_10000_to_25000(lib):
    assert submit(lib, "c1", 1_000_001).status == "PENDING"
    assert submit(lib, "c2", 2_500_000).status == "PENDING"
    # 采购负责人无权
    r = lib.approve(caller="pl", idem_key="ap-c1", po_id="c1", revision=1,
                    level=1, approver="leader1", role=PL)
    assert not r.ok and r.reason == "role_not_allowed"
    r = lib.approve(caller="fm", idem_key="ap-c2", po_id="c2", revision=1,
                    level=1, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED"


def test_two_level_above_25000(lib):
    assert submit(lib, "d1", 2_500_001).status == "PENDING"
    # 二审必须在首审之后
    r = lib.approve(caller="fm", idem_key="ap-d1-l2", po_id="d1", revision=1,
                    level=2, approver="fin1", role=FM)
    assert not r.ok and r.reason == "level_not_ready"
    # 首审只能采购负责人
    r = lib.approve(caller="fm", idem_key="ap-d1-l1-fm", po_id="d1", revision=1,
                    level=1, approver="fin1", role=FM)
    assert not r.ok and r.reason == "role_not_allowed"
    r = lib.approve(caller="pl", idem_key="ap-d1-l1", po_id="d1", revision=1,
                    level=1, approver="leader1", role=PL)
    assert r.ok and r.status == "PENDING"  # 仅首审不算完全批准
    view = lib.get_purchase_order("d1")
    assert view.status == "PENDING" and view.budget.spent_cents == 0
    # 二审只能财务经理
    r = lib.approve(caller="pl", idem_key="ap-d1-l2-pl", po_id="d1", revision=1,
                    level=2, approver="leader2", role=PL)
    assert not r.ok and r.reason == "role_not_allowed"
    r = lib.approve(caller="fm", idem_key="ap-d1-l2b", po_id="d1", revision=1,
                    level=2, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED"


def test_dual_role_person_cannot_approve_both_levels(lib):
    """一人兼任两角色,两级仍须不同身份。"""
    submit(lib, "e1", 3_000_000)
    r = lib.approve(caller="boss", idem_key="ap-e1-l1", po_id="e1", revision=1,
                    level=1, approver="boss", role=PL)
    assert r.ok
    r = lib.approve(caller="boss", idem_key="ap-e1-l2", po_id="e1", revision=1,
                    level=2, approver="boss", role=FM)
    assert not r.ok and r.reason == "approver_must_differ"
    r = lib.approve(caller="fm", idem_key="ap-e1-l2b", po_id="e1", revision=1,
                    level=2, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED"


def test_level_cannot_be_repeated_or_skipped(lib):
    submit(lib, "f1", 3_000_000)
    lib.approve(caller="pl", idem_key="ap-f1-l1", po_id="f1", revision=1,
                level=1, approver="leader1", role=PL)
    r = lib.approve(caller="pl", idem_key="ap-f1-l1b", po_id="f1", revision=1,
                    level=1, approver="leader2", role=PL)
    assert not r.ok and r.reason == "level_already_approved"
    r = lib.approve(caller="fm", idem_key="ap-f1-l3", po_id="f1", revision=1,
                    level=3, approver="fin1", role=FM)
    assert not r.ok and r.reason == "invalid_level"


def test_resubmit_recomputes_requirements(lib):
    """改报后按新金额重新判定层级。"""
    submit(lib, "g1", 3_000_000)
    lib.approve(caller="pl", idem_key="ap-g1-l1", po_id="g1", revision=1,
                level=1, approver="leader1", role=PL)
    r = lib.resubmit(caller="buyer", idem_key="re-g1", po_id="g1",
                     expected_revision=1, amount_cents=600_000,
                     category="HW", quarter="2026Q4")
    assert r.ok and r.revision == 2 and r.status == "PENDING"
    # 新金额只需一人批准,财务经理直接批
    r = lib.approve(caller="fm", idem_key="ap-g2", po_id="g1", revision=2,
                    level=1, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED"
    # 完全批准后不能改报
    r = lib.resubmit(caller="buyer", idem_key="re-g1b", po_id="g1",
                     expected_revision=2, amount_cents=700_000,
                     category="HW", quarter="2026Q4")
    assert not r.ok and r.reason == "already_approved"


def test_resubmit_requires_current_revision(lib):
    submit(lib, "h1", 600_000)
    lib.resubmit(caller="buyer", idem_key="re-h1", po_id="h1",
                 expected_revision=1, amount_cents=700_000,
                 category="HW", quarter="2026Q4")
    r = lib.resubmit(caller="buyer", idem_key="re-h1b", po_id="h1",
                     expected_revision=1, amount_cents=800_000,
                     category="HW", quarter="2026Q4")
    assert not r.ok and r.reason == "revision_mismatch"
