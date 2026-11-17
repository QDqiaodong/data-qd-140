# data-qd-140 · 储能调度能量核算

Exact fixed-slot storage dispatch with losses and unmet requests。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/storage-dispatch --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

测试覆盖 v1 回放、预约簿规划器（重叠窗口抢占、禁止贪心、同成本规范平局、非整数效率）、
独立顶点枚举 oracle、SQLite 预约簿（关闭重开、部分交付后取消保留执行事实、请求冲突/回滚、
提交后导出失败重试）、真实独立进程的两种锁策略竞争与防超售，以及不缩小输入边界的
192 槽/80 合约非枚举实例：

```sh
python3 -m unittest discover -s tests -v
```

src/storage_dispatch/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 可执行的合约预约簿（v2）

`storage_dispatch.reserve` 为需求响应运营提供可执行的合约预约簿：多份交付窗口重叠的放电合约
共享同一块电池，由精确 Fraction 线性规划统一决定每槽网侧充电与每合约交付，先最小化购电成本，
再按槽升序、每槽先充电后按 contract_id ASCII 升序的交付向量取字典序最小规范解。接口与 JSON
schema 见 [预约簿契约](docs/reservations.md)。仅用 Python 标准库（含 sqlite3）。

```sh
# 建簿（新库；无合约也必须形成可行计划）
storage-dispatch reservations --db run/book.db init fixtures/reserve_init.json
# 新增 / 取消 / 推进，请求体可来自文件或标准输入（-）
echo '{"request_id":"r1","contract_id":"C-A","start":1,"end":4,"energy":20}' \
  | storage-dispatch reservations --db run/book.db add -
storage-dispatch reservations --db run/book.db advance run/advance.json
storage-dispatch reservations --db run/book.db cancel run/cancel.json
storage-dispatch reservations --db run/book.db status
```

并发写有两种等价于某串行序的策略：默认 `external`（锁外求解、提交前按新状态重算，旧解即使仍
可行也不冒充新最优）与 `transaction`（整段求解持写锁，竞争写入等待/重试），均防止并发超售：

```sh
storage-dispatch reservations --db run/book.db --strategy transaction add run/add.json
```

请求按完整原始 UTF-8 JSON 字节摘要幂等：同字节跨进程重试返回原回执，异字节冲突，失败不占用 ID。
状态、规范计划、执行前缀、revision 与回执在同一 SQLite 事务提交。

## 相关计量与联合票额稽核（metering）

`storage_dispatch.metering` 为储能运营数据核账提供“相关计量与联合票额稽核”：输入是已经发生的电表
观测（至多 64 项，1..24 槽）与已经开出的账单（至多 4 张票），在一个由设备限额、双向效率、SOC 耦合
与全部观测共同定义的**完整有理可行域**上做精确 LP 判定与票额反演。不读取预约簿、不把计划/合约当
计量事实、不执行设备控制；方向/效率/SOC 契约与 v1 回放共用 `physics.py`，仅复用通用精确 LP 引擎，
不把未来调度规划器当计量曲线。Schema、API、状态机与错误边界见
[计量稽核契约](docs/metering.md)。

```sh
storage-dispatch metering ingest --db run/metering.db --source fixtures/metering_source.json
storage-dispatch metering audit  --db run/metering.db --dataset <SHA256> \
    --invoices fixtures/metering_invoices.json --output run/audit.json
```

状态为 `possible` / `impossible` / `inconsistent`；正需量费的峰值必须被真实窗口达到（自由 peak
反例会被拒绝），联合结论要求同一条全槽曲线同时满足全部票额等式，冲突报告给出极小集合与逐个移除
后的全槽见证。数据库审计连接只读，失败退出 2 且保留旧输出，拒绝同路径与符号链接输出。

## 多电池供能来源追溯（provenance）

`storage-dispatch provenance` 回答每个来源的能量最终送给了谁、损耗在哪里、还存在何处。输入严格为
`{buses, devices, frames}`（1..32 母线、1..16 设备、1..256 槽、至多 64 来源、每槽至多 64 注入/
128 链路/64 终端），全部能量为规范 Fraction 字符串，仅用标准库精确求解，不读取预约簿、计量库或账单，
不求最优计划，也不推断未知电表读数。Schema、输出与异常的完整定义见
[来源追溯契约](docs/provenance.md)。

```sh
storage-dispatch provenance --source fixtures/provenance_demo.json \
    --output run/provenance_report.json
```

也可在代码中独立调用批量 API（严格 JSON 字节进、含源字节 SHA256 的完整报告出），或逐槽
`TraceSession(document).feed(frame)` 后 `finish()`：

```python
from storage_dispatch.provenance import trace_provenance, TraceSession
report = trace_provenance(raw_bytes)

session = TraceSession(document)
for frame in document["frames"]:
    session.feed(frame)          # 失败不改变任何设备状态、来源库存或槽号，可修正后重试
report = session.finish()        # 仅在全部槽成功后允许调用
```

逐槽物理步进抽自 v1（`physics.step_request`），v1 回放与 TraceSession 共用同一方向判断、限额与
SOC 饱和裁剪，接受量与损耗不可能出现两套结果；电池另维护按来源的内部库存，其和始终精确等于物理
SOC。母线每槽无库存、完全混合，含环网络按同槽共同混合状态联立精确 Gauss–Jordan（Fraction，无浮点、
无拓扑丢边、无迭代收敛），矩阵奇异即正流量闭环无可到达来源，报 `ambiguous_origin`；能量不平衡先报
`unbalanced`。逐槽全部计算在局部副本上完成，只有整槽成功才提交 SOC 与来源库存，因此失败 feed 不污染
状态。长轨迹可用 `TraceSession(document, retain_slots=False)` 流式处理：每槽报告由 `feed` 返回后即
可消费丢弃，`finish` 仅回汇总，内存占用不随槽数增长。每个来源满足
`初始量 + 外部注入 = 终端交付 + 链路及转换损耗 + 末库存`，残差恰为 0，循环吞吐不重复累计。

不缩小输入边界的可执行样例（32 母线/16 设备/64 来源/128 链路/256 槽，含损环路与接近 1 的分数
效率）由生成器构造并在测试中自动验收：

```sh
PYTHONPATH=src python3 tools/provenance_sample.py run/provenance_input.json \
    run/provenance_full_report.json
```

## 当前业务与样例
# Fixed-slot storage energy accounting v1

面向储能数据分析者，回放明确的离散时槽电网侧能量请求。capacity为1..10^9、initial为0..capacity、max_charge/max_discharge为0..10^9整数能量单位/槽，不把它们当功率；requests最多20000个±10^9整数，正数请求充电、负数请求对外放电。效率分子分母整数1..10^6，0<效率<=1，使用精确Fraction。
充电接受网侧min(请求,充电限额,(容量-SOC)/充电效率)，SOC增加接受能量×效率；放电交付min(请求绝对值,放电限额,SOC×放电效率)，SOC扣交付/效率。每槽只有一种方向，不同时充放，未满足请求按电网侧列示。输出前后SOC、网侧充放量、损耗和未满足量，另核全程守恒：initial+grid_in-grid_out-loss=final。
样例final100，网侧累计充电175、放电48、损耗47、未满足97。不优化电价、不推断实际电池安全、退化或功率动态；无设备控制，所有输入虚构。

