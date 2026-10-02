# 采购单审批库（purchase_approval）

纯 Python + SQLite 的采购单审批库，由上层直接调用。采购单、修订、审批、预算累计、
阈值通知与幂等回执全部持久化到同一个 SQLite 文件；多线程可各自持有库实例并发操作同一文件。

## 安装与运行

无第三方依赖（Python 3.10+，标准库 `sqlite3`）。测试需要 `pytest`：

```bash
python3 -m pytest tests/ -q
```

## 快速上手

```python
from purchase_approval import ApprovalService, Role

svc = ApprovalService("approval.db")          # 每个线程可持有独立实例，指向同一文件
svc.define_budget("it", "2026Q4", 10_000_000) # 预置分类季度预算（幂等，不修改已有预算）

# 提交（首次为修订1）；idem_key 为幂等键
r = svc.submit("buyer1", "key-1", "PO-100", 3_000_000, "it", "2026Q4")

# 改报：必须以当前修订号为前提，成功后修订号递增，旧修订审批全部失效
r = svc.resubmit("buyer1", "key-2", "PO-100", 1, 4_000_000, "it", "2026Q4")

# 审批：必须针对明确的修订与层级；roles 为调用者持有的角色
r = svc.approve("lead1", "key-3", "PO-100", 2, 1, [Role.PROCUREMENT_LEAD])  # 首审
r = svc.approve("fin1",  "key-4", "PO-100", 2, 2, [Role.FINANCE_MANAGER])   # 二审

# 一致视图：单据 + 对应预算 + 通知，来自同一已提交时刻
snap = svc.get_snapshot("PO-100")
snap.order            # PurchaseOrderView（当前修订、状态、有效审批）
snap.budget           # BudgetView（limit_cents / used_cents）
snap.notifications    # 该分类季度的阈值通知（80% / 100%）

svc.close()           # 正常关闭后重开，状态与幂等回执仍然有效
```

## 业务规则

- 金额与预算均为正整数美分；预算不是硬上限，超额仍可批准。
- 审批层级按每次提交/改报后的金额重新判定：
  - `< 5000.00`：自动批准，立即入账；
  - `5000.00 ~ 10000.00`：采购负责人或财务经理一人批准；
  - `10000.00 ~ 25000.00`：仅财务经理一人批准；
  - `> 25000.00`：采购负责人首审 + 财务经理二审，两人身份必须不同（兼任两角色也不例外）。
- 审批只授权审批人看到的那个修订：改报后旧修订审批失效，新审批不得自动转用；
  每层只能成功推进一次，二审在首审前必拒绝；仅首审完成不算完全批准，不产生预算消耗。
- 完全批准（含自动批准）时将该修订全额计入其分类季度预算，且只记一次。
- 预算累计从低于 80% / 100% 变为达到或超过时各产生一条通知；一次跨过两个阈值产生两条；
  同一分类季度每个阈值只通知一次。
- 幂等：提交、改报、审批均带 `(调用者, 幂等键)`。同键同参数返回首次完成的原回执
  （含业务拒绝）；同键不同参数拒绝且不改变原结果；回放优先于当前状态检查。

## 并发与持久化保证

- 每次写操作都在单个 `BEGIN IMMEDIATE` 事务中完成：状态推进、预算入账、通知与幂等回执
  一起生效或完全不生效；多实例并发等价于某个串行执行顺序。
- `get_snapshot` 在单个读事务中完成，不会观察到单据与预算只完成一半的状态。
- 使用 WAL 模式与 30s busy_timeout；正常关闭重开后状态与幂等回执保持有效
  （不保证执行中强杀进程的场景）。

## 验收测试

- `tests/test_tiers.py`：金额门槛、审批层级、兼任角色的双人要求。
- `tests/test_resubmit_idempotency.py`：首审后改报换分类、旧回执重放、拒绝保持、预算只记一次。
- `tests/test_notifications_persistence.py`：80%/100% 阈值通知、跨阈值双通知、关闭重开。
- `tests/test_concurrency.py`：改报与末审竞争、16 线程混合负载与重复请求、最终一致性校验。
- `tests/test_scale.py`：1 万张采购单、16 线程各自持有库实例操作同一 SQLite 文件。
