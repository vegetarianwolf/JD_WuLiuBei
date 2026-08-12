# 物流无人机调度算法

本目录实现了容量二、词典序混合自适应大邻域搜索（C2-Lex-HALNS）及其协作扩展（Pickup-First Cooperative HALNS）。算法处理开放式取送货路线：每项任务必须先取后送、同机完成，每架无人机最多同时携带 2 件快递，最后一次送达后不计算返航里程。

## 正式目标

**正式目标严格为两层词典序 `(逾期任务数, 总飞行里程)`**：先最小化逾期任务数，数量相同时最小化总里程。`总逾期分钟` 仅作诊断指标，绝不参与任何 Score 比较、best 更新或接受判定。

> 早期文档中的 `(逾期任务数, 总逾期分钟, 总里程)` 三层表述均为 legacy/outdated。

## 题意假设

附件没有提供起点坐标，默认实验采用 `(0, 0) km`，命令行可通过 `--depot-x` 和 `--depot-y` 覆盖。Excel 的 `Tmax` 表头明确使用分钟，因此内部速度采用 `0.9 km/min`，等价于题目的 `15 m/s`。仅送达点受软截止期约束；逾期任务仍必须完成。所有无人机从 `t=0` 独立出发，取送作业耗时为 0。

用户转换的 CSV 为 GB18030 编码，加载器同时支持 GB18030 和 UTF-8。原始命题数据统一存放在仓库根的 `data/raw/`：`.xls` 和转换后的 `.xlsx` 保留作审计输入，求解命令默认读取其中的 `.csv`，不需要额外 Excel 依赖。

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
├── run_cooperative_halns_benchmark.py   Cooperative-HALNS 基准（Gate A/B/C/D）
├── run_phase1_verify.py                  3 种子 × 60s 机制触发 Gate
├── run_dynamic_cooperative_benchmark.py  旧 B1 动态协作 5 种子 × 240s
├── run_swap_v2_benchmark.py              交换 v2 3 种子 × 240s
├── run_three_layer_benchmark.py          三层 A2 3 种子 × 240s
├── run_ablation.py                       两层 vs 三层 A2 消融
├── run_physical_diagnosis.py             任务物理可行性诊断
├── run_relay_swap_validate.py            B.2/B.3 时间可行性与交换校验
├── compare_a2_coop.py / quick_240s_bench.py   快速对比脚本
└── show_coop_stats.py                    读取 Gate C 结果统计
results/              原始运行、汇总统计与最终路线
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
  --input 'data/raw/命题1-低空经济场景下的物流无人机调度算法数据.csv' \
  --method halns --tasks 200 --drones 8 --max-tasks 25 \
  --iterations 400 --time-limit 240 --seed 2026080504 \
  --output algorithm/results/solution.json
```

`--method` 还支持 `exact`、`edd`、`nearest`、`greedy`、`regret2` 和 `alns-core`；`basic-alns` 作为 `alns-core` 的兼容别名保留。当前 ALNS Core 默认用 assignment destroy 替换旧 route-clear，并启用 deadline-risk guidance；不启用驱逐交换、VND、cluster repair 或路线池。HALNS 在此基础上启用 ejection，route pool 默认关闭。VND、cluster repair 和全部细分预算可通过 Python API 的 `ALNSConfig` 显式开启。将 `--candidate-limit` 设为 `0` 可关闭候选位置剪枝；固定迭代数适合复现比较，`--time-limit` 适合墙钟预算控制。小规模精确求解默认最多 10 个任务，可通过 `--exact-max-tasks` 调整，但状态空间指数增长。

## 复现实验

Cooperative-HALNS 的完整基准（unit test → 冒烟 → 3×60s 对比 → common-start 公平实验）见
`experiments/run_cooperative_halns_benchmark.py`；机制触发 Gate 见 `experiments/run_phase1_verify.py`。

研究报告与实验报告统一存放在仓库根的 [docs/](../docs/)：

- [Pickup-First Cooperative HALNS 重构报告](../docs/COOPERATIVE_HALNS_REPORT.md)（现行，正式目标两层）
- [SWAP-only 对比报告](../docs/SWAP_ONLY_COMPARISON_REPORT.md)（历史，legacy 三层口径）
- [三层消融报告](../docs/THREE_LAYER_ABLATION_REPORT.md)（历史，legacy 三层口径）
- [Relay 两阶段求解验证报告](../docs/RSLA_2S_ALNS_RELAY_VALIDATION_REPORT.docx)（历史，legacy）

原始逐运行结果与完整路线位于 `results/` 各子目录。

## 历史实验脚本

以下实验脚本保留在 `experiments/` 供复跑，但**结论请以 docs/ 下现行报告为准**（三层目标表述均为
legacy/outdated）：

- `run_three_layer_benchmark.py` — 三层 A2 3 种子 × 240s
- `run_ablation.py` — 两层 vs 三层 A2 消融
- `run_swap_v2_benchmark.py` / `run_relay_swap_validate.py` — 交换 v2 基准与校验
- `run_dynamic_cooperative_benchmark.py` — 旧 B1 动态协作 5 种子 × 240s
- `run_physical_diagnosis.py` — 任务物理可行性诊断
- `compare_a2_coop.py` / `quick_240s_bench.py` — 快速对比脚本

