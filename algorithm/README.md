# 物流无人机调度算法

本目录实现了容量二、开放式物流无人机调度求解器，并新增允许静态缓冲交接的 Relay 事件层。题目主比较口径是先最大化按时任务数（等价于最小化逾期任务数），同分时再最小化总配送里程。旧求解器保留 `total_lateness_min` 作为内部诊断或历史 tie-break；它不是题目明示的评分项，正式报告不会让它压过总里程。

## 题意假设

附件没有提供起点坐标，默认实验采用 `(0, 0) km`，命令行可通过 `--depot-x` 和 `--depot-y` 覆盖。Excel 的 `Tmax` 表头明确使用分钟，因此内部速度采用 `0.9 km/min`，等价于题目的 `15 m/s`。仅送达点受软截止期约束；逾期任务仍必须完成。所有无人机从 `t=0` 独立出发，取送作业耗时为 0。

用户转换的 CSV 为 GB18030 编码，加载器同时支持 GB18030 和 UTF-8。原始 `.xls` 和转换后的 `.xlsx` 保留作审计输入，生产求解命令默认读取同目录下的 `.csv`，不需要额外 Excel 依赖。

## 实现结构

```text
src/uav_dispatch/
├── model.py          任务、问题与词典序评分模型
├── io.py             UTF-8/GB18030 CSV 加载
├── validation.py     不复用搜索缓存的独立完整校验器
├── search.py         路线缓存、容量二 O(1) 固定位置检查与成对插入
├── exact.py          小规模 Pareto 标签动态规划 oracle
├── alns.py           ALNS、任务分配邻域、风险引导、VND 与可选强化
├── relay.py          独立的接力事件、静态 hub 与任务计数语义
├── relay_validation.py 跨无人机事件 DAG、载荷、custody 与等待验证
├── relay_search.py   split/merge/change-hub/change-receiver 候选生成
├── relay_alns.py     直接 ALNS + 静态缓冲接力搜索
└── cli.py            求解与 JSON 结果输出
experiments/
├── run_benchmarks.py              多规模、多随机种子可复现实验
├── run_alns_core_comparison.py    ALNS Core/HALNS 配对对比
├── run_neighborhood_ablation.py   问题特定邻域逐项消融
├── generate_relay_scenarios.py    U/G/MZ/MIX synthetic campus 生成器
├── run_relay_handoff.py           strict/primary-owner 接力实验
├── run_relay_sensitivity.py       hub 数×交接耗时配对敏感性
├── build_relay_comparison.py      历史与接力结果两层指标汇总
└── migrate_relay_handoff_artifacts.py 旧产物元数据审计迁移
results/              原始运行、汇总统计与两类最终路线
tests/                行为测试、随机交叉验证与 CLI 测试
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
  --output algorithm/results/solution.json
```

`--method` 还支持 `exact`、`edd`、`nearest`、`greedy`、`regret2` 和 `alns-core`；`basic-alns` 作为 `alns-core` 的兼容别名保留。当前 ALNS Core 默认用 assignment destroy 替换旧 route-clear，并启用 deadline-risk guidance；不启用驱逐交换、VND、cluster repair 或路线池。HALNS 在此基础上启用 ejection，route pool 默认关闭。VND、cluster repair 和全部细分预算可通过 Python API 的 `ALNSConfig` 显式开启。将 `--candidate-limit` 设为 `0` 可关闭候选位置剪枝；固定迭代数适合复现比较，`--time-limit` 适合墙钟预算控制。小规模精确求解默认最多 10 个任务，可通过 `--exact-max-tasks` 调整，但状态空间指数增长。

### 静态缓冲接力

`relay-alns` 使用单独的事件路线，支持 `PICKUP → HANDOFF_DROP → HANDOFF_PICK → DELIVERY`。接力层按历史表现自适应选择 split/merge/change-receiver/change-hub，并保留 best-so-far；下例启用 4 个从任务流确定性生成的静态 hub，每单最多一次接力：

```bash
PYTHONPATH=algorithm/src python3 -m uav_dispatch solve \
  --input 'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv' \
  --method relay-alns --relay-mode static-buffered \
  --relay-hub-count 4 --handoff-service-min 0.5 \
  --relay-task-count-semantics primary-owner \
  --tasks 200 --drones 8 --max-tasks 25 \
  --iterations 10000 --time-limit 240 --seed 2026080500 \
  --output algorithm/results/relay_solution.json
```

`strict-touch` 是题意保守解释：任务被两架无人机运输，就分别占用两架机的任务额度。官方实例满足 `200 = 8 × 25`，因此 strict-touch 下数学上不能出现跨机接力。`primary-owner` 只把订单计入取件无人机的 25 单额度，能研究接力，但属于明确的题意扩展，不能冒充官方合规结果。完整证明与验收边界见 [relay_task_count_semantics.md](reports/relay_task_count_semantics.md)。

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

## Adaptive Deadline / Rejection Pool 实验

在稳定 A2 上分别测试 late-risk destroy、temporary rejection pool、soft-deadline
search package 及三者组合：

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_adaptive_deadline_rejection.py \
  --methods baseline experiment1 experiment2 experiment3 experiment4 \
  --equal-seed-count 0 --wall-seed-count 3 \
  --wall-time-limit 240 --wall-safety-margin 2 \
  --output-dir results/adaptive_deadline_rejection
```

断点续跑增加 `--resume`；恢复时会校验输入、源码、完整配置、路线文件哈希、
历史内部得分、预算与拒绝池终态。报告同时审计 main 之外所有历史分支；按题面直接
排名时只使用按时任务数与总里程。历史内部三层记录中，仅启用 `late_risk_destroy`
的三-seed 均值为 `(68.667 个逾期, 2310.280 分钟诊断逾期量, 645.854 km)`；
其余新增模块应保持关闭。完整结论见
[adaptive_deadline_rejection_report.md](reports/adaptive_deadline_rejection_report.md)。

## Relay 正式实验

官方 200 单实验使用 3 个固定 seed、每次 240 秒总预算。接力预算包含前置直接 ALNS 搜索；结果保存完整事件路线、输入/问题哈希和独立复算诊断。

```bash
PYTHONPATH=algorithm/src python3 algorithm/experiments/run_relay_handoff.py \
  --dataset official \
  --methods baseline static-1hub static-multihub \
  --seeds 2026080500 2026080501 2026080502 \
  --time-limit 240 --wall-safety-margin 2 \
  --task-count-semantics primary-owner --hub-count 4 \
  --handoff-service-min 0.5 \
  --output-dir algorithm/results/relay_handoff/official_primary
```

正式结论、全部历史实验拉表和限制见 [relay_handoff_report.md](reports/relay_handoff_report.md)，逐运行结果位于 `results/relay_handoff/`。

Synthetic campus 敏感性使用 U（均匀负对照）、G（gate cut）、MZ（多分区）和 MIX（混合流向）四类坐标度量场景。下列 680-run 开发矩阵只研究 hub 数与交接耗时，不与官方 240 秒结果混排：

```bash
PYTHONPATH=algorithm/src python3 algorithm/experiments/run_relay_sensitivity.py \
  --scenarios U G MZ MIX --tasks 50 \
  --seeds 2026081000 2026081001 2026081002 2026081003 2026081004 \
          2026081005 2026081006 2026081007 2026081008 2026081009 \
  --hub-counts 1 2 4 8 --handoff-times 0 0.5 1 2 \
  --time-limit 1.0 --wall-safety-margin 0.2 \
  --candidate-limit 4 --task-sample-size 8 --base-search-fraction 0.60 \
  --require-relay-iterations \
  --output-dir algorithm/results/relay_handoff/synthetic_development_sensitivity
```
