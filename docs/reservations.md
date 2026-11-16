# 可执行的合约预约簿 v2（storage-dispatch reservations）

面向储能需求响应运营的预约簿（reservation book）。多份交付窗口重叠的放电合约共享同一块电池，
由一个精确线性规划器统一决定每槽网侧充电 `c_t` 与每合约交付 `x_{i,t}`，而不是把请求逐条回放。
全部计算使用 `fractions.Fraction`，不取整、不离散化能量、不用浮点判可行；所有有理数在输出中
使用规范 Fraction 字符串（如 `"25/8"`、`"0"`、`"3"`）。

v1 的固定时槽回放 API 与命令保持不变（见 `contract.md` 与 `replay` 子命令）。

## 命令行

```sh
storage-dispatch reservations --db DB [--strategy external|transaction]
                              [--timeout SECONDS] ACTION [REQUEST_FILE|-]
```

* `--db DB`：SQLite 数据库文件路径。`init` 仅允许作用于尚不存在表的新库。
* `--strategy`：并发写策略，默认 `external`（锁外求解，提交前按新状态重算）；
  可选 `transaction`（整段求解持写锁，竞争写入等待/可重试）。
* `ACTION`：`init | add | cancel | advance | status`。
* 请求 JSON 从文件读取，`-` 表示标准输入；`status` 不需要请求体。
* 成功在标准输出打印回执 JSON；校验或可行性失败打印错误并以退出码 2 结束，不改动数据库。

## Python API

```python
from storage_dispatch.reserve import ReservationBook

book = ReservationBook("book.db", strategy="external")   # 或 "transaction"
revision, receipt = book.initialize(init_bytes)
revision, receipt = book.add_contract(add_bytes)
revision, receipt = book.cancel_contract(cancel_bytes)
revision, receipt = book.advance(advance_bytes)
snapshot = book.status()          # 一致快照，不触发任何执行
```

`storage_dispatch.reserve.planner.plan(...)` 与
`storage_dispatch.reserve.simplex.Simplex` 可被独立调用，不依赖 SQLite。

## 请求 JSON Schema

所有请求都是严格的 UTF-8 JSON 字节：拒绝重复键、`NaN`/`Infinity` 等非有限常量、
未知字段与重复字段；整数位置不接受布尔值。ID 为 1..64 位 ASCII 字母、数字、下划线或连字符
（正则 `[A-Za-z0-9_-]{1,64}`）。

### init

```json
{
  "capacity": 100,
  "initial": 10,
  "charge_efficiency": {"numerator": 4, "denominator": 5},
  "discharge_efficiency": {"numerator": 4, "denominator": 5},
  "terminal_min": 5,
  "slots": [
    {"charge_limit": 50, "discharge_limit": 0, "price": 1},
    {"charge_limit": 0, "discharge_limit": 20, "price": 7}
  ]
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `capacity` | 整数 1..10^9 |
| `initial` | 整数 0..capacity |
| `charge_efficiency` / `discharge_efficiency` | `{numerator, denominator}`，分子分母均为 1..10^6 整数且分子 ≤ 分母（0 < η ≤ 1） |
| `terminal_min` | 整数 0..capacity，末槽后 SOC 下限 |
| `slots` | 数组，长度 1..192，按数组位置编号为槽 0..N-1 |
| 每槽 `charge_limit` / `discharge_limit` | 整数 0..10^9，二者不能同时为正 |
| 每槽 `price` | 非负整数 0..10^9 |

init 不含 `requests`，也没有旧的全局充放限额。初始 `cursor=0`。即使没有任何合约也必须形成
可行计划，否则整个 init 失败且不建表。成功后 `revision=0`（init 本身不递增版本）。

### add

```json
{"request_id": "r-1", "contract_id": "C-A", "start": 1, "end": 4, "energy": 20}
```

* `start`、`end` 为整数且 `cursor <= start < end <= 槽数`；
* `energy` 为 1..10^9 的整数，表示在窗口 `[start, end)` 必须恰好交付的网侧能量单位；
* `contract_id` 永不复用；全簿最多 80 份合约（含已取消与已完成）；
* 若无可行解，整次 add 不入簿、不占用 `request_id`、`revision` 不变。

### cancel

```json
{"request_id": "r-2", "contract_id": "C-A"}
```

仅取消 active 合约的未交付部分。已执行的交付与购电成本永久保留，未来交付归零。
已完成或已取消的合约不能再次取消。

### advance

```json
{"request_id": "r-3", "to_slot": 8}
```

仅接受 `cursor < to_slot <= 槽数`。把当前规范计划中 `[cursor, to_slot)` 的逐槽充电、放电及
各合约交付冻结为不可改的执行事实，再把 `cursor` 推进到 `to_slot`；后续规划从该前缀的实际末
SOC 与各合约剩余量起算。

## 规划模型

对尚未执行的时槽，规划器同时决定每槽网侧充电 `c_t ≥ 0` 与每合约交付 `x_{i,t} ≥ 0`：

* 槽限额：`0 ≤ c_t ≤ charge_limit_t`；`sum_i x_{i,t} ≤ discharge_limit_t`；
* 合约仅在自身窗口内交付，窗口外 `x_{i,t}=0`，且窗口内总和恰好为剩余能量；
* 共享放电限额是对同一槽所有合约交付之和的约束；
* SOC 递推：`SOC_{t+1} = SOC_t + eta_c * c_t - sum_i x_{i,t} / eta_d`；
* 始终 `0 ≤ SOC_t ≤ capacity`，末槽后 `SOC ≥ terminal_min`；
* 不允许任何额外放电（放电只能来自合约交付）。

目标按字典序：

1. 先最小化购电成本 `sum_t price_t * c_t`；
2. 成本相同的最优解中，按槽升序、每槽先 `c_t` 后按 `contract_id` ASCII 升序的 `x` 向量，
   取字典序最小解。

每次状态变化（add/cancel/advance）后都对尚未执行部分重新整体规划，不逐合约贪心，也不把旧预约
的未来分配锁死。两阶段精确单纯形（Bland 反循环 + 字典序层级目标）保证终止并返回该唯一规范解。

## status 输出 Schema

`status()` 在单个一致事务（快照）内返回，不触发执行：

```json
{
  "revision": 3,
  "cursor": 2,
  "capacity": 100,
  "terminal_min": 5,
  "eta_c": "4/5",
  "eta_d": "4/5",
  "prefix_end_soc": "45/2",
  "contracts": [
    {"contract_id": "C-A", "start": 1, "end": 4, "energy": 20,
     "state": "active|cancelled|completed",
     "delivered": "6", "remaining": "14"}
  ],
  "execution": [
    {"slot": 0, "charge": "25/8", "discharge_total": "0",
     "soc_before": "10", "soc_after": "45/2", "deliveries": {}}
  ],
  "plan": {
    "slots": [
      {"slot": 2, "charge": "0", "discharge_total": "18",
       "soc_before": "45/2", "soc_after": "9/2", "price": 7,
       "deliveries": {"C-A": "10", "C-B": "8"}}
    ],
    "contracts": {"C-A": {"planned": "14", "remaining": "0"}}
  },
  "receipts": [
    {"request_id": "r-1", "digest": "sha256-of-raw-bytes",
     "operation": "add", "revision": 2}
  ]
}
```

`execution` 仅含已冻结前缀；`plan.slots` 覆盖从当前 `cursor` 起的规范计划。所有 SOC、充电、
放电、交付量均为规范 Fraction 字符串。

## 幂等、冲突与并发

* 每次成功 add/cancel/advance 各把 `revision` 递增一次。
* `request_id` 绑定该操作完整原始 UTF-8 JSON 字节的 SHA-256 摘要：
  同进程或跨进程用相同字节重试，返回原成功回执且不重复执行；
  同一 `request_id` 提交不同字节则报冲突；失败不占用 ID。
* 状态、规范计划、执行前缀、`revision` 与成功回执在同一个 SQLite 事务内提交。
* 提交后即使回执文件导出失败，预约也已持久化；重试仍拿到同一回执。
* 独立连接/进程竞争的结果等价于某个提交串行序：
  * `external`：锁外算出候选解后，提交前在写锁内重新读取状态；若预约集合、cursor 或剩余义务
    被其它提交改变，则依据新状态重新求解——旧解即使仍可行也不能冒充新状态的最优解。
  * `transaction`：求解全程持写锁，竞争写入在锁上等待（`SQLITE_BUSY`，由 busy timeout 承接）
    或重试；原事务提交后再成功完成，并得到对应串行顺序的规范最优解。
  * 两种策略下并发新增都不能超售共享放电限额。

## 持久化表

`meta`（参数与 cursor/revision）、`slot`（槽定义）、`contract`（原始合约、状态、累计交付）、
`execution`（不可改执行前缀及逐合约明细）、`plan_slot` 与 `plan_contract`（当前规范计划）、
`receipt`（请求摘要与回执）。取消后重规划可让其它 active 合约改用被释放的便宜时槽，但任何重排都
不得改动已执行事实，即使全时域重算更便宜。
