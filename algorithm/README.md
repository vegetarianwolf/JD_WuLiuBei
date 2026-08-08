# 物流无人机调度算法

本目录实现了研究报告建议的容量二、词典序混合自适应大邻域搜索（C2-Lex-HALNS）。算法处理开放式取送货路线：每项任务必须先取后送、同机完成，每架无人机最多同时携带 2 件快递，最后一次送达后不计算返航里程。最终目标严格按“逾期任务数、总逾期分钟、总里程”三层词典序比较，不使用可能颠倒优先级的固定加权和。

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
├── interaction_graph.py  容量二任务交互图与冲突排序
├── pair_repair.py        六种双任务取送顺序与 pair-regret 修复
├── propagation_eval.py   插入后的前向截止期延迟传播评估
├── hypergraph_destroy.py 高冲突任务对联合破坏
└── cli.py            求解与 JSON 结果输出
experiments/
├── run_benchmarks.py              多规模、多随机种子可复现实验
├── run_alns_core_comparison.py    ALNS Core/HALNS 配对对比
├── run_neighborhood_ablation.py   问题特定邻域逐项消融
└── run_capacity_halns_benchmark.py A2/容量感知 HALNS 配对实验
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

## 容量感知 HALNS 实验

容量交互图、pair-regret 修复、前向截止期传播与 hypergraph destroy 均为可选邻域；默认配置不启用，因此旧 ALNS/修复算子和既有实验可原样复现。专项实验以 A2（assignment destroy + deadline risk）为控制组，仅为处理组开启新的 pair repair 与 hypergraph destroy，并在每次运行前清空交互边缓存，使图构建时间计入同一墙钟预算：

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_capacity_halns_benchmark.py \
  --output-dir algorithm/results/capacity_halns \
  --seed-count 3 --time-limit 240 --safety-margin 2
```

三组配对种子中容量感知版本胜 1 组、A2 胜 2 组；容量感知版本平均逾期任务数为 `71.000`，A2 为 `69.333`。这说明新邻域在个别种子上能找到明显更好的容量二协同解，但当前固定墙钟配置尚未形成稳定总体优势。逐种子得分、运行时间、迭代数、完整路线和严格词典序比较见 [results/capacity_halns/BENCHMARK_REPORT.md](results/capacity_halns/BENCHMARK_REPORT.md)。
