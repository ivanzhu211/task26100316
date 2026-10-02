"""规模验收：1 万张采购单、16 线程各自通过库实例操作同一 SQLite 文件。"""
import threading
from collections import defaultdict

from purchase_approval import ApprovalService, OrderStatus, Role

N_THREADS = 16
N_ORDERS = 10_000
CATEGORIES = ("it", "ops", "rd", "mkt")
LIMIT = 4 * 10**9  # 每类累计约 75 亿，确保跨过 80% 与 100%


def test_scale_10k_orders_16_threads(db_path):
    setup = ApprovalService(db_path)
    for cat in CATEGORIES:
        setup.define_budget(cat, "2026Q4", LIMIT)
    setup.close()

    per_thread = N_ORDERS // N_THREADS
    errors = []

    def worker(tid):
        svc = ApprovalService(db_path)
        try:
            for i in range(per_thread):
                n = tid * per_thread + i
                po_id = f"PO-{n:05d}"
                category = CATEGORIES[n % len(CATEGORIES)]
                amount = 3_000_000 + n  # 全部走两级审批
                r = svc.submit(f"buyer{tid}", f"sub-{po_id}", po_id, amount,
                               category, "2026Q4")
                assert r.ok, r
                # 提交重试（重复请求）
                assert svc.submit(f"buyer{tid}", f"sub-{po_id}", po_id, amount,
                                  category, "2026Q4") == r
                r1 = svc.approve(f"lead{tid}", f"l1-{po_id}", po_id, 1, 1,
                                 [Role.PROCUREMENT_LEAD])
                assert r1.ok, r1
                r2 = svc.approve(f"fin{tid}", f"l2-{po_id}", po_id, 1, 2,
                                 [Role.FINANCE_MANAGER])
                assert r2.ok and r2.status == OrderStatus.APPROVED, r2
                # 审批重试（重复请求）
                assert svc.approve(f"fin{tid}", f"l2-{po_id}", po_id, 1, 2,
                                   [Role.FINANCE_MANAGER]) == r2
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            svc.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]

    # 最终校验：预算 == 独立求和；通知恰好两条（80/100）
    svc = ApprovalService(db_path)
    expected = defaultdict(int)
    for n in range(N_ORDERS):
        expected[CATEGORIES[n % len(CATEGORIES)]] += 3_000_000 + n
    for cat in CATEGORIES:
        snap = svc.get_snapshot("PO-0000%d" % (CATEGORIES.index(cat)))
        budget = svc.get_snapshot(f"PO-{CATEGORIES.index(cat):05d}").budget
        assert budget.used_cents == expected[cat], cat
        assert sorted(n.threshold_percent for n in snap.notifications) == [80, 100]
    svc.close()
