# 30 秒 7 场景配对墙钟对比实验报告

日期：2026-08-15
环境：Windows-11-10.0.22621-SP0 / Python 3.13.3；200 tasks / 8 UAV / capacity=2 / K=25
配置：seed 2026080500–2026080500（1 种子，7 个场景共用同一 seed），candidate_limit=48，初始解 = regret-2 直送构造（构造时间计入预算）
Relay 网络：**4 个中继站（需求驱动自动确定），每任务 2 个候选站，绕行比 2.0，兜底开启**，选址方法 `weighted_kmedoids`，选址种子 42
求解：墙钟 30s（安全余量 2s），分阶段 warmup 比例见场景定义；构造后中继播种 + 跨机精化开启（direct 场景无中继故不生效）

## 场景定义（7 个场景共用同一 seed，各自跑满总预算）

| 场景 | 定义 | Relay | warmup | 起点（drone homes） |
|---|---|---|---|---|
| `pure_direct` | 纯 Direct（原点起点，无中继） | 否 | — | origin（原点） |
| `pure_relay` | 纯 Relay（原点起点，warmup=0） | 是 | 0.00 | origin（原点） |
| `direct_relay` | Direct + Relay（原点起点，80/20 分阶段） | 是 | 0.80 | origin（原点） |
| `direct_stations` | Direct + 起点不同（预部署中继站） | 否 | — | stations（动态部署，按任务需求分配 0..n 架） |
| `direct_relay_stations` | Direct + Relay + 起点不同（预部署 + 80/20） | 是 | 0.80 | stations（动态部署，按任务需求分配 0..n 架） |
| `direct_stations_fixed` | Direct + 起点不同（固定一站一机，A/B 基准） | 否 | — | stations_fixed（固定一站一机，A/B 基准） |
| `direct_relay_stations_fixed` | Direct + Relay + 起点不同（固定一站一机 + 80/20，A/B 基准） | 是 | 0.80 | stations_fixed（固定一站一机，A/B 基准） |

## 1. 主要结果（各场景均值）

| 场景 | late_count | total_lateness_min | distance_km | it/s | relay 任务数 | 跨机接驳 |
|---|---:|---:|---:|---:|---:|---:|
| `pure_direct` | 76.00 | 2658.07 | 687.39 | 26.97 | 0.00 | 0.00 |
| `pure_relay` | 90.00 | 3383.73 | 732.09 | 10.49 | 20.00 | 19.00 |
| `direct_relay` | 79.00 | 2796.03 | 706.36 | 23.38 | 4.00 | 4.00 |
| `direct_stations` | 74.00 | 2702.98 | 661.02 | 28.33 | 0.00 | 0.00 |
| `direct_relay_stations` | 77.00 | 2746.36 | 684.13 | 22.65 | 6.00 | 5.00 |
| `direct_stations_fixed` | 73.00 | 2354.59 | 668.02 | 24.09 | 0.00 | 0.00 |
| `direct_relay_stations_fixed` | 75.00 | 2611.26 | 690.42 | 20.53 | 9.00 | 8.00 |

基准：`pure_direct`（纯 Direct，原点起点）。

## 2. 构造阶段 vs 最终得分

| 场景 | 构造得分（0 迭代） | 最终得分 | 构造→最终改进（逾期 min / 里程 km） |
|---|---:|---:|---:|
| `pure_direct` | `(108, 4464.18, 775.77)` | `(76, 2658.0734471631213, 687.392154226679)` | -1806.11 / -88.38 |
| `pure_relay` | `(108, 4383.54, 780.89)` | `(90, 3383.7333479008394, 732.0920388879251)` | -999.80 / -48.80 |
| `direct_relay` | `(108, 4464.18, 775.77)` | `(79, 2796.0267591904562, 706.3576268437403)` | -1668.16 / -69.42 |
| `direct_stations` | `(103, 3900.23, 733.14)` | `(74, 2702.9835780417675, 661.0202522259186)` | -1197.25 / -72.12 |
| `direct_relay_stations` | `(103, 3900.23, 733.14)` | `(77, 2746.356712965192, 684.1260540807642)` | -1153.87 / -49.02 |
| `direct_stations_fixed` | `(111, 4324.83, 760.07)` | `(73, 2354.5857212040296, 668.0163847234609)` | -1970.25 / -92.06 |
| `direct_relay_stations_fixed` | `(111, 4324.83, 760.07)` | `(75, 2611.2551540594227, 690.4170988962454)` | -1713.58 / -69.66 |

- 构造得分是 regret-2 直送构造在 0 次搜索迭代时的得分（构造时间计入预算）；构造与最终之间的差距反映搜索对起点优势的留存或抹平程度：若搜索后差距缩小，说明起点收益主要是冷启动优势，短预算下更明显。

## 3. 首段空飞与部署利用率

| 场景 | deadhead_km | vs 全驻原点节省 | 就近错配任务数 | 部署首段下界 LB | 全驻原点 LB |
|---|---:|---:|---:|---:|---:|
| `pure_direct` | 21.79 | 0.00 | 0 | 13.98 | 13.98 |
| `pure_relay` | 24.13 | 0.00 | 0 | 13.98 | 13.98 |
| `direct_relay` | 22.23 | 0.00 | 0 | 13.98 | 13.98 |
| `direct_stations` | 8.48 | 42.31 | 141 | 4.00 | 13.98 |
| `direct_relay_stations` | 8.35 | 42.18 | 141 | 4.00 | 13.98 |
| `direct_stations_fixed` | 11.12 | 22.89 | 162 | 8.29 | 13.98 |
| `direct_relay_stations_fixed` | 11.12 | 22.89 | 155 | 8.29 | 13.98 |

- `deadhead_km`=各机起点→首个访问的空飞总里程；`vs 全驻原点节省`=若全部驻原点时同样路线的首段总里程减实际首段（部署节省）；`就近错配任务数`=所在机起点并非离该任务取件点最近部署点的任务数；LB=每机到其最近任务取件点距离之和的下界，两 LB 之差给出分散部署在首段上的杠杆上限。

## 3b. 动态部署分布与初始空飞

| 场景 | 部署分布 | demand 得分 | 初始空飞总 km | 初始空飞均值 km | 最终就近命中率 |
|---|---|---|---:|---:|---:|
| `pure_direct` | depot:8 |  | 22.62 | 2.83 | 100.00% |
| `pure_relay` | depot:8 |  | 22.62 | 2.83 | 100.00% |
| `direct_relay` | depot:8 |  | 22.62 | 2.83 | 100.00% |
| `direct_stations` | depot:1 / relay_1:2 / relay_2:2 / relay_3:2 / relay_4:1 | depot:100.5 / relay_1:153.2 / relay_2:151.1 / relay_3:151.0 / relay_4:150.8 | 5.15 | 0.64 | 29.50% |
| `direct_relay_stations` | depot:1 / relay_1:2 / relay_2:2 / relay_3:2 / relay_4:1 | depot:100.5 / relay_1:153.2 / relay_2:151.1 / relay_3:151.0 / relay_4:150.8 | 5.15 | 0.64 | 27.32% |
| `direct_stations_fixed` | depot:4 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 | depot:100.5 / relay_1:153.2 / relay_2:151.1 / relay_3:151.0 / relay_4:150.8 | 9.34 | 1.17 | 19.00% |
| `direct_relay_stations_fixed` | depot:4 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 | depot:100.5 / relay_1:153.2 / relay_2:151.1 / relay_3:151.0 / relay_4:150.8 | 9.34 | 1.17 | 18.85% |

- 部署分布=每个候选起点（depot / relay_<id>）实际部署的 UAV 数，由任务空间分布与时间窗需求动态决定，允许 0 架、2 架甚至更多（不再是一站一机）；`demand 得分`=各候选起点的需求加权分（空间衰减×紧迫度×服务价值）；`初始空飞`=构造解（0 次搜索迭代）各机起点→首个访问的空飞；`就近命中率`=最终解中所在机起点即离该任务取件点最近部署点的任务占比（仅分析指标，不进目标）。

## 4. 与基准（纯 Direct）的逐种子配对比较

### pure_relay vs `pure_direct`

| Seed | 基准得分 | pure_relay 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(76, 2658.0734471631213, 687.392154226679)` | `(90, 3383.7333479008394, 732.0920388879251)` | +14 | +725.66 | +44.70 | 20 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : pure_relay 0** |

### direct_relay vs `pure_direct`

| Seed | 基准得分 | direct_relay 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(76, 2658.0734471631213, 687.392154226679)` | `(79, 2796.0267591904562, 706.3576268437403)` | +3 | +137.95 | +18.97 | 4 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay 0** |

### direct_stations vs `pure_direct`

| Seed | 基准得分 | direct_stations 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(76, 2658.0734471631213, 687.392154226679)` | `(74, 2702.9835780417675, 661.0202522259186)` | -2 | +44.91 | -26.37 | 0 | direct_stations |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_stations 1** |

### direct_relay_stations vs `pure_direct`

| Seed | 基准得分 | direct_relay_stations 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(76, 2658.0734471631213, 687.392154226679)` | `(77, 2746.356712965192, 684.1260540807642)` | +1 | +88.28 | -3.27 | 6 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay_stations 0** |

### direct_stations_fixed vs `pure_direct`

| Seed | 基准得分 | direct_stations_fixed 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(76, 2658.0734471631213, 687.392154226679)` | `(73, 2354.5857212040296, 668.0163847234609)` | -3 | -303.49 | -19.38 | 0 | direct_stations_fixed |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_stations_fixed 1** |

### direct_relay_stations_fixed vs `pure_direct`

| Seed | 基准得分 | direct_relay_stations_fixed 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(76, 2658.0734471631213, 687.392154226679)` | `(75, 2611.2551540594227, 690.4170988962454)` | -1 | -46.82 | +3.02 | 9 | direct_relay_stations_fixed |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_relay_stations_fixed 1** |

## 5. 全场景两两胜率矩阵（行 vs 列，win/tie/loss）

| 行 \ 列 | `pure_direct` | `pure_relay` | `direct_relay` | `direct_stations` | `direct_relay_stations` | `direct_stations_fixed` | `direct_relay_stations_fixed` |
|---|---|---|---|---|---|---|---|
| `pure_direct` | — | 1/0/0 | 1/0/0 | 0/0/1 | 1/0/0 | 0/0/1 | 0/0/1 |
| `pure_relay` | 0/0/1 | — | 0/0/1 | 0/0/1 | 0/0/1 | 0/0/1 | 0/0/1 |
| `direct_relay` | 0/0/1 | 1/0/0 | — | 0/0/1 | 0/0/1 | 0/0/1 | 0/0/1 |
| `direct_stations` | 1/0/0 | 1/0/0 | 1/0/0 | — | 1/0/0 | 0/0/1 | 1/0/0 |
| `direct_relay_stations` | 0/0/1 | 1/0/0 | 1/0/0 | 0/0/1 | — | 0/0/1 | 0/0/1 |
| `direct_stations_fixed` | 1/0/0 | 1/0/0 | 1/0/0 | 1/0/0 | 1/0/0 | — | 1/0/0 |
| `direct_relay_stations_fixed` | 1/0/0 | 1/0/0 | 1/0/0 | 0/0/1 | 1/0/0 | 0/0/1 | — |

## 6. 逐种子明细

| Seed | 场景 | 得分 | it/s | 迭代 | relay 任务 | 跨机接驳 | 总墙钟(s) |
|---|---|---:|---:|---:|---:|---:|---:|
| 2026080500 | `direct_relay` | `(79, 2796.03, 706.36)` | 23.38 | 611 | 4 | 4 | 28.0 |
| 2026080500 | `direct_relay_stations` | `(77, 2746.36, 684.13)` | 22.65 | 598 | 6 | 5 | 28.0 |
| 2026080500 | `direct_relay_stations_fixed` | `(75, 2611.26, 690.42)` | 20.53 | 535 | 9 | 8 | 28.0 |
| 2026080500 | `direct_stations` | `(74, 2702.98, 661.02)` | 28.33 | 752 | 0 | 0 | 28.0 |
| 2026080500 | `direct_stations_fixed` | `(73, 2354.59, 668.02)` | 24.09 | 627 | 0 | 0 | 28.0 |
| 2026080500 | `pure_direct` | `(76, 2658.07, 687.39)` | 26.97 | 703 | 0 | 0 | 28.0 |
| 2026080500 | `pure_relay` | `(90, 3383.73, 732.09)` | 10.49 | 274 | 20 | 19 | 28.0 |

## 7. 结论与分析

**词典序质量排序（最优在前）：`direct_stations_fixed` > `direct_stations` > `direct_relay_stations_fixed` > `pure_direct` > `direct_relay_stations` > `direct_relay` > `pure_relay`**

- 第一目标（逾期任务数）与第二目标（总逾期分钟）在各场景间的词典序对比，见第 4/5 节配对结果。

### 7.1 预部署（起点不同）的影响

- Direct 场景：预部署使 `direct_stations`：late_count -2，总逾期 +44.91 min，里程 -26.37 km。
- Relay 场景：预部署使 `direct_relay_stations`：late_count -2，总逾期 -49.67 min，里程 -22.23 km。

### 7.2 动态部署 vs 固定一站一机（A/B）

- **direct_stations（动态部署）vs direct_stations_fixed（固定一站一机）**：`direct_stations_fixed`：late_count +1，总逾期 +348.40 min，里程 -7.00 km。
  - 部署分布：动态 `{'depot': 1, 'relay_1': 2, 'relay_2': 2, 'relay_3': 2, 'relay_4': 1}` vs 固定 `{'depot': 4, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1}`；初始空飞总里程 5.15 vs 9.34 km；就近命中率 29.50% vs 19.00%。
- **direct_relay_stations（动态部署）vs direct_relay_stations_fixed（固定一站一机）**：`direct_relay_stations_fixed`：late_count +2，总逾期 +135.10 min，里程 -6.29 km。
  - 部署分布：动态 `{'depot': 1, 'relay_1': 2, 'relay_2': 2, 'relay_3': 2, 'relay_4': 1}` vs 固定 `{'depot': 4, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1}`；初始空飞总里程 5.15 vs 9.34 km；就近命中率 27.32% vs 18.85%。

### 7.3 纯 Relay（warmup=0）与分阶段（warmup=0.8）

- 纯 Relay 迭代 274（it/s=10.49，全局评估 13,090）远少于分阶段 611（it/s=23.38，全局评估 2,638）。
- 纯 Relay 从弱起点出发并采用 20 个中继任务（跨机 19 次），但 `pure_relay`：late_count +14，总逾期 +725.66 min，里程 +44.70 km，是唯一词典序劣于基准的场景。
- 分阶段 `direct_relay` 中继阶段（5.2s）把预热得分 `(79, 2814.34, 700.8)` 改进到 `(79, 2796.0267591904562, 706.3576268437403)`（逾期 -18.31 min）。

### 7.4 分阶段 Relay 搜索阶段的改进

| 场景 | 预热得分 | 最终得分 | 中继阶段改进 | relay 任务 | 跨机接驳 |
|---|---:|---:|---:|---:|---|
| `direct_relay` | `(79, 2814.34, 700.8)` | `(79, 2796.0267591904562, 706.3576268437403)` | 逾期 -18.31 min | 4 | 4 |
| `direct_relay_stations` | `(77, 2784.54, 686.85)` | `(77, 2746.356712965192, 684.1260540807642)` | 逾期 -38.18 min | 6 | 5 |

### 7.5 综合结论

- 本 seed 下最优场景为 `direct_stations_fixed`（得分 `(73, 2354.5857212040296, 668.0163847234609)`）。
- 预部署（`stations` 起点）使 `direct_stations`：late_count -2，总逾期 +44.91 min，里程 -26.37 km；省去「原点→站」空飞是其主要来源（见第 3 节 deadhead 对比）。
- 纯 Relay（warmup=0）在本预算下不可行：中继邻域单次迭代极贵（全局评估次数是分阶段的约 5 倍），迭代数锐减，虽采用大量中继却从弱起点出发，最终词典序最差。分阶段（先 80% 建立强 DIRECT 起点，再 20% 中继搜索）是唯一能发挥中继价值的结构。
- 在预部署下中继边际收益变小（`direct_relay_stations`：late_count +3，总逾期 +43.37 min，里程 +23.11 km）：预部署已把大部分任务调度得足够好，中继主要作为第二/三目标的微调手段。

## 8. 可审计性

- 求解器源码 SHA-256：`75c485f54e6250dcd9339feb5f7d225fa1123dcb0cf84cca843365fd6b0b74e2`
- 输入 CSV SHA-256：`386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`
- 中继站选址方法：`weighted_kmedoids`；起点感知开关：home_seed=False，home_displaced=False，home_bias=False，disable_home_aware_init=False（stations 场景默认开启 home 感知初始构造，除非显式禁用）
- 原始记录：`d:\uav_project\algorithm\results\dynamic_deployment_30s_auto`（`scenario_comparison_results.json`、`scenario_comparison_runs.csv`、`run_solutions/` 内每场景每种子完整路线）
- 生成时间：2026-08-15T08:22:23.731362+00:00
