# batch023 加权退火温度诊断（v1）

2026-10-15。仅以标准库读取 batch023 冻结源码、保存配置和完整修复记录并做
`math.exp/log` 运算；不运行优化器、不改变旧源码或历史结果。对应可检查数字与
源文件 SHA 见 `batch023_weighted_temperature_diagnosis_v1.json`。

## 实际接受规则及单位

冻结 `weighted_solver.zip/uav_dispatch/alns.py::_accept_worse` 的首分支是
`if _weighted_adapter.active: return _weighted_adapter.accept(...)`。适配器实际计算：

`ΔJ = .0025 ΔN + .0000560012816944 ΔL + .000322259147822 ΔD`

`ΔJ<=0` 接受；`ΔJ>0` 以 `exp(-ΔJ/max(T,1e-12))` 接受。这里 T 与当前加权 J
处于同一尺度，未走原 lex 接受函数里分 N/L/D 的归一化差值。示例若只增加1件
迟到、其余不变，ΔJ=.0025；真实移动必须计算三项有正有负的净和。

旧 pin helper 在普通 ALNS 接受前只拦截非法回滚/已到期；正常有效候选委托上述
冻结 `_accept_worse`。batch023记录的回滚均为0，所以没有pin拒绝异常解释该轮无收益。

## 短叶片里的实际温度

旧有效配置是 T0=.03、Tmin=.0005、α=.995、max_iterations=400，四项均来自
保存的每个 leaf `helper_metadata.configuration`。helper同时强制
`enable_budget_cooling=False`，因此按完成迭代数降温，未按剩余墙钟时间重排端点。

第 k 次接受判断前：`T=max(Tmin,T0*α**(k-1))`；完成 k 次更新后：
`T=max(Tmin,T0*α**k)`。新研究 profile 是 T0=.0025、Tmin=.00001、α=.97、
max_iterations=1,000,000；迭代上限变大用于让绝对截止时间控制叶片长度，非增加墙钟预算。

下表每行是“已完成 k 次更新，下一次接受判断”的温度/概率。概率只针对固定的
净 ΔJ，不是实际运行接受率。

| 已完成更新 k | 旧 T | 新 T | 旧 P(ΔJ=.0025) | 新 P(ΔJ=.0025) | 旧 P(ΔJ=.01) | 新 P(ΔJ=.01) |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0.03 | 0.0025 | 0.920044 | 0.367879 | 0.716531 | 0.0183156 |
| 50 | 0.023349377 | 0.00054516344 | 0.898463 | 0.0101958 | 0.651631 | 1.08064e-08 |
| 100 | 0.018173113 | 0.00011888127 | 0.871477 | 7.36299e-10 | 0.576798 | 2.93911e-37 |
| 182 | 0.012048194 | 1e-05 | 0.812613 | 2.66919e-109 | 0.436049 | 0 |
| 399 | 0.0040600415 | 1e-05 | 0.540232 | 2.66919e-109 | 0.0851766 | 0 |
| 400 | 0.0040397413 | 1e-05 | 0.538563 | 2.66919e-109 | 0.0841288 | 0 |

因此，旧第400次接受判断实际使用 T=.0040600415；完成400次之后 T=.0040397413，
并非Tmin。旧配置需要817次更新才能到下限，400次上限内到不了；新配置182次更新
到下限，第183次接受判断开始使用Tmin。表中的0是 `exp(-1000)` 的浮点下溢；
数学上概率仍为正但极小。

低温显著抑制大幅净劣化，也更容易困在局部最优。这里不是判定原default存在程序
bug；它是在特定加权尺度、短leaf预算与接近局部优良输入下值得检验的搜索参数假设。

## 保存证据说明搜索确实在走

| arm / leaf | 已完整验证候选 | rollback | 报告迭代 | 总接受数 | 保存的8个候选J范围 |
|---|---:|---:|---:|---:|---:|
| full_credit_pin_direct / 1 | 400 | 0 | 400 | 321 | 0.438635–0.460288 |
| full_credit_pin_direct / 2 | 400 | 0 | 400 | 323 | 0.451942–0.501566 |
| full_credit_pin_relay / 1 | 361 | 0 | 361 | 291 | 0.450085–0.475656 |
| full_credit_pin_relay / 2 | 354 | 0 | 超时未生成summary | 未报告 | 0.456650–0.482408 |

所有32个保存修复快照的路线都不同于各自原macro，且均通过完整检查。DIRECT 两叶
总接受率为321/400=80.25%、323/400=80.75%；RELAY首叶291/361≈80.61%。这些总数
包含改善与相等候选，不能当成“劣解接受率”。第二个RELAY叶虽有354个有效候选，
但因截止异常未生成ALNS summary，不能擅自补出其精确已接受数或完成迭代数。
保存8条快照也不是400次的完整轨迹，不足以估计全程状态分布。

四叶均没有改善各自原完整macro的best，更没有超过外层输入guard。许多“current
improvement”只是从已偏离的当前状态回升，并不说明超过了原macro。

## 新薄配置的只读接入检查

`cold_weighted_pinned_repooling.py` 从调用方真实 `alns_module.ALNSConfig()` 或
同模块类型的 caller config 做 `dataclasses.replace`，只替换 T0/Tmin/α/max_iterations。
随后将该 config 与同一个 `alns_module` 原样传给冻结的 full-credit pinned facade；
旧 `polish_pinned_macro` 的 `replace` 没有覆盖这四项，而保留
`enable_budget_cooling=False`、绝对时间上限、完整pin/真实global检查和最终weighted guard。

旧 helper 仍检查真实 `solve_alns/_repair.__globals__` 身份、活动加权系数；既有外层
核对8个构造hook、5个ALNS hook、adapter状态与Score比较器恢复。未见实际模块兼容
blocking。构造代理已报告8项tiny通过，其中真实weighted双臂小迭代验证了最终helper
配置、共同selector和独立pin审计；本文没有另跑优化器。

DIRECT/RELAY使用同一新profile、相同20秒总预算与其余既有边界。后续应按实际叶片
迭代、完整候选、完整post-pin结果报告；在新冷profile结果出现之前，不能声称它能
解决中继绕行代价或保证收益，也不在本次诊断中新增flex-pin或定向destroy。

