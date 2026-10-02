"""并发验收：改报与末审竞争、多线程交错操作、最终状态可串行化。"""
import threading
from collections import defaultdict

import pytest

from purchase_approval import ApprovalService, OrderStatus, Role


def _run_threads(n_threads, worker):
    errors = []

    def wrap(tid):
        try:
            worker(tid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


@pytest.mark.parametrize("rounds", [25])
def test_resubmit_races_final_approval(db_path, rounds):
    """旧版末审与改报成功不能同时成立。"""
    for rnd in range(rounds):
        svc = ApprovalService(db_path)
        svc.define_budget("it", "2026Q4", 10**12)
        po_id = f"PO-R{rnd}"
        svc.submit("buyer", f"s-{rnd}", po_id, 3_000_000, "it", "2026Q4")
        assert svc.approve("lead", f"a1-{rnd}", po_id, 1, 1, [Role.PROCUREMENT_LEAD]).ok
        svc.close()

        results = {}

        def worker(tid):
            s = ApprovalService(db_path)
            try:
                if tid == 0:
                    results["resubmit"] = s.resubmit(
                        "buyer", f"s2-{rnd}", po_id, 1, 4_000_000, "it", "2026Q4")
                else:
                    results["approve"] = s.approve(
                        "fin", f"a2-{rnd}", po_id, 1, 2, [Role.FINANCE_MANAGER])
            finally:
                s.close()

        _run_threads(2, worker)

        svc = ApprovalService(db_path)
        snap = svc.get_snapshot(po_id)
        resubmit_ok = results["resubmit"].ok
        approve_ok = results["approve"].ok
        assert not (resubmit_ok and approve_ok), "改报与旧版末审不能同时成功"
        if approve_ok:
            assert snap.order.status == OrderStatus.APPROVED
            assert snap.order.revision == 1
            assert snap.budget.used_cents >= 3_000_000
        else:
            assert resubmit_ok, "两者必居其一"
            assert snap.order.revision == 2
            assert snap.order.status == OrderStatus.PENDING
        svc.close()


def test_concurrent_mixed_workload(db_path):
    """16 线程各自持有库实例：并发批准不同单据并夹杂重复请求。"""
    n_threads = 16
    pos_per_thread = 40
    limit = 10**9

    setup = ApprovalService(db_path)
    setup.define_budget("it", "2026Q4", limit)
    setup.define_budget("ops", "2026Q4", limit)
    # 预提交：每个线程一段单据，一半大额（两级审批），一半小额（自动批准）
    plan = {}
    for tid in range(n_threads):
        for i in range(pos_per_thread):
            po_id = f"PO-{tid}-{i}"
            if i % 2 == 0:
                amount = 3_000_000 + tid * 1000 + i  # 两级审批
                setup.submit(f"buyer{tid}", f"sub-{po_id}", po_id, amount, "it", "2026Q4")
                plan[po_id] = ("it", amount, True)
            else:
                amount = 100_000 + tid * 100 + i  # 自动批准
                setup.submit(f"buyer{tid}", f"sub-{po_id}", po_id, amount, "ops", "2026Q4")
                plan[po_id] = ("ops", amount, False)
    setup.close()

    def worker(tid):
        svc = ApprovalService(db_path)
        try:
            for i in range(0, pos_per_thread, 2):
                po_id = f"PO-{tid}-{i}"
                # 首审（含一次重复请求）
                r1 = svc.approve(f"lead{tid}", f"l1-{po_id}", po_id, 1, 1,
                                 [Role.PROCUREMENT_LEAD])
                r1b = svc.approve(f"lead{tid}", f"l1-{po_id}", po_id, 1, 1,
                                  [Role.PROCUREMENT_LEAD])
                assert r1.ok and r1b == r1
                # 二审（含一次重复请求）
                r2 = svc.approve(f"fin{tid}", f"l2-{po_id}", po_id, 1, 2,
                                 [Role.FINANCE_MANAGER])
                r2b = svc.approve(f"fin{tid}", f"l2-{po_id}", po_id, 1, 2,
                                  [Role.FINANCE_MANAGER])
                assert r2.ok and r2.status == OrderStatus.APPROVED and r2b == r2
        finally:
            svc.close()

    _run_threads(n_threads, worker)

    # 最终校验：预算 == 对最终批准修订独立求和；通知不重不漏
    svc = ApprovalService(db_path)
    expected_used = defaultdict(int)
    approved_count = 0
    for po_id, (category, amount, manual) in plan.items():
        snap = svc.get_snapshot(po_id)
        assert snap.order.status == OrderStatus.APPROVED
        assert snap.order.revision == 1
        assert snap.order.amount_cents == amount
        assert snap.order.category == snap.budget.category == category
        if manual:
            assert len(snap.order.approvals) == 2
        expected_used[category] += amount
        approved_count += 1
    assert approved_count == n_threads * pos_per_thread

    for category in ("it", "ops"):
        any_po = next(p for p, v in plan.items() if v[0] == category)
        budget = svc.get_snapshot(any_po).budget
        assert budget.used_cents == expected_used[category]
        notifications = svc.get_snapshot(any_po).notifications
        thresholds = [n.threshold_percent for n in notifications]
        expected_thresholds = [p for p in (80, 100)
                               if expected_used[category] * 100 >= p * limit]
        assert sorted(thresholds) == expected_thresholds
        assert len(thresholds) == len(set(thresholds))  # 不重
    svc.close()
