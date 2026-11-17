# 多电池供能来源追溯（provenance）契约

面向园区储能运营者，回答“每个来源的能量最终送给了谁、损失在哪里、还存在何处”。
仅使用 Python 标准库，全部能量与比例以精确 `Fraction` 计算，不使用浮点、不采样、
不做拓扑排序丢边。新业务不读取预约簿、计量库或账单，不求最优计划，也不推断未知
电表读数；v1 回放、预约簿与计量接口保持不变。

## 1. CLI 与 API

```sh
storage-dispatch provenance --source <输入.json> --output <报告.json>
```

失败退出码 2 且保留已有输出；成功时以临时文件 + 原子替换发布，输入/输出同路径与
输出符号链接一律拒绝。输入上限沿用 8 MB。

可独立调用的 Python API（`storage_dispatch.provenance`）：

| API | 作用 |
| --- | --- |
| `trace_provenance(raw: bytes) -> dict` | 批量入口：严格 JSON 字节进，完整报告出，含源字节 SHA256 |
| `trace_document(document: dict, source_sha256=None) -> dict` | 对已解析文档执行与批量完全相同的 feed/finish 路径 |
| `TraceSession(document)` / `.feed(frame)` / `.finish()` | 逐槽会话；`feed` 成功才推进槽号并提交状态，失败不改变任何设备状态、来源库存或槽号，可修正后重试 |
| `solve_slot(...)` | 单槽精确母线混合求解器，可单独调用 |
| `validate_document` / `validate_frame` / `parse_strict_json` | 严格校验 |
| `ProvenanceError` / `Unbalanced` / `AmbiguousOrigin` | 异常类型 |

`TraceSession.feed` 使用初始化时该槽的设备请求；`finish` 只允许在全部槽成功后调用，
否则抛 `ProvenanceError`。批量 API 与逐槽 feed/finish 产出同构且字节稳定的报告。

长轨迹内存控制：`TraceSession(document, retain_slots=False)` 不在实例上保留逐槽
报告——每槽报告仍由 `feed` 返回供调用者即时消费（写文件或校验后丢弃），`finish`
只返回 `devices`、`source_summary` 与空的 `slots`，从而让 256 槽级轨迹的内存占用
保持平坦；逐来源汇总与电池末库存始终保留。默认 `retain_slots=True` 的行为不变。

## 2. 输入 schema

顶层严格为 `{"buses", "devices", "frames"}`，未知/重复字段、布尔冒充整数、浮点、
非规范有理数字符串与非法 UTF-8 全部整体拒绝。

* `buses`：1..32 个不同母线 ID。
* `devices`：1..16 项，每项严格为 `{device_id, bus, device, initial_origins}`。
  * `device` 是现有 v1 完整源对象（`capacity, initial, max_charge,
    max_discharge, charge_efficiency, discharge_efficiency, requests`），
    整数与效率范围沿用 v1；各设备 `requests` 长度相同且为 1..256。
  * `initial_origins` 为来源 ID → 规范非负有理数字符串的对象，其总量必须
    精确等于 `device.initial`；允许零值条目，零值来源在输出中省略。
* `frames`：长度等于槽数。第 t 项严格为 `{injections, links, sinks}`。
  * `injections`：每项 `{source_id, bus, energy}`，同槽 `(source_id, bus)` 不重复，
    每槽至多 64 项。
  * `links`：每项 `{link_id, from_bus, to_bus, sent, efficiency}`，
    `link_id` 同槽唯一，每槽至多 128 条；禁止自环，允许不同 ID 的平行链路、
    反向链路与跨母线环路。
  * `sinks`：每项 `{sink_id, bus, energy}`，`sink_id` 同槽唯一，每槽至多 64 个。
* 全输入至多 64 种来源；所有 ID 为 1..64 位 ASCII 字母数字、下划线或连字符；
  引用必须存在，数组/对象内 ID 不得重复。
* `energy`、`sent` 为 0..10^12 的规范 `Fraction` 字符串，分母至多 10^6；
  `efficiency` 同为规范字符串且 `0 < efficiency <= 1`。

## 3. 电池库存物理（与 v1 共用同一步进）

逐槽物理步进抽自 `dispatch.simulate`，现位于 `physics.step_request`，v1 回放与
TraceSession 共用，接受量、SOC、损耗与 unmet 不可能出现两套结果。规则仍为按请求、
设备限额与 SOC 饱和裁剪。

每块电池另维护按来源的内部能量库存，其和始终精确等于物理 SOC：

* 放电：槽初库存 `I_s`、SOC `S`、实际网侧交付 `d`。来源库存扣除
  `(d/eta_d) * I_s/S`，网侧输出 `d * I_s/S`，二者之差为该来源放电损耗；
  `S = 0` 时交付只能为 0，不做除法。每槽放电只使用该槽开始时的库存组成。
* 充电：实际接受 `c`，接入母线的来源比例 `p_s`，库存增加 `eta_c * c * p_s`，
  充电损耗 `(1 - eta_c) * c * p_s`。
* 零请求与空库存不产生来源；跨槽只传递电池库存，上一槽母线比例绝不缓存沿用。

## 4. 母线混合与环路求解

每槽所有母线无库存且完全混合：所有离开同一母线的正流量具有同一来源比例，
不按输入顺序先到先得。令 `Q_b` 为母线 b 总取走量（出向链路 `sent` + 电池实际
充电 + 外部 sink），对每个来源 s 同槽联立：

```
Q_b * p[b][s] = 外部注入[b][s] + 电池网侧放电[b][s]
              + Σ(链路 a→b: sent * efficiency * p[a][s])
```

* 链路从上游取走 `sent`，下游到达 `sent * efficiency`，差值为链路损耗并继承
  上游来源比例；零流量链路不参与传播，无流量母线输出空组成。
* 电池放电是母线注入；电池实际充电与外部 sinks 是母线取走量。配平只使用物理
  步进的实际接受量，不能用请求量配平、调整链路量或补造电源。
* 每个母线必须精确满足总到达量 = 总取走量，否则报 `unbalanced`。
* 含环母线按同槽共同混合状态，用精确 Fraction 的 Gauss–Jordan 消元一次求解
  （同一系数矩阵按来源复用），不拓扑排序、不迭代、无浮点容差。
* 系数矩阵奇异意味着某个正流量闭环分量没有任何外部或电池来源可到达：即使能量
  配平也报 `ambiguous_origin`，不任取比例、不默认绿电。同网另有正常供能分量
  也不能掩盖该歧义。不平衡与歧义并存时先报不平衡。

## 5. 输出 schema

报告为 JSON 对象：

* `schema_version`：固定 `2`。
* `source_sha256`：输入原始字节 SHA256（批量 API 与 CLI）。
* `buses`：稳定排序的母线 ID。
* `devices`：每块电池 `{device_id, bus, initial, final, initial_origins,
  final_origins}`，origins 为 `{source_id, energy}` 列表并按来源 ID 排序。
* `slots`：逐槽数组，槽号为结构整数，每槽含：
  * `slot`；
  * `devices`：逐设备物理回放字段 `request, before, after, grid_in,
    grid_out, loss, unmet`，以及 `before_origins, after_origins,
    input_origins（网侧充入来源量）, output_origins（网侧放出来源量）,
    conversion_loss_origins（转换损耗来源量）`；
  * `buses`：每母线 `{bus, withdrawal, shares:[{source_id, share}]}`；
  * `links`：每链路 `{link_id, from_bus, to_bus, sent, arrived, loss,
    arrival_origins, loss_origins}`，按 link_id 排序；
  * `sinks`：每终端 `{sink_id, bus, energy, origins}`，按 sink_id 排序；
  * `injections`：外部注入回显（含来源标注），按 `(source_id, bus)` 排序。
* `source_summary`：按来源 ID 排序，每来源
  `{source_id, initial, external_injection, sink_delivery, link_loss,
  conversion_loss, total_loss, final_inventory, conservation_residual}`。

除槽号等结构整数外，所有数值均为规范 `Fraction` 字符串；零值来源条目省略。
内部链路的循环吞吐不重复计为新增来源或终端消耗。

## 6. 逐来源守恒

对每个来源必须精确成立（残差恰为 `"0"`，以 Fraction 精确比较）：

```
初始量 + 外部注入 = 终端交付 + 链路损耗 + 转换损耗 + 末库存
```

## 7. 异常

| 异常 | 触发条件 |
| --- | --- |
| `ProvenanceError` | 全部 schema/域校验失败与会话误用（如全部槽完成后再 feed、未完成即 finish） |
| `Unbalanced` | 某母线总到达量 ≠ 总取走量，或解出负比例/比例和不为 1 |
| `AmbiguousOrigin` | 正流量闭环无可到达外部/电池来源，组成不唯一 |

歧义与不平衡同时存在时先报 `Unbalanced`。CLI 捕获这些异常与 JSON/IO 错误后
输出 stderr 并以退出码 2 结束。
