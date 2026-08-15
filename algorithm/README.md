# 物流无人机调度算法

本目录实现了研究报告建议的容量二、词典序混合自适应大邻域搜索（C2-Lex-HALNS），并在此基础上重构为 **固定中继站网络 + Relay-aware Pickup-Delivery + ALNS**。算法处理开放式取送货路线：每项任务必须先取后送，每架无人机最多同时携带 2 件快递，最后一次送达后不计算返航里程。最终目标严格按“逾期任务数、总逾期分钟、总里程”三层词典序比较，不使用可能颠倒优先级的固定加权和。

最新的 Relay/Direct 正确性修复、性能数据和三种子等墙钟结果见 [OPTIMIZATION_UPDATE_2026-08-15.md](OPTIMIZATION_UPDATE_2026-08-15.md)。

**240 秒调整配置配对结果（Direct 1 : Relay 2，Relay 首次胜出）见 [RELAY_ADJUSTED_240S_REPORT.md](RELAY_ADJUSTED_240S_REPORT.md)**；默认 Relay 配置已更新为 7 站 / 每任务 2 候选 / 绕行比 2.0 / 兜底开启，并新增构造后中继播种与跨机精化。

**中继点预部署（多机巢）已支持**：`--drone-homes stations` 按**任务需求动态分配**（`algorithm/src/uav_dispatch/deployment.py`）：以「depot + 全部中继站」为候选起点，综合 pickup 距离（平滑空间衰减）、deadline 紧迫度与服务价值计算每个候选点的需求得分，经 Hamilton 最大余数法 + 软性单点上限 + 轻量 local search 决定每点 0..n 架 UAV（不再是一站一机）；`--drone-homes origin` 全部驻原点。**中继站数量同样需求驱动**：`--relay-count auto`（默认）用覆盖率拐点搜索（`relay.compute_dynamic_station_count`，任务中点平均最近站距的边际改善跌破阈值即停）自动确定建站数，也可用整数覆盖。30 秒 A/B 中预部署使 Direct 逾期任务数 -5、Relay -6，总逾期与里程全面改善（见下）。

## Relay-aware 模型

- 场景初始化时按任务中点做加权 k-medoids 生成固定中继站（默认 `--relay-count auto` 按任务分布动态确定站数、每任务 2 候选、绕行比 2.0，走廊外任务兜底到最近站），一次求解内坐标不变。
- **多机巢预部署（动态）**：每架无人机有固定起点 `home`（原点或某中继站）。`--drone-homes stations` 调用 `compute_dynamic_uav_homes(...)` 根据当前任务分布自动决定每个候选起点的 UAV 数量（允许 0 架、2 架甚至更多，总数恒等于无人机数），构造初始解默认开启 home 感知（就近播种 + home→pickup 平局偏向，`--disable-home-aware-init` 可关）；`--drone-homes origin` 全部驻原点。库层 `Problem.drone_homes`（`None`=原点 / 站 id）精确建模：首段距离从各自 home 起算，`RouteEvaluator`、`GlobalRelayEvaluator`、独立校验器与全部插入/移除 delta 均按路线索引取 home（`RouteEvaluator.evaluate(..., start_node=)` / `routes_score` 自动按索引）。**部署与中继语义解耦**：某站部署 0 架无人机不影响它作为中继交接点使用。预部署在解空间上严格不劣于纯原点（原点解必为合法预部署解）。
- 每项原始任务有两种服务方案：`DIRECT`（P→D，保留原始 `+task_id/-task_id` 访问编码）或 `RELAY(r)`（P→中继、中继→D 两条 TransportLeg，允许不同无人机执行）。
- 每条 Relay 腿从 immutable `LegRegistry` 分配唯一编号；`+leg_id` 为腿起点事件（机载 load +1）、`-leg_id` 为腿终点事件（load -1），Relay 卸货真实减载、Relay 取货真实加载，机载容量始终 ∈ [0,2]。
- 每个任务最多经过一个中继（`MAX_RELAY_HOPS = 1`）；Relay 库存暂不设上限。
- `GlobalRelayEvaluator` 以紧凑数组式 precedence DAG（路线续接边 + 交接边 drop→pickup）做拓扑序最长路径调度：取件早于卸货自动等待，等待计入后续到达时间与逾期；DAG 有环即 deadlock，候选解判不可行。事件语义预解码，结果通过只读 Mapping 对外兼容。
- K=25 语义保持“原始任务责任”：以最终完成 Delivery 的无人机计责任，每条腿只占一个 K 槽位，Relay 不会把 200 个任务变成超过 200 个。
- ALNS destroy/repair 直接搜索 DIRECT 与 RELAY 方案：destroy 以 original task 为单位（两腿同删），repair 的 greedy/regret-2/regret-3/deadline/slack 在 DIRECT 与各候选 Relay 计划之间比较（regret 可跨模式），任务每次重建后可自由改变模式与中继。
- 两级剪枝：单腿候选位置上限 4、leg beam 8、plan beam 6，候选先用算术 surrogate 排序，再对 DIRECT top-2 与覆盖不同中继站的 Relay 短名单做 global 精确评估（含 waiting）。存在全局等待时不会使用不安全的 route-local 下界；另带路线版本化缓存与 `sample_every=3` 门控，probe 比例 0.40 / 每轮最多 6 个任务生成中继 beam，detour > 2.0 的任务兜底到最近站（`build_relay_network(fallback=True)`）。
- 生产 Relay 默认在同一总预算内先用 80% 做 DIRECT 预热，再用 20% 从强 incumbent 搜索 Relay；第二阶段保留 best-so-far，因此不会为了使用中继而丢弃更好的直接配送解。可用 `--relay-direct-warmup 0` 关闭预热。构造后默认执行一次**中继播种 pass**（把严格更优的 DIRECT 任务转为中继）与周期**跨机精化**（移动单条中继腿），可用 `--disable-relay-seed` / `--disable-relay-refine` 关闭。
- `--disable-relay` 通过空注册表退化为完整 DIRECT-only 行为，同 seed 下与旧实现位级一致，用于公平 A/B。

## 题意假设

附件没有提供起点坐标，默认实验采用 `(0, 0) km`，命令行可通过 `--depot-x` 和 `--depot-y` 覆盖。Excel 的 `Tmax` 表头明确使用分钟，因此内部速度采用 `0.9 km/min`，等价于题目的 `15 m/s`。仅送达点受软截止期约束；逾期任务仍必须完成。所有无人机从 `t=0` 独立出发（默认部分预部署在中继站），取送作业耗时为 0。

用户转换的 CSV 为 GB18030 编码，加载器同时支持 GB18030 和 UTF-8。原始 `.xls` 和转换后的 `.xlsx` 保留作审计输入，生产求解命令默认读取同目录下的 `.csv`，不需要额外 Excel 依赖。

## 实现结构

```text
src/uav_dispatch/
├── model.py          任务、问题、RelayStation/TransportLeg/TaskPlan 与词典序评分模型
├── relay.py          中继站选址、LegRegistry、交接调度与方案索引、统计
├── io.py             UTF-8/GB18030 CSV 加载
├── validation.py     不复用搜索缓存的独立完整校验器（含交接时序）
├── search.py         路线缓存、容量二 O(1) 固定位置检查、单腿/成对插入与插入面
├── exact.py          小规模 Pareto 标签动态规划 oracle（DIRECT-only）
├── alns.py           ALNS、Relay-aware repair、任务分配邻域、风险引导
└── cli.py            求解与 JSON 结果输出（含 relay 统计）
experiments/
├── run_benchmarks.py              多规模、多随机种子可复现实验
├── run_alns_core_comparison.py    ALNS Core/HALNS 配对对比
├── run_neighborhood_ablation.py   问题特定邻域逐项消融
├── run_relay_comparison.py        Direct-only 与 Relay-aware 配对墙钟对比
├── run_scenario_comparison.py     五场景（纯 Direct/纯 Relay/Direct+Relay/起点变体）同种子配对对比
└── profile_run.py                 单次 Direct/Relay 剖析与吞吐测量（--no-profile 测真实 it/s）
results/              原始运行、汇总统计与两类最终路线
tests/                行为测试、随机交叉验证、CLI 与 relay 测试
```

固定位置的容量查询通过路线载荷区间最大值表实现为 `O(1)`；候选插入的软截止期变化仍会完整扫描最长 50 个访问节点，以免把硬时间窗文献中的快速检查错误套用到本题软截止期。

## 安装与测试

项目无第三方运行依赖，要求 Python 3.11 或更高版本。

```bash
python3 -m pip install -r requirements-dev.txt -e .
python3 -m pytest algorithm/tests
```

不安装包时，也可以从仓库根目录直接运行：

```bash
PYTHONPATH=algorithm/src python3 -m uav_dispatch --help
```

## 求解命令

求解完整 200 任务实例并把合规结果写为 JSON：

```bash
PYTHONPATH=algorithm/src python3 -m uav_dispatch solve \
  --input 'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv' \
  --method halns --tasks 200 --drones 8 --max-tasks 25 \
  --iterations 400 --time-limit 240 --seed 2026080504 \
  --relay-count 7 --relay-candidates-per-task 2 --relay-detour-ratio 2.0 \
  --relay-direct-warmup 0.8 --relay-plan-beam 6 --relay-leg-beam 8 \
  --relay-sample-every 3 --relay-location-seed 42 \
  --drone-homes stations \
  --output algorithm/results/solution.json
```

Relay 默认启用。`--disable-relay` 完全关闭中继（等价 Direct-only baseline）；`--relay-count 0` 等价。`--drone-homes {origin,stations}` 控制无人机初始部署（默认 `stations`：原点 1 架 + 每中继站 1 架；`origin`：全部在原点）。相关参数包括 `--relay-count`、`--relay-candidates-per-task`、`--relay-direct-warmup`、`--relay-detour-ratio`、`--relay-plan-beam`、`--relay-leg-beam`、`--relay-event-cap`、`--relay-sample-every`、`--relay-global-limit`、`--relay-location-seed`、`--relay-probe-fraction`、`--relay-probe-min`、`--relay-probe-max-tasks`、`--relay-seed-task-limit`、`--disable-relay-seed`、`--disable-relay-refine`、`--relay-refine-interval`、`--relay-refine-task-limit`、`--relay-auto-widen`、`--drone-homes`、`--relay-debug`。输出 JSON 额外包含 `relay` 统计块、`problem.drone_homes`、`task_plans`、全局一致的 per-route 逾期统计与每个 visit 的坐标/到达时刻/leg/relay 元数据。

`--method` 还支持 `exact`、`edd`、`nearest`、`greedy`、`regret2` 和 `alns-core`；`basic-alns` 作为 `alns-core` 的兼容别名保留。当前 ALNS Core 默认用 assignment destroy 替换旧 route-clear，并启用 deadline-risk guidance；不启用驱逐交换、VND、cluster repair 或路线池。HALNS 在此基础上启用 ejection，route pool 默认关闭。VND、cluster repair 和全部细分预算可通过 Python API 的 `ALNSConfig` 显式开启。将 `--candidate-limit` 设为 `0` 可关闭候选位置剪枝；固定迭代数适合复现比较，`--time-limit` 适合墙钟预算控制。小规模精确求解默认最多 10 个任务，可通过 `--exact-max-tasks` 调整，但状态空间指数增长。

## Direct-only 与 Relay-aware 配对对比

同一数据、同 seed、同墙钟预算（默认 240 秒，预留 2 秒安全余量，初始构造时间计入预算）、同目标函数与 ALNS Core，逐 seed 按词典序判优。Relay 的 DIRECT 预热比例会完整写入 manifest：

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_relay_comparison.py \
  --output-dir algorithm/results/relay_comparison_240s \
  --wall-seed-count 3 --wall-time-limit 240 --wall-safety-margin 2
```

本轮（调整配置，Direct 1 : Relay 2）240 秒三种子配对结果见 [RELAY_ADJUSTED_240S_REPORT.md](RELAY_ADJUSTED_240S_REPORT.md)，原始记录位于 `results/relay_adjusted_240s/`；旧配置（Direct 3:0）见 [RELAY_VS_DIRECT_240S_REPORT.md](RELAY_VS_DIRECT_240S_REPORT.md)，原始记录位于 `results/relay_comparison_240s/`；30 秒优化更新见 [OPTIMIZATION_UPDATE_2026-08-15.md](OPTIMIZATION_UPDATE_2026-08-15.md)，其原始记录位于 `results/optimization_update_30s_staged/`。快速自检可增加 `--quick`。

**预部署 A/B**（30 秒、3 种子、同为 7 站，仅起点不同；`results/predeploy_ab_origin/` 与 `results/predeploy_ab_stations/`）：

| 起点模式 | Direct（逾期数, 逾期分钟, 里程） | Relay |
|---|---:|---:|
| origin（纯原点） | (77.33, 2688.2, 679.9) | (80.33, 2876.7, 699.5) |
| **stations（预部署）** | **(72.33, 2551.7, 668.0)** | **(74.00, 2790.6, 691.1)** |

预部署省去「原点→站」空飞，Direct 逾期任务数 -5、Relay -6，总逾期与总里程全面改善；组内 Relay 仍受 80/20 预算结构影响而落后 Direct（与预部署无关）。

## 五场景同种子 240 秒配对对比

在同一 seed、同一 240 秒墙钟预算下，对五个场景做配对对比：**纯 Direct**（原点起点）、**纯 Relay**（原点起点、warmup=0）、**Direct + Relay**（原点起点、80/20 分阶段）、**Direct + 起点不同**（预部署中继站）、**Direct + Relay + 起点不同**（预部署 + 80/20）。Relay 网络、目标函数与 ALNS Core 完全一致，仅按场景切换 relay/warmup/起点：

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_scenario_comparison.py \
  --output-dir algorithm/results/scenario_comparison_240s \
  --report algorithm/SCENARIO_COMPARISON_240S_REPORT.md \
  --wall-seed-count 1 --seed-base 2026080500 \
  --wall-time-limit 240 --wall-safety-margin 2
```

已跑结果可用 `--render-only` 直接重渲染报告，无需重跑。240 秒报告见 [SCENARIO_COMPARISON_240S_REPORT.md](SCENARIO_COMPARISON_240S_REPORT.md)，原始记录位于 `results/scenario_comparison_240s/`（含每场景完整路线）。核心结论：词典序排序为 `direct_stations > direct_relay_stations > direct_relay > pure_direct > pure_relay`；预部署是收益最大的单一杠杆，纯 Relay（warmup=0）在 240s 内不可行，分阶段 80/20 是唯一能发挥中继价值的结构。

## 复现实验

正式实验使用 5 个固定随机种子、单机 1500 轮与 120 秒上限、集群 400 轮与 240 秒上限，并另做一次不计入四分钟合规成绩的 1500 轮离线搜索：

```bash
PYTHONPATH=algorithm/src python3 algorithm/experiments/run_benchmarks.py \
  --output-dir algorithm/results \
  --seed-count 5 --single-iterations 1500 --single-time-limit 120 \
  --fleet-iterations 400 --fleet-time-limit 240 \
  --extended-iterations 1500
```

快速冒烟实验可增加 `--quick`。完整数值结论、限制与复现环境见 [RESULTS_REPORT.md](RESULTS_REPORT.md)。`results/best_fleet_solution_compliant.json` 是第一次基准实验中四分钟内的最好路线；`results/best_fleet_solution.json` 是同批次的超时离线扩展路线，二者不可混为竞赛成绩。

## ALNS Core 对比实验

为分别回答“每轮搜索质量”和“固定墙钟内的实用性能”，专项实验同时运行 5 个种子的 400 轮对比和 3 个种子的 240 秒配对对比：

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_alns_core_comparison.py \
  --output-dir algorithm/results/alns_core_comparison \
  --equal-seed-count 5 --equal-iterations 400 \
  --wall-seed-count 3 --wall-time-limit 240 \
  --wall-safety-margin 2
```

断点续跑可增加 `--resume`；脚本会核对输入与参数签名，并为新运行持久化完整路线。结论、逐种子得分和统计限制见 [ALNS_CORE_COMPARISON_REPORT.md](ALNS_CORE_COMPARISON_REPORT.md)，原始记录位于 `results/alns_core_comparison/`。

## 问题特定邻域消融

新实验按 `A0 → A1（assignment destroy 替换 route-clear）→ A2（deadline risk）→ A3（VND）→ A4（cluster repair）` 展开，并可继续隔离 A5 的 ejection 与 A6 的 route pool。A1 是保持 destroy pool 大小不变的算子替换，后续步骤才逐项累加。默认计划同时支持旧实验的 5 个种子 × 400 轮和 3 个种子 × 240 秒；快速自检可增加 `--quick`。

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_neighborhood_ablation.py \
  --output-dir algorithm/results/neighborhood_ablation \
  --include-hybrid-tuning
```

三个种子的正式 240 秒结果、默认开关依据，以及按“逾期任务数、总逾期分钟、总里程”与前两次实验的比较见 [NEIGHBORHOOD_ABLATION_REPORT.md](NEIGHBORHOOD_ABLATION_REPORT.md)，原始逐运行结果及完整路线位于 `results/neighborhood_ablation/`。
