# 2026-10-13 原题优先的小预算优化实验

[打开本批次原始 HTML 报告](index.initial.html)；[当前诊断与优化复测报告](index.html)
已更新到同一入口。原始 HTML 原样归档，原 `acceptance.json` 中的 HTML
指纹对应 `index.initial.html`；本目录原始数据、源码快照与验收记录没有重写。
页面内嵌数据和绘图代码，可直接用浏览器
打开，查看按时完成数／按时率、总航程、总逾期分钟、端到端耗时、配对差值
和三级目标的收敛曲线。完整路线与审计文件从报告中的相对链接打开。

## 本轮结论

两项优化候选均未达到默认采用条件，`enable_lazy_repair` 和
`enable_route_distinct_regret` 保持 `False`。Relay 存在交接等待时的局部
误剪枝属于独立的正确性问题，其修复保留在默认实现中。

| 检查 | 实际结果 | 决定 |
| --- | --- | --- |
| 固定 200 次迭代的等价计时 | 两个实例各 3 个种子，共 6 组配对，路线、得分、搜索决策全部等价 | 等价性通过 |
| 等价提速 | 单机中位耗时降低 0.23%，集群降低 5.65% | 集群低于预设 10% 门槛，不单独晋级 |
| 5／10 秒初筛 | 分配候选集群 2 胜，单机 2 平 | 分配候选进入中测 |
| 30 秒中测 | 分配候选集群 3 胜，单机 3 平 | 进入唯一候选长测 |
| 120／240 秒确认 | 首个单机配对持平；首个集群配对逾期数由 63 增至 65 | 已违反所有确认配对均不退化的门槛，提前停止 |
| 扩展兼容 | 三个场景各 2 个种子，共 12 次运行；全部合法且在 20 秒预算内，候选获得 6 次配对胜利 | 仅作兼容检查与补充观察，不改变原题淘汰结论 |

长测集群种子 `2026101321` 的完整指标如下。即使低优先级指标改善，第一
目标逾期任务数增加仍构成词典序退化。

| 指标 | baseline | allocation |
| --- | ---: | ---: |
| 逾期任务数 | 63 | 65 |
| 按时完成数 | 137 / 200 | 135 / 200 |
| 按时率 | 68.5% | 67.5% |
| 总逾期分钟 | 2179.499869 | 1996.967268 |
| 总航程 km | 635.429526 | 599.044019 |
| 端到端耗时 s | 239.009790 | 239.010892 |

确认阶段原计划 6 个配对，本轮只完成 2 个配对。种子 `2026101322` 的单机
`allocation` 已完成，结果为 14 个逾期任务、587.614709 分钟总逾期、
101.606013 km 航程；其配对的 `baseline` 在进程运行约 80 秒时中止，
没有完整解与最终分数。候选这条孤立记录保留在原始数据中，不计入配对均值、
配对胜负或已完成配对数量。详见[执行说明](execution_notes.json)和
[阶段决策](decisions.json)。没有更换种子、补造成绩或重跑择优。

本批次保存 41 条完整的固定时限运行：初筛 12 条、中测 12 条、确认 5 条、
扩展 12 条；另保存 12 条固定迭代计时，形成 6 组等价性配对。完整运行的
程序计时共约 1507.55 秒（25.13 分钟），不含上述中止进程约 80 秒，亦不含
开发、测试、审计与报告制作。完整运行记录均标记为合法且预算内，独立复算
覆盖范围以[validation.json](validation.json)为准。

## 题目口径与比较方法

- 官方 CSV 前 25 项任务用于单机实例，完整 200 项任务用于 8 架无人机实例。
- 容量 2、每机最多 25 项任务、速度 15 m/s（0.9 km/min）、原点起飞；
  全部任务必须完成，最后送达后不返航。
- 按“逾期任务数 → 总逾期分钟 → 总航程”严格词典序比较。总逾期分钟是
  项目采用的次级目标，报告同时直接展示题目相关的按时完成与航程指标。
- 中继、需求驱动预部署及其组合只用于补充检查：固定 4 站、选址种子 42，
  启用 Relay 时 Direct 预热比例为 0.8。
- A/B 使用相同初解与求解种子，按种子交替运行顺序。计时从读取 CSV 前开始，
  至独立完整核验和完整解 JSON 写出后结束；不含 Python 进程启动及模块导入。
  预处理计入预算，搜索时限扣除已用时间并预留 1 秒收尾。
- 均值和样本标准差仅描述共同有效配对种子；不按均值三元组判胜。单个配对
  不计算样本标准差，少量种子不构成统计显著性证明，扩展结果不外推为原题稳定改进。

## 数据与版本证据

| 文件 | 内容 |
| --- | --- |
| [manifest.json](manifest.json) | 官方输入、固定协议、基线提交、执行脚本和 solver 快照指纹 |
| [runs.json](runs.json)、[runs.csv](runs.csv) | 41 条原始记录，含配置、计时分项、完整轨迹、初解与解文件指纹 |
| [equivalence.json](equivalence.json) | 6 组等价性配对、12 条完整运行、搜索决策指纹和中位耗时降低率 |
| [decisions.json](decisions.json)、[execution_notes.json](execution_notes.json) | 晋级、淘汰、提前停止和无成绩中止记录 |
| [run_solutions/](run_solutions/) | 每次已完成运行的完整路线、送达时刻、评分和求解元数据 |
| [validation.json](validation.json) | 独立完整复算、协议与指纹核验覆盖范围 |
| [acceptance.json](acceptance.json) | 513 项测试、最终离线页面交互与桌面／窄屏验收、交付代码指纹 |
| [summary.json](summary.json) | 描述性分组汇总；原始样本数量可能不同，报告配对比较使用共同种子 |
| [sources/baseline.zip](sources/baseline.zip)、[sources/candidate.zip](sources/candidate.zip) | 实际执行的 solver 源码，ZIP 内为 `uav_dispatch/*.py` |
| [sources/run_optimization_experiments.py](sources/run_optimization_experiments.py) | 本批次原始执行 runner |
| [sources/run_scenario_comparison.py](sources/run_scenario_comparison.py) | 本批次原始场景构造 helper |

基线提交为 `88f232ca66e2ed74ebeb90c4141654413c0714e3`。`fast` 和
`allocation` 共用候选 solver ZIP，分别启用不同研究开关；每条记录均绑定
所用 ZIP 的 SHA-256。源码快照、原始 runner 和 helper 共同记录实际执行
版本，不能用当前工作区的文件替代这些证据。

最终工作区运行器增加了配对初解检查、续跑源码指纹保护和确认阶段自动早停。
它与本批次归档 runner 存在版本差异；本批次提前停止过程记录在
`execution_notes.json`。最终运行器的改进不追溯修改已完成运行。

## 复算、报告重建与重放

在仓库根目录运行以下命令，复算已保存的结果并重建 HTML；不会求解新实例：
此处的重建命令会把当前入口还原成旧批次视图；需要保留更新后的入口时，
最后执行新报告的 `render_regret_followup_report --update-previous-entry`。

```bash
PYTHONPATH=algorithm/src python -m algorithm.experiments.audit_optimization_results \
  --output-dir algorithm/results/optimization_20261013
python -m algorithm.experiments.render_optimization_report \
  --output-dir algorithm/results/optimization_20261013
```

审计从官方 CSV 重建各场景，用完整评价器复算全部唯一解，核验完成性、
约束、三级得分、按时率、轨迹、时间预算、配对初解及源码／输入／解文件指纹。
如实记录的超时或非法解不会被伪装成成功；没有有效结果时也不宣称实验成功。

若要重放本批次，应在基线提交的独立 checkout 中恢复归档 runner、helper
到原实验脚本路径，并将候选 solver ZIP 恢复到 `algorithm/src/`；保留
Git 基线不变，在新的空输出目录准备快照。按原固定种子、配置、预算和
`execution_notes.json` 中的提前停止边界运行。归档 runner 不含后来加入
的自动早停，不能直接续跑本目录来补齐原计划。固定墙钟预算受硬件与机器
负载影响，即使种子相同也不承诺逐位复现分数。

新探索可使用当前运行器和新的空目录，命令见[算法运行说明](../../README.md)。
重建旧论文及 `final_*` 产物须另用其原源码版本和匹配的冻结实验数据；不要
覆盖既有结果、修改旧 manifest 指纹或绕过论文的源码一致性检查。
