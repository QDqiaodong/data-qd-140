# Fixed-slot storage energy accounting v1

面向储能数据分析者，回放明确的离散时槽电网侧能量请求。capacity为1..10^9、initial为0..capacity、max_charge/max_discharge为0..10^9整数能量单位/槽，不把它们当功率；requests最多20000个±10^9整数，正数请求充电、负数请求对外放电。效率分子分母整数1..10^6，0<效率<=1，使用精确Fraction。
充电接受网侧min(请求,充电限额,(容量-SOC)/充电效率)，SOC增加接受能量×效率；放电交付min(请求绝对值,放电限额,SOC×放电效率)，SOC扣交付/效率。每槽只有一种方向，不同时充放，未满足请求按电网侧列示。输出前后SOC、网侧充放量、损耗和未满足量，另核全程守恒：initial+grid_in-grid_out-loss=final。
样例final100，网侧累计充电175、放电48、损耗47、未满足97。不优化电价、不推断实际电池安全、退化或功率动态；无设备控制，所有输入虚构。
