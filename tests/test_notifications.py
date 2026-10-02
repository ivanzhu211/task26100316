"""预算累计与 80%/100% 阈值通知。"""
import pytest

from procure_approval import ProcureLibrary, Role

FM = Role.FINANCE_MANAGER


@pytest.fixture()
def lib(tmp_path):
    library = ProcureLibrary(str(tmp_path / "test.db"))
    library.define_budget("HW", "2026Q4", 1_000_000)  # 1万美元
    yield library
    library.close()


def approve_single(lib, po_id, key):
    r = lib.approve(caller="fm", idem_key=key, po_id=po_id, revision=1,
                    level=1, approver="fin1", role=FM)
    assert r.ok and r.status == "APPROVED", r


def submit_approved(lib, po_id, amount, key_prefix):
    r = lib.submit(caller="u", idem_key=f"{key_prefix}-s", po_id=po_id,
                   amount_cents=amount, category="HW", quarter="2026Q4")
    if r.status == "PENDING":
        approve_single(lib, po_id, f"{key_prefix}-a")
    assert lib.get_purchase_order(po_id).status == "APPROVED"


def test_crossing_80_generates_one_notification(lib):
    submit_approved(lib, "p1", 700_000, "p1")   # 70%
    assert lib.get_purchase_order("p1").notifications == []
    submit_approved(lib, "p2", 200_000, "p2")   # 90%:跨过 80%
    notes = lib.get_purchase_order("p2").notifications
    assert [n.threshold for n in notes] == [80]
    assert notes[0].spent_cents == 900_000 and notes[0].limit_cents == 1_000_000


def test_crossing_100_generates_one_notification(lib):
    submit_approved(lib, "p1", 900_000, "p1")   # 90%
    submit_approved(lib, "p2", 200_000, "p2")   # 110%:跨过 100%
    notes = lib.get_purchase_order("p2").notifications
    assert [n.threshold for n in notes] == [80, 100]


def test_one_jump_crossing_both_thresholds_generates_two(lib):
    submit_approved(lib, "p1", 1_100_000, "p1")  # 0% -> 110%
    notes = lib.get_purchase_order("p1").notifications
    assert [n.threshold for n in notes] == [80, 100]


def test_each_threshold_notified_only_once(lib):
    submit_approved(lib, "p1", 850_000, "p1")   # 85% -> 通知 80
    submit_approved(lib, "p2", 100_000, "p2")   # 95% -> 不再通知 80
    submit_approved(lib, "p3", 200_000, "p3")   # 115% -> 通知 100
    submit_approved(lib, "p4", 200_000, "p4")   # 135% -> 不再通知
    notes = lib.get_purchase_order("p4").notifications
    assert [n.threshold for n in notes] == [80, 100]


def test_exact_threshold_boundary_notifies(lib):
    submit_approved(lib, "p1", 800_000, "p1")   # 恰好 80%
    notes = lib.get_purchase_order("p1").notifications
    assert [n.threshold for n in notes] == [80]


def test_auto_approved_also_consumes_budget(lib):
    lib.submit(caller="u", idem_key="s", po_id="p1", amount_cents=499_999,
               category="HW", quarter="2026Q4")
    view = lib.get_purchase_order("p1")
    assert view.status == "APPROVED" and view.budget.spent_cents == 499_999


def test_pending_approval_does_not_consume_budget(lib):
    lib.submit(caller="u", idem_key="s", po_id="p1", amount_cents=600_000,
               category="HW", quarter="2026Q4")
    assert lib.get_purchase_order("p1").budget.spent_cents == 0


def test_notifications_scoped_per_category_quarter(lib):
    lib.define_budget("SW", "2026Q4", 1_000_000)
    submit_approved(lib, "p1", 900_000, "p1")   # HW 90%
    lib.submit(caller="u", idem_key="s2", po_id="p2", amount_cents=100_000,
               category="SW", quarter="2026Q4")  # SW 10%(自动批准)
    assert [n.threshold for n in lib.get_purchase_order("p1").notifications] == [80]
    assert lib.get_purchase_order("p2").notifications == []
