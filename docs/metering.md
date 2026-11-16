# 相关计量与联合票额稽核（storage-dispatch metering）

面向储能运营**数据核账**的新业务：输入是已经发生的电表观测与已经开出的账单，输出是测量可行域上的
精确稽核结论。它与 v1 回放、v2 预约簿并存但互不读取：

* 不读取预约簿，不把计划或合约当作计量事实，不执行任何设备控制；
* 不把未来调度规划器（`storage_dispatch.reserve.planner`）当作实际计量曲线，只复用通用的精确 LP
  引擎 `storage_dispatch.reserve.simplex`；
* 方向、网侧限额、效率与 SOC 换算只有一份实现（`storage_dispatch.physics`），v1 饱和回放与新计量域
  共用，不存在第二套效率公式。

全部计算使用 `fractions.Fraction`：能量、SOC、损耗、费用与见证数值都是精确有理数，输出为规范
`str(Fraction)` 字符串；结构性下标保持整数；不离散化能量、不用浮点容差或采样判定无解。仅用 Python
标准库（含 `sqlite3`）。

## 命令行

```sh
# 入库：严格读取 {device, readings}，整次原子写入 SQLite
storage-dispatch metering ingest --db run/metering.db --source fixtures/metering_source.json

# 稽核：数据库只读快照 + 至多 4 张票，原子替换输出 JSON
storage-dispatch metering audit --db run/metering.db \
    --dataset <SHA256> --invoices fixtures/metering_invoices.json \
    --output run/audit.json
```

* `--source` / `--invoices` 可用 `-` 表示标准输入；
* 校验/可行性失败打印错误并以退出码 2 结束，已存在的输出文件保持不变；
* 审计以 `mode=ro` 只读 URI 打开数据库，绝不写源数据；
* 拒绝输入输出同路径、拒绝输出符号链接；成功后 `os.replace` 原子替换；
* 相同原始字节复用同一 SHA 数据集，不覆盖任何其它数据集。

## 可独立调用的 Python API

```python
from storage_dispatch.metering import (
    run_ingest, run_audit,          # 端到端编排（CLI 也走这里）
    MeteringDomain, BillingSolver,  # 计量域 / 账单稽核，无 SQLite 依赖亦可调用
    validate_ingest, validate_invoices, parse_strict_json,
)
from storage_dispatch.metering.store import open_snapshot, load_dataset
```

`MeteringDomain(device, readings, slots)`：

* `is_feasible(reading_indices=None, extra_rows=())`；
* `witness(...)`：返回**全槽**见证（每槽 `e/import/export/loss` 与前后 SOC，外加全程
  `conservation_residual`）；
* `verify_witness(witness, reading_indices=None)`：独立代回全部设备/SOC/观测约束复核；
* `reading_conflict()`：测量域不可行时返回极小冲突 `reading_id` 集合与逐个移除后的全槽见证。

`BillingSolver(domain, invoices)`：

* `invoice_bounds(i)`：第 *i* 张票在**整个测量可行域**上的精确最小值/最大值及各自的全槽达到见证；
* `joint_witness(subset=None)`：是否存在**同一条**测量曲线同时满足全部 `F(e)=amount`；
* `joint_conflict()`：极小冲突票集合与逐个移除后的共同全槽见证（原始计量观测全部保留）；
* `evaluate(invoice, witness)`：从见证独立重算 `F` 与真实峰值。

## 入库 Schema（严格 JSON 字节）

拒绝重复对象键、`NaN`/`Infinity` 常量、未知/重复字段与非法 UTF-8；整数位置拒绝布尔值，浮点不能
冒充整数或有理数。

```json
{
  "device": {
    "capacity": 10,
    "initial": 0,
    "max_charge": 10,
    "max_discharge": 10,
    "charge_efficiency": {"numerator": 1, "denominator": 1},
    "discharge_efficiency": {"numerator": 1, "denominator": 1},
    "requests": [10, 10]
  },
  "readings": [
    {"reading_id": "M00", "channel": "import", "start": 0, "end": 2,
     "lower": "10", "upper": "10"}
  ]
}
```

* `device` 直接沿用 v1 源对象与字段范围；本入口 `requests` 限定 **1..24** 槽，元素为 ±10⁹ 整数，
  正/零请求为充电方向，负请求为放电方向；
* `readings` 至多 **64** 项，每项严格为
  `{reading_id, channel, start, end, lower, upper}`；
* `reading_id` 为 1..64 位 ASCII 字母数字、下划线或连字符（`[A-Za-z0-9_-]{1,64}`）且不重复；
* `channel ∈ {import, export, loss}`，`0 <= start < end <= 槽数`；
* `lower/upper` 为非负**规范有理数字符串**（即 `str(Fraction)` 形式，分母 ≤ 10⁶，值 ≤ 10¹²），
  `lower <= upper`；`"1.5"`、`"3/1"`、`"1e2"`、带空格等非规范形式一律拒绝；
* 结构合法但相互矛盾的观测照样入库，供后续诊断（测量域在稽核阶段判定 `inconsistent`）。

### SQLite 表（独立于预约簿表）

| 表 | 内容 |
| --- | --- |
| `metering_dataset` | `dataset_sha`（原始字节 SHA256 主键）、`raw_bytes`、`created_at` |
| `metering_device` | 设备参数、效率规范串、槽数、请求 JSON |
| `metering_slot` | 每槽请求与方向（`charge`/`discharge`） |
| `metering_reading` | 观测序号、ID、通道、窗口、上下界规范串 |

## 测量可行域（相关，而非各槽独立）

每槽只允许请求指定的方向。变量为每槽网侧接受能量 `e_t`（充/放各只存在对应方向的变量）与各边界
SOC `s_t`，全部非负：

* 网侧接受量 `0 <= e_t <= |请求|` 且不超过对应设备限额；
* 充电：`s_{t+1} = s_t + eta_c * e_t`，损耗 `e_t*(1-eta_c)`，`import=e_t`、`export=0`；
* 放电：`s_{t+1} = s_t - e_t/eta_d`，损耗 `e_t*(1/eta_d-1)`，`export=e_t`、`import=0`；
* `s_0 = device.initial`，所有边界 `0 <= s_t <= capacity`；
* 每条观测在其 `[start, end)` 窗口内对对应通道（import/export/loss）求和，要求落在 `[lower, upper]`。

SOC 状态把所有槽耦合为**一个完整有理多面体**：不插值补唯一曲线，也不用各槽独立上下界替代。
观测给出的是窗口总量，跨开票窗口与外部槽的观测会通过外部槽设备限额、SOC 边界收紧窗口内能量。

## 票额 Schema 与费用定义

`invoices` 是至多 **4** 项的数组，每项严格为
`{invoice_id, start, end, prices, window, demand_rate, amount}`：

* `invoice_id` 规则同 `reading_id` 且不重复；`0 <= start < end <= 槽数`；
* `prices` 长度等于 `end-start`，每项与 `demand_rate` 为 `0..10⁹` 整数；
* `1 <= window <= end-start`；`amount` 为非负规范有理数字符串。

```
F(e) = Σ prices[t-start] * import_t                     （start <= t < end）
     + demand_rate * max_k ( Σ_{k<=t<k+window} import_t / window )
```

需量是**窗口平均网侧充电能量（能量，不换算成功率）**；放电不计负充电费。

* 正需量费时引入 peak 变量与“peak ≥ 每个窗口均值”的支配行；极值与联合分支再用一条显式等式把
  peak 钉到**某个真实达到的窗口**——仅放一个不被任何窗口达到的自由 peak 不能证明票额可实现；
  多个并列峰值合法。`demand_rate=0` 时不引入 peak 变量也不附加峰值限制；
* 每张票报告整个测量可行域上的精确最小/最大值及各自全槽达到见证，**不要求**与 `amount` 相等；
  跨票可以使用不同的极值见证。

## 稽核状态与联合结论

| 状态 | 含义 |
| --- | --- |
| `possible` | 存在同一条测量曲线使所有票 `F(e)=amount` 同时成立；附该条共同全槽见证；空票数组在测量域可行时也是 `possible` |
| `impossible` | 不存在共同曲线；附极小冲突 `invoice_id` 集合与逐个移除任一票后的共同全槽见证，原始观测全保留 |
| `inconsistent` | 测量域本身不可行；只返回观测冲突与移除见证，不输出票额上下界或票冲突 |

联合判定不能用“每张票的 amount 各自落在其上下界内”替代；票额等式又反向收紧同一测量域来寻找共同
见证，不为每张票另建互不关联的曲线。即使内部消去不在任何票窗口中的槽，最终所有见证都能提升到满足
每条原始观测、设备限额、SOC 与效率换算的完整时间轴。

报告绑定 `dataset_sha256` / `source_sha256`（计量源）与 `invoice_source_sha256`（票源原始字节）。

## 24 槽 / 64 观测 / 4 票可执行样例

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install --no-build-isolation --no-deps .
storage-dispatch metering ingest --db run/metering.db \
    --source fixtures/metering_source.json
SHA=$(python3 -c "import sqlite3;print(sqlite3.connect('run/metering.db')\
    .execute('select dataset_sha from metering_dataset').fetchone()[0])")
storage-dispatch metering audit --db run/metering.db --dataset "$SHA" \
    --invoices fixtures/metering_invoices.json --output run/audit.json
python3 -m json.tool run/audit.json
```

样例含非单位双向效率（19/20、9/10）、跨充放电槽的 import/export/loss 观测与分数票额，稽核状态为
`possible`，共同见证 24 槽守恒残差为 `0`。

## 测试与独立 oracle

```sh
python3 -m unittest discover -s tests -v
```

`tests/_metering_oracle.py` 是独立小规模精确 oracle：解析消去 SOC，在网侧能量空间用自带高斯消元
枚举多面体顶点，覆盖逐票上下界与联合结论；它不导入被测求解器/规划器，处理连续有理域而非整数网格
抽样。验收还覆盖：关闭重开源字节/约束一致、空观测、矛盾观测、窗口外放电与 SOC 边界收紧、正/零
需量费、并列峰、分数输入、虚高自由 peak 反例、需量峰所属窗口改变、票额等式导致的非凸可行分支、
新旧物理契约逐项一致，以及上述 24 槽全规模样例。
