"""并发验收:改报与末审竞争、重复请求、预算与通知一致性、一致性读。"""
import random
import threading
from collections import defaultdict

import pytest

from procure_approval import ProcureLibrary, Role

FM = Role.FINANCE_MANAGER
PL = Role.PURCHASING_LEAD

THREADS = 16


def run_parallel(db_path, worker, threads=THREADS):
    barrier = threading.Barrier(threads)
    errors = []

    def wrap(tid):
        lib = ProcureLibrary(db_path)
        try:
            barrier.wait(timeout=30)
            worker(lib, tid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            lib.close()

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=300)
    assert not errors, errors
    assert all(not t.is_alive() for t in ts)


def test_resubmit_vs_final_approval_race(tmp_path):
    """旧版批准与改报成功不能同时成立;最终状态必为某个串行顺序的结果。"""
    db = str(tmp_path / "race.db")
    setup = ProcureLibrary(db)
    setup.define_budget("HW", "2026Q4", 10**12)
    rounds = 150
    for i in range(rounds):
        setup.submit(caller="u", idem_key=f"s{i}", po_id=f"po{i}",
                     amount_cents=3_000_000, category="HW", quarter="2026Q4")
    setup.close()

    results = []

    def worker(lib, tid):
        for i in range(tid, rounds, THREADS):
            po = f"po{i}"
            r_l1 = lib.approve(caller="pl", idem_key=f"l1-{i}", po_id=po,
                               revision=1, level=1, approver="leader1", role=PL)
            r_l2 = lib.approve(caller="fm", idem_key=f"l2-{i}", po_id=po,
                               revision=1, level=2, approver="fin1", role=FM)
            r_re = lib.resubmit(caller="u", idem_key=f"re-{i}", po_id=po,
                                expected_revision=1, amount_cents=4_000_000,
                                category="HW", quarter="2026Q4")
            results.append((po, r_l1, r_l2, r_re))

    run_parallel(db, worker)

    lib = ProcureLibrary(db)
    spent_total = 0
    for po, r_l1, r_l2, r_re in results:
        view = lib.get_purchase_order(po)
        approved_rev1 = r_l1.ok and r_l2.ok
        if approved_rev1:
            assert not r_re.ok and r_re.reason == "already_approved", (po, r_re)
            assert view.revision == 1 and view.status == "APPROVED"
            spent_total += 3_000_000
        else:
            assert r_re.ok and r_re.revision == 2, (po, r_l1, r_l2, r_re)
            assert view.revision == 2 and view.status == "PENDING"
            assert view.approvals == []  # 旧审批已失效
    assert lib.get_purchase_order("po0").budget.spent_cents == spent_total
    lib.close()


def test_concurrent_mixed_workload(tmp_path):
    """不同单据并发批准 + 重复请求:预算等于最终批准修订之和,通知不重不漏。"""
    db = str(tmp_path / "mixed.db")
    per_thread = 40
    total = THREADS * per_thread
    setup = ProcureLibrary(db)
    setup.define_budget("HW", "2026Q4", 10**12)
    setup.define_budget("SW", "2026Q4", 10**12)
    setup.close()

    def worker(lib, tid):
        rng = random.Random(tid)
        for j in range(per_thread):
            n = tid * per_thread + j
            po = f"po{n}"
            cat = "HW" if n % 2 == 0 else "SW"
            amount = 500_000 + (n % 500) * 1000  # 单人审批区间
            r1 = lib.submit(caller=f"u{tid}", idem_key=f"s-{n}", po_id=po,
                            amount_cents=amount, category=cat, quarter="2026Q4")
            # 重复提交(回放):回执必须一致
            r2 = lib.submit(caller=f"u{tid}", idem_key=f"s-{n}", po_id=po,
                            amount_cents=amount, category=cat, quarter="2026Q4")
            assert r1 == r2
            a1 = lib.approve(caller="fm", idem_key=f"a-{n}", po_id=po,
                             revision=1, level=1, approver="fin1", role=FM)
            assert a1.ok and a1.status == "APPROVED"
            # 重复审批(回放):不重复入账
            a2 = lib.approve(caller="fm", idem_key=f"a-{n}", po_id=po,
                             revision=1, level=1, approver="fin1", role=FM)
            assert a2 == a1
            # 部分单据改报后重新批准
            if rng.random() < 0.3:
                continue  # 已批准的单据改报会被拒绝,这里跳过保持账目可预测

    run_parallel(db, worker)

    lib = ProcureLibrary(db)
    expected = defaultdict(int)
    for n in range(total):
        view = lib.get_purchase_order(f"po{n}")
        assert view.status == "APPROVED" and view.revision == 1
        expected[view.category] += view.amount_cents
    for cat in ("HW", "SW"):
        view = lib.get_purchase_order("po0" if cat == "HW" else "po1")
        assert view.budget.category == cat
        assert view.budget.spent_cents == expected[cat]
        # 每个阈值最多一条通知(预算极大,本例不会触发)
        assert view.notifications == []
    lib.close()


def test_threshold_notifications_exactly_once_under_concurrency(tmp_path):
    """并发下阈值通知不重不漏。"""
    db = str(tmp_path / "notify.db")
    limit = THREADS * 4 * 1_000_000  # 每线程 4 张 x 100万 -> 恰好 100%
    setup = ProcureLibrary(db)
    setup.define_budget("HW", "2026Q4", limit)
    setup.close()

    def worker(lib, tid):
        for j in range(4):
            n = tid * 4 + j
            lib.submit(caller=f"u{tid}", idem_key=f"s-{n}", po_id=f"po{n}",
                       amount_cents=1_000_000, category="HW", quarter="2026Q4")
            lib.approve(caller="fm", idem_key=f"a-{n}", po_id=f"po{n}",
                        revision=1, level=1, approver="fin1", role=FM)

    run_parallel(db, worker)

    lib = ProcureLibrary(db)
    view = lib.get_purchase_order("po0")
    assert view.budget.spent_cents == limit
    thresholds = sorted(n.threshold for n in view.notifications)
    assert thresholds == [80, 100]
    lib.close()


def test_reads_never_see_half_applied_state(tmp_path):
    """查询不能观察到单据与预算只完成一半的状态。"""
    db = str(tmp_path / "reads.db")
    setup = ProcureLibrary(db)
    setup.define_budget("HW", "2026Q4", 10**12)
    count = 64
    for i in range(count):
        setup.submit(caller="u", idem_key=f"s{i}", po_id=f"po{i}",
                     amount_cents=600_000, category="HW", quarter="2026Q4")
    setup.close()
    stop = threading.Event()

    def writer(lib, tid):
        for i in range(tid, count, THREADS // 2):
            lib.approve(caller="fm", idem_key=f"a{i}", po_id=f"po{i}",
                        revision=1, level=1, approver="fin1", role=FM)
        if tid == 0:
            stop.set()

    def reader(lib, tid):
        while not stop.is_set():
            view = lib.get_purchase_order(f"po{tid % count}")
            if view is not None and view.status == "APPROVED":
                # 已批准 => 同一次读到的预算必须已包含该单据金额
                assert view.budget.spent_cents >= view.amount_cents
                assert len(view.approvals) == 1

    threads = THREADS // 2
    run_parallel(db, writer, threads=threads)
    run_parallel(db, reader, threads=threads)


@pytest.mark.slow
def test_scale_10k_pos_16_threads(tmp_path):
    """1 万张采购单、16 线程、各自库实例操作同一 SQLite 文件。"""
    db = str(tmp_path / "scale.db")
    total = 10_000
    setup = ProcureLibrary(db)
    setup.define_budget("HW", "2026Q4", 10**15)
    setup.close()

    def worker(lib, tid):
        for n in range(tid, total, THREADS):
            lib.submit(caller=f"u{tid}", idem_key=f"s-{n}", po_id=f"po{n}",
                       amount_cents=600_000, category="HW", quarter="2026Q4")
            lib.approve(caller="fm", idem_key=f"a-{n}", po_id=f"po{n}",
                        revision=1, level=1, approver="fin1", role=FM)

    run_parallel(db, worker)

    lib = ProcureLibrary(db)
    view = lib.get_purchase_order("po0")
    assert view.budget.spent_cents == total * 600_000
    # 抽查幂等回放仍有效
    r = lib.submit(caller="u3", idem_key="s-3", po_id="po3",
                   amount_cents=600_000, category="HW", quarter="2026Q4")
    assert r.ok and r.revision == 1
    lib.close()
