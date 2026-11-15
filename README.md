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

src/storage_dispatch/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前业务与样例
# Fixed-slot storage energy accounting v1

面向储能数据分析者，回放明确的离散时槽电网侧能量请求。capacity为1..10^9、initial为0..capacity、max_charge/max_discharge为0..10^9整数能量单位/槽，不把它们当功率；requests最多20000个±10^9整数，正数请求充电、负数请求对外放电。效率分子分母整数1..10^6，0<效率<=1，使用精确Fraction。
充电接受网侧min(请求,充电限额,(容量-SOC)/充电效率)，SOC增加接受能量×效率；放电交付min(请求绝对值,放电限额,SOC×放电效率)，SOC扣交付/效率。每槽只有一种方向，不同时充放，未满足请求按电网侧列示。输出前后SOC、网侧充放量、损耗和未满足量，另核全程守恒：initial+grid_in-grid_out-loss=final。
样例final100，网侧累计充电175、放电48、损耗47、未满足97。不优化电价、不推断实际电池安全、退化或功率动态；无设备控制，所有输入虚构。

