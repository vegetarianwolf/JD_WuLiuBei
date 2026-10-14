# 物流无人机调度算法

本目录实现容量为 2 的开放式多无人机取送货调度。每项任务必须先取后送，
最后一次送达后不返航；软截止期允许逾期但所有任务必须完成。所有求解器均按
以下三级目标严格词典序比较：

1. 逾期任务数；
2. 总逾期时间（min）；
3. 总航程（km）。

## 当前方法

新增独立完整求解器 `Joint-Relay-VNS`，CLI 使用 `--method joint-relay`。
它在同一搜索状态中联合调整两任务的分机、直送/中继、站点与访问次序，
不调用 ALNS 搜索或分阶段 DIRECT 预热；其他邻域负责单任务模式修改及访问
交换，历史最优仍经完整校验。官方 10 秒初筛四负，用户独立追加 30 秒两负，
故默认入口仍为 ALNS。代码和实验保持可运行，完整对照、真实跨机交接及
使用命令见[新算法报告](results/joint_relay_trials_20261014/batch1/index.html)和
[复现说明](results/joint_relay_trials_20261014/batch1/README.md)。

最新[中继接力 HTML 对比](results/relay_trials_20261013/batch1/index.html)
独立尝试 `enable_relay_seed_checkpoint`、`enable_relay_route_diversity` 与
`enable_relay_risk_seed`。三个开关默认关闭，分别保留中继播种预算耗尽前的
已核验改善、在固定腿束宽内保留不同无人机候选、按含等待的全局风险优先播种。
跨机候选多样性在两个代表场景的 30 秒三种子验证均为 1 胜 2 负，没有
替换默认。全部 35 次运行通过完整结构及独立 DAG 时序复算，完整口径、逐种子
结果与重放命令见[批次说明](results/relay_trials_20261013/batch1/README.md)。

最新默认启用 `enable_adjacent_polish=True`：多机 DIRECT 搜索每 40 轮对一条候选
路线尝试至多 49 对相邻访问交换，仅采用首个严格路线改善；完整历史最优仍须全局
复算。单机与实际 Relay 阶段不启用，分阶段 Relay 的 DIRECT 预热可使用它。
可通过 `ALNSConfig(enable_adjacent_polish=False)` 关闭。
[最新 HTML 对比](results/method_trials_20261013/iteration3/index.html)中 30 秒集群
三种子全部获胜，但 10 秒初筛曾一胜一负；本次按用户探索门槛设默认，不声称分钟级
或跨实例普遍改善。前三项候选仍默认关闭，分批记录与文献见
[协议说明](results/method_trials_20261013/iteration3/README.md)。

主算法为 `C2-Lex-ALNS`。默认算法注册表为 14 个有效算子：

- 破坏算子（9）：`random`、`worst_distance`、`worst_lex`、
  `spatial_related`、`deadline_related`、`late_critical`、
  `route_segment`、`capacity_conflict`、`assignment_destroy`；
- 修复算子（5）：`greedy`、`regret2`、`regret3`、`deadline`、`slack`。

程序不包含未进入最终方法的 VND、Ejection Swap、Route Pool 或 Cluster
Regret 分支。每次 ALNS 运行会在 `metadata.operator_statistics` 中记录算子的
使用次数、接受次数、当前解改进次数、全局最优改进次数和奖励。研究开关
`enable_regret_portfolio=True` 仅在多机 DIRECT 搜索中增加两个独立加权的
跨机 regret 修复算子；默认算法仍使用上述 14 个算子。

固定中继站由加权 k-medoids 生成；`--relay-count auto` 可按覆盖收益自动
选站数。`--drone-homes stations` 用需求得分、Hamilton 最大余数分配和轻量
局部调整，将无人机预部署到原点/中继站。Relay 交接由 precedence DAG 做
全局时序校验，允许异步先卸后取并计入等待时间。

ALNS 保存构造或传入初始解的完整评分；中继播种只采用完整三级得分严格改善
的候选，启动播种/精化均不得使完整评分退化。搜索中潜在的 best 改善须经完整
评价确认，轨迹记录严格改善的完整得分。`initial_score` 对应播种/精化前的
原始初始解，启动改善可在迭代 0 追加轨迹点。

分阶段 Relay 在末尾以同一完整评价器重新评分 Direct 预热解和 Relay 解，
严格保留较优者；精确等分采用 Relay 阶段解。保留预热解时记录
`relay_retained_warmup_incumbent=true`、`relay_warmup_score` 和
`relay_search_final_score`，并重新生成所选路线的接力统计，保留与所选解
一致的 best 轨迹；两阶段真实耗时、迭代和算子工作量仍完整相加。该保护不
改变比较容差，也不执行随机重跑择优。正式基准、四场景、虚拟消融及多种子
敏感性全部使用含此保护的同一源码版本重算，生成器禁止混用旧源码结果。

小规模 `Pareto-DP` 是单机、公共原点、Direct-only 的精确 oracle，默认最多
10 个任务；可用 `--exact-max-tasks` 调高，但状态空间呈指数增长。CLI 选择
`--method exact` 时会强制上述语义，避免把不支持的 Relay/异地起点结果误称
为精确解。

## 结构

```text
src/uav_dispatch/
├── model.py          问题、任务、中继腿和三级词典序评分
├── validation.py     独立完整可行性与时序校验
├── search.py         路线评估、插入与缓存
├── exact.py          小规模 Pareto-DP 精确 oracle
├── relay.py          固定站点、Relay 方案和全局交接调度
├── deployment.py     需求驱动的无人机初始预部署
├── alns.py           9+5 算子 ALNS 与 Relay 分阶段求解
└── cli.py            命令行入口和 JSON 序列化
experiments/
├── run_benchmarks.py             第5章精确/多规模实验
├── run_virtual_ablation.py       第5.5节虚拟算法对比与组件消融
├── run_scenario_comparison.py    第6.1节四场景×三种子实验
├── run_sensitivity_analysis.py   第6.2节四策略单因素灵敏度
├── audit_sensitivity_results.py  独立完整评价器复核敏感性结果
├── run_optimization_experiments.py  小预算、逐级晋级的优化对照
├── audit_optimization_results.py    从官方数据完整复算优化结果
├── render_optimization_report.py    可离线打开的交互 HTML 报告
├── diagnose_regret_search.py         旧长测轨迹、算子与完整路线诊断
├── run_regret_followup.py            位置回退／自适应组合的有界复测
├── run_regret_portfolio_validation.py  独立追加组合验证，共享累计预算
├── render_regret_followup_report.py  机制证据与分批配对的离线报告
└── final_plot_style.py           论文结果图公共样式
```

## 安装、测试与单次求解

```bash
python -m pip install -r requirements-dev.txt -e .
MPLBACKEND=Agg python -m pytest -q -p no:cacheprovider algorithm/tests
```

不安装包时，从仓库根目录运行：

```bash
PYTHONPATH=algorithm/src python -m uav_dispatch solve \
  --input 'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv' \
  --method alns --tasks 200 --drones 8 --max-tasks 25 \
  --time-limit 300 --iterations 10000000 --seed 2026081701 \
  --relay-count auto --drone-homes stations \
  --output algorithm/results/solution.json
```

`--method` 还支持 `exact`、`edd`、`nearest`、`greedy`、`regret2`。
`--disable-relay` 关闭中继；`--drone-homes origin` 让全部无人机从原点起飞。

## 小预算优化实验（2026-10-13）

最新批次见[原题指标 HTML 对比](results/objective_trials_20261013/index.html)及
[协议、逐种子结果和复现说明](results/objective_trials_20261013/README.md)。
新增 `enable_boundary_destroy` 与 `enable_objective_rewards` 两个独立实验开关，
只影响多机 DIRECT 搜索，均默认关闭。前者保留现有破坏算子并增加截止期边界
邻域，后者强化减少逾期任务数的算子奖励，不改变接受准则与完整词典序评分。
两候选均通过初筛，但 30 秒中测均 1 胜 2 负，未晋级长测。单机固定 200 轮
配对决策全部等价，39 次实际运行全部通过独立审核。

`run_objective_trials`、`audit_objective_trials` 与 `render_objective_trials_report`
分别负责冻结源码及分阶段执行、完整路线复算、离线报告；独立追加的
`run_compact_trials` / `audit_compact_trials` 绑定父批证据，使用相同旧基线源码
检验 2%–5% 常规移除比例，父子批共用 2700 秒上限。用户选择确认阶段即使
遇到退化也跑完既定四个种子；报告保留所有逐种子胜负，不能用均值抵消退化。
追加候选中测 0 胜 3 负，未进入确认。两批共 65 次运行，累计求解 10.45 分钟，
全部独立核验合法且预算内；默认移除比例仍为 3%–8%，本轮未替换默认算法。

此前跨机 regret 复测见[退化诊断 HTML](results/regret_followup_20261013/index.html)及
[完整协议、证据与重建命令](results/regret_followup_20261013/README.md)。新增
`ALNSConfig.enable_regret_position_fallback` 和 `enable_regret_portfolio`：
前者在跨机备选不足时恢复位置后悔值，后者同时保留位置与跨机算子并独立更新
权重。它们不改变初解；单机与实际 Relay 搜索阶段不启用组合池。分阶段 Relay
的 DIRECT 预热可启用组合；最终 metadata 的 `regret_portfolio_active` 仅
描述最后阶段，跨阶段使用次数以合并后的 `operator_statistics` 为准。

位置回退 A 中测两胜一负，未晋级；组合 B 在独立追加中测三胜，但已知反例
240 秒确认仍由基线 62 个逾期增至 65 个。故后续新种子长测提前停止，四个
研究开关均保持默认 `False`，不把短测优势当作默认算法改进。

计数器新增跨机排名／轮次、备选不足、位置回退、真实位置不足及全 inf 轮次，
用于区分“没有足够无人机备选”和“没有足够插入位置”。原题满载场景下，
前一现象可使所有待插任务同时失去 regret 区分能力。报告将该已验证机制与
长测退化的因果推断分开，不把局部修复频率外推到所有搜索。

后续专用入口 `run_regret_followup` 先冻结四个版本，执行 5／10 秒初筛和
5／30 秒中测。只有中测通过才执行 240 秒集群确认；跨机改动不作用于单机，
本轮以固定迭代测试与 5 秒配对验证单机，不重复单机 120 秒长测。
`run_regret_portfolio_validation` 是明确记录的事后追加探索，仅接受主批 A
中测失败、B 已通过初筛的情况；用新配对基线，共享总预算，不合并两批成绩。

以下为首轮优化的历史记录：

本轮以原题条件下的 25 任务单机和 200 任务／8 架集群为主，保留容量 2、
每机上限 25、0.9 km/min、原点起飞及不返航语义。实验采用独立批次，
不重跑下述论文敏感性矩阵，也不将不同源码、不同预算的历史结果混入 A/B。

新增两个 `ALNSConfig` 研究开关，默认值均为 `False`：`enable_lazy_repair`
启用后让 DIRECT 的 deadline/slack 修复仅为实际可选任务计算排名；
`enable_route_distinct_regret` 启用后让多机 DIRECT regret 的备选来自不同无人机。
二者只影响搜索修复，初始构造保持一致；单机不启用跨机 regret。
中继精化另修复了交接等待吸收局部延迟时的
增量误剪枝；该正确性修复保留在默认实现中，继续由全局完整评价器判优。

首轮结果见[原始 HTML 报告](results/optimization_20261013/index.initial.html)及
[完整批次说明](results/optimization_20261013/README.md)。固定 200 次迭代的
6 组配对全部等价；单机、集群中位耗时分别降低 0.23%、5.65%，集群未达到
预设的 10% 提速门槛。跨机分配候选在 30 秒集群中测获得 3 胜、单机 3 平，
但首个 240 秒集群配对（种子 `2026101321`）出现 63→65 个逾期任务，
按第一目标已退化，故提前停止确认并保留两项开关默认关闭。

确认阶段完成两个配对，另保留种子 `2026101322` 的单机 `allocation` 完整
结果；其 `baseline` 在终止时尚未产出完整解，因此没有得分。未配对记录不
参与配对均值或胜负，未启动的运行也不填成绩。三个扩展场景共 12 次运行、
6 个配对均合法且在预算内；其短预算结果仅作为兼容与补充观察。

复算首轮结果，无需求解新实例。首轮 `index.html` 已升级为后续诊断报告入口；
原 HTML 原样保存在 `index.initial.html`，原验收记录中的 HTML 指纹对应此归档。
重建首轮页面会覆盖更新后的入口，需随后重新执行后续报告的
`--update-previous-entry` 命令：

```bash
PYTHONPATH=algorithm/src python -m algorithm.experiments.audit_optimization_results \
  --output-dir algorithm/results/optimization_20261013
python -m algorithm.experiments.render_optimization_report \
  --output-dir algorithm/results/optimization_20261013
```

开展新一轮优化时，先在新的空目录冻结 Git 基线和候选源码，再运行逐级门槛：

```bash
PYTHONPATH=algorithm/src python -m algorithm.experiments.run_optimization_experiments \
  prepare --output-dir algorithm/results/optimization_new --baseline-commit 88f232c
PYTHONPATH=algorithm/src python -m algorithm.experiments.run_optimization_experiments \
  run --output-dir algorithm/results/optimization_new
PYTHONPATH=algorithm/src python -m algorithm.experiments.audit_optimization_results \
  --output-dir algorithm/results/optimization_new
python -m algorithm.experiments.render_optimization_report \
  --output-dir algorithm/results/optimization_new
```

运行器依次进行固定 200 轮等价计时、5／10 秒初筛、30 秒中测，只有唯一
入围候选通过门槛后才进行单机 120 秒、集群 240 秒的三种子确认。
三个扩展场景各做两种子、每次 20 秒的补充检查。所有求解顺序运行，
按种子交替版本顺序；累计实验上限 3600 秒，未通过门槛即停止扩大。
最终运行器检查配对初解和续跑源码指纹，并在确认阶段出现不能满足既定晋级
条件的完整配对时自动早停；本次保留的原始执行脚本与最终运行器有版本区别。
计时从 CSV 读取前至完整校验与解 JSON 写出后，不含解释器启动／模块导入；
预处理从总预算扣除，预留 1 秒收尾，超时记录不参与有效胜负统计。

每批保存源码 ZIP、原始执行脚本、输入指纹、完整配置、逐运行路线、初解指纹、计时、
收敛轨迹、晋级记录与独立审计。报告以原始逐种子三级词典序判胜，
同时展示按时率、总航程、总逾期和时限；总逾期分钟是本项目采用的次级目标。
样本 SD 不是置信区间，少量种子只反映本实例的有限观测。

本批次 `sources/` 保存实际执行的 runner、场景构造 helper 和两个 solver ZIP，
manifest 绑定其哈希。重放原批次须使用独立 checkout，按归档恢复 runner、
helper 与候选 solver 源码，写入新的空结果目录；依据 `execution_notes.json`
保留原提前停止边界。最终运行器不会悄悄续写由其他版本脚本生成的旧批次。
固定时限下的搜索进度会受机器负载影响，相同种子不保证逐位复现原成绩。

旧论文和 `final_*` 结果仍绑定原源码指纹。当前算法变化后，旧论文生成器
拒绝混合当前源码与旧结果属于预期审计保护；重建旧论文须使用基线
`88f232ca66e2ed74ebeb90c4141654413c0714e3` 的独立检出及其原实验产物。
不要修改旧 manifest 或绕过指纹检查来生成“最新”论文。

## 正式实验协议

第 5 章先精确复现两任务单机例（最优访问序列
`P1→P2→D2→D1`、11 km），再对官方数据前 5/8/10/12 个任务做精确
对照，并在截止期统一乘 2.5 的 50/100/150/200 任务多机算例上验证规模
适应性：

```bash
PYTHONPATH=algorithm/src python -m algorithm.experiments.run_benchmarks
```

当前同源码正式重跑在 5/8/10/12 任务的四个精确对照中，ALNS 于 5、10
任务匹配 Pareto-DP，8、12 任务未匹配。8 任务的 ALNS 航程虽短，总逾期却更大，
按三级词典序仍较差。论文动态读取匹配数量、具体规模与差距，不把可行性等同于
达到精确最优。

第 5.5 节使用完全模拟、非业务观测的 30/60/90 任务规模–种子单元，比较
三个已实现的贪心构造与完整 ALNS，并对四个 ALNS 组件组做关闭消融。所有
ALNS 变体复用同一初始路线、求解种子和固定 1000 次迭代预算：

```bash
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.run_virtual_ablation
```

第 6.1 节只比较以下四个场景，使用三个配对种子。Direct 基础模块为 240 s；
Relay 与 Position（需求驱动 Station 预部署）每启用一项分别增加 240 s，
因此四场景名义预算依次为 240/480/480/720 s：

- `pure_direct`；
- `direct_relay`；
- `direct_stations`；
- `direct_relay_stations`。

```bash
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.run_scenario_comparison
```

第 6.2 节覆盖上述四种策略，统一采用独立的 300 s 单次总预算，使用五个配对
种子 `2026081701/2026081702/2026081703/2026081704/2026081705`。每个种子
扫描无人机数 `8/9/10/12/16` 和截止期倍率 `0.8/0.9/1.0/1.1/1.2`；使用
站点的三个策略另扫描站点数 `2/3/4/5/6`。`pure_direct` 没有 Relay 或
预部署，站点数敏感性不适用，实际站数为 0。
共 55 个配置，每配置 n=5，形成 275 条原始行。同一策略、同一种子跨轴复用
基准解，35 条复用行不增加样本量，实际求解 240 次。两个 Relay 策略的
Direct 预热比例均为 0.8；基准为 8 架、截止倍率 1.0，使用站点时为 4 站。

```bash
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.run_sensitivity_analysis \
  --output-dir algorithm/results/sensitivity_rerun --workers 8
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.sensitivity_statistics algorithm/results/sensitivity_rerun
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.audit_sensitivity_results algorithm/results/sensitivity_rerun
```

仓库中的 `final_sensitivity/` 为冻结的正式产物，重跑须指定新的空目录。
可用 `--workers N` 并发求解；断点续跑保持同一输出目录、并发数与其他配置，
增加 `--resume`，从通过协议与哈希检查的检查点继续。manifest schema 3
记录五种子、并发数、按种子轮换的策略交错顺序、求解配置、参数适用性与计划
数量；每行的 `cell_id` 含策略与种子，`run_id`、`reused_from` 和解文件
SHA-256 区分结果单元格、实际求解与基准复用。300 s 为包含构造的名义预算；
预算与 `wall_seconds` 使用 Python `perf_counter` 单调时钟计时，不是 CPU
时间；固定时限下的搜索进度受并发负载影响，固定种子不保证逐位复现。

本次 macOS 主机的 `perf_counter` 使用 `mach_absolute_time()`，该时钟在
系统休眠时暂停（见 [Apple 文档](https://developer.apple.com/documentation/kernel/1462446-mach_absolute_time)）。
2026-10-12 的一次主机暂停影响了 8 组运行，其程序总计时为
299.953–300.209 s，均通过完整 300 s 程序计时预算核验。恢复后保留原次
求解结果，没有重跑择优；暂停增加的日历耗时不计入上述记录。此说明仅适用于
本机时钟实现，不代表所有平台的 `perf_counter` 都排除系统休眠时间。
对应运行标识、解文件指纹和最终核验记录见
[`execution_notes.json`](results/final_sensitivity/execution_notes.json)。

统计模块输出 `summary.json/csv` 的 55 个配置，包含三项目标与五项机制指标
的均值、样本 SD（ddof=1）、最小值、最大值和均值±SD；图中误差线是一个
样本 SD，不是置信区间。`stability.json` 按同一种子的原始三级词典序报告
策略胜平负、首末档配对变化、全轴单调性以及最优档位频次；不对均值三元组
排序。每档策略统计中的胜/平/负分别指唯一最优/并列最优/未达最优，两两比较
则以 A 相对 B 计数。最优档位频次含并列，合计可超过 5；单调不增含相等。
五个种子反映算法随机性的有限观测，不能外推需求分布，也不构成显著性检验。
单因素设计无法识别参数交互。

本次正式结果中，四策略在无人机数轴（8–16）和截止期倍率轴（0.8–1.2）上，
全部 5 个种子的三级得分均随水平增加而单调不劣，首末档均严格改善；16 架
无人机时四策略的逾期数均为 0。站点数从 2 增至 6 时，Direct + Relay、
Direct + Position、组合策略的首末档改善/持平/变差次数分别为 3/0/2、2/0/3、
3/0/2，三个策略的全轴单调不劣次数均为 0/5，最优站点数也随种子变化。
这不支持“站点越多越好”的结论。

15 个展示水平中，Direct + Position 在 8 档获得 5/5 次唯一最优，其余 7 档
的最优策略随种子变化。各轴基准点复用同一批解，因此这些展示水平不能被当作
相互独立的重复试验；上述频次只描述本实例和当前预算下的观测结果。

统计生成后，审计从官方 CSV 重建问题，独立完整评价全部 240 个唯一解；
同时从路线重算五项机制指标，检查阶段末严格择优与预热保留标志；
另外独立重算均值、样本 SD、范围、配对胜平负与稳定性，并核对 CSV。
`validation.json` schema 2 绑定原始记录、统计文件、完整解与审计脚本的
哈希。论文生成器验证 275 行覆盖、240 次求解、五种子配对、机制开关、单因素
控制、同种子基准复用、重新计算的统计、独立审计凭据及图像来源。复现顺序
必须是运行实验、生成统计、独立审计，再生成图表和论文。

四个入口均支持 `--quick` 冒烟模式。正式输出会保存 manifest、输入/源码
指纹、CSV/JSON 汇总及逐运行完整路线；`--quick` 结果明确标记为非正式，
不得写入论文结论。`_archive/` 与旧 `results/` 中的历史材料仅供审计，不能与
上述最终协议的结果混用。
