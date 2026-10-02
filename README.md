# task26100316 — 采购单审批库

纯 Python + SQLite 实现的采购单审批库,由上层直接调用,无外部服务依赖。

## 能力

- 提交 / 改报采购单(`submit` / `resubmit`),审批(`approve`),一致性查看(`get_purchase_order`)。
- 采购单、审批、预算累计、阈值通知、幂等回执全部持久化到同一个 SQLite 文件。
- 所有写操作在单个 `BEGIN IMMEDIATE` 事务内完成"查幂等 → 校验 → 改状态 → 写回执",
  并发结果等价于某个串行执行顺序;读操作在单个读事务内取快照。
- 关闭重开后状态与幂等回执仍然有效。

## 业务规则要点

- 金额、预算均为正整数美分;预算预先通过 `define_budget` 给定,不是硬上限。
- 首次提交为修订 1;未完全批准可按当前修订号改报,改报后修订号递增、原审批失效,
  并按新金额重新判定审批要求;完全批准后不能改报。
- 审批门槛:`< $5000` 自动批准;`$5000–$10000` 采购负责人或财务经理一人批准;
  `($10000, $25000]` 仅财务经理;`> $25000` 采购负责人首审 + 财务经理二审,
  两人身份必须不同(兼任两角色也不行)。
- 审批针对明确修订与层级,不自动转用于当前修订;二审须待首审完成;每层只推进一次。
- 完全批准(含自动批准)时把该修订全额计入分类季度预算;累计从低于 80%/100%
  变为达到或超过时各产生一条通知,一次跨两档产生两条,每档只通知一次。
- 提交 / 改报 / 审批均带幂等键:同调用者同键同参返回首次回执(含业务拒绝),
  同键不同参拒绝且不改原结果;回放优先于当前状态检查。

## 调用示例

```python
from procure_approval import ProcureLibrary, Role

lib = ProcureLibrary("procure.db")          # 每个线程各自持有一个实例
lib.define_budget("HW", "2026Q4", 10_000_000)

r = lib.submit(caller="buyer1", idem_key="req-1", po_id="PO-1",
               amount_cents=3_000_000, category="HW", quarter="2026Q4")
# r.ok=True, r.status="PENDING", r.revision=1

r = lib.approve(caller="lead1", idem_key="req-2", po_id="PO-1", revision=1,
                level=1, approver="leader1", role=Role.PURCHASING_LEAD)
r = lib.approve(caller="fm1", idem_key="req-3", po_id="PO-1", revision=1,
                level=2, approver="fin1", role=Role.FINANCE_MANAGER)
# r.status == "APPROVED"

view = lib.get_purchase_order("PO-1")       # 单据 + 当前修订审批 + 预算 + 通知,同一快照
lib.close()
```

`resubmit` 需要 `expected_revision` 等于当前修订号。所有回执为 `Receipt`
(`ok / op / po_id / revision / status / reason`),业务拒绝也是回执并参与幂等回放。

## 测试

```bash
python3 -m pytest            # 全部(含 1 万张 x 16 线程规模测试)
python3 -m pytest -m "not slow"   # 仅快速测试
```

覆盖:金额门槛与双人要求、3 万改报 4 万换分类、提前二审拒绝不可翻案、
改报与末审竞争、并发重复请求、阈值通知不重不漏、一致性读、重开持久化。
