# 30 秒 7 场景配对墙钟对比实验报告

日期：2026-08-15
环境：Windows-11-10.0.22621-SP0 / Python 3.13.3；200 tasks / 8 UAV / capacity=2 / K=25
配置：seed 2026080500–2026080500（1 种子，7 个场景共用同一 seed），candidate_limit=48，初始解 = regret-2 直送构造（构造时间计入预算）
Relay 网络：**7 个中继站，每任务 2 个候选站，绕行比 2.0，兜底开启**，选址方法 `weighted_kmedoids`，选址种子 42
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
| `pure_direct` | 76.00 | 2650.69 | 667.70 | 27.17 | 0.00 | 0.00 |
| `pure_relay` | 94.00 | 3816.12 | 789.28 | 10.32 | 28.00 | 23.00 |
| `direct_relay` | 81.00 | 2727.60 | 693.12 | 22.69 | 7.00 | 5.00 |
| `direct_stations` | 74.00 | 2595.20 | 683.77 | 27.25 | 0.00 | 0.00 |
| `direct_relay_stations` | 77.00 | 2667.68 | 680.28 | 22.73 | 6.00 | 6.00 |
| `direct_stations_fixed` | 74.00 | 2579.81 | 667.70 | 27.78 | 0.00 | 0.00 |
| `direct_relay_stations_fixed` | 77.00 | 2667.68 | 680.28 | 22.77 | 6.00 | 6.00 |

基准：`pure_direct`（纯 Direct，原点起点）。

## 2. 构造阶段 vs 最终得分

| 场景 | 构造得分（0 迭代） | 最终得分 | 构造→最终改进（逾期 min / 里程 km） |
|---|---:|---:|---:|
| `pure_direct` | `(108, 4464.18, 775.77)` | `(76, 2650.6948685581247, 667.7025871642916)` | -1813.49 / -108.07 |
| `pure_relay` | `(107, 4450.49, 788.87)` | `(94, 3816.1221040695136, 789.2751209102729)` | -634.37 / +0.41 |
| `direct_relay` | `(108, 4464.18, 775.77)` | `(81, 2727.598547194451, 693.1176868577144)` | -1736.58 / -82.66 |
| `direct_stations` | `(106, 4065.76, 740.83)` | `(74, 2595.204893706611, 683.7727413909481)` | -1470.55 / -57.05 |
| `direct_relay_stations` | `(106, 4065.76, 740.83)` | `(77, 2667.6836523095753, 680.2835182346051)` | -1398.08 / -60.54 |
| `direct_stations_fixed` | `(106, 4065.76, 740.83)` | `(74, 2579.81483402165, 667.7006104342414)` | -1485.94 / -73.13 |
| `direct_relay_stations_fixed` | `(106, 4065.76, 740.83)` | `(77, 2667.6836523095753, 680.2835182346051)` | -1398.08 / -60.54 |

- 构造得分是 regret-2 直送构造在 0 次搜索迭代时的得分（构造时间计入预算）；构造与最终之间的差距反映搜索对起点优势的留存或抹平程度：若搜索后差距缩小，说明起点收益主要是冷启动优势，短预算下更明显。

## 3. 首段空飞与部署利用率

| 场景 | deadhead_km | vs 全驻原点节省 | 就近错配任务数 | 部署首段下界 LB | 全驻原点 LB |
|---|---:|---:|---:|---:|---:|
| `pure_direct` | 21.79 | 0.00 | 0 | 13.98 | 13.98 |
| `pure_relay` | 23.49 | 0.00 | 0 | 13.98 | 13.98 |
| `direct_relay` | 22.23 | 0.00 | 0 | 13.98 | 13.98 |
| `direct_stations` | 6.76 | 38.80 | 157 | 3.39 | 13.98 |
| `direct_relay_stations` | 7.89 | 40.61 | 154 | 3.39 | 13.98 |
| `direct_stations_fixed` | 5.84 | 40.46 | 157 | 3.39 | 13.98 |
| `direct_relay_stations_fixed` | 7.89 | 40.61 | 154 | 3.39 | 13.98 |

- `deadhead_km`=各机起点→首个访问的空飞总里程；`vs 全驻原点节省`=若全部驻原点时同样路线的首段总里程减实际首段（部署节省）；`就近错配任务数`=所在机起点并非离该任务取件点最近部署点的任务数；LB=每机到其最近任务取件点距离之和的下界，两 LB 之差给出分散部署在首段上的杠杆上限。

## 3b. 动态部署分布与初始空飞

| 场景 | 部署分布 | demand 得分 | 初始空飞总 km | 初始空飞均值 km | 最终就近命中率 |
|---|---|---|---:|---:|---:|
| `pure_direct` | depot:8 |  | 22.62 | 2.83 | 100.00% |
| `pure_relay` | depot:8 |  | 22.62 | 2.83 | 100.00% |
| `direct_relay` | depot:8 |  | 22.62 | 2.83 | 100.00% |
| `direct_stations` | depot:1 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 / relay_5:1 / relay_6:1 / relay_7:1 | depot:100.6 / relay_1:153.6 / relay_2:152.7 / relay_3:151.6 / relay_4:150.9 / relay_5:142.5 / relay_6:139.7 / relay_7:149.9 | 3.67 | 0.46 | 21.50% |
| `direct_relay_stations` | depot:1 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 / relay_5:1 / relay_6:1 / relay_7:1 | depot:100.6 / relay_1:153.6 / relay_2:152.7 / relay_3:151.6 / relay_4:150.9 / relay_5:142.5 / relay_6:139.7 / relay_7:149.9 | 3.67 | 0.46 | 20.62% |
| `direct_stations_fixed` | depot:1 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 / relay_5:1 / relay_6:1 / relay_7:1 | depot:100.6 / relay_1:153.6 / relay_2:152.7 / relay_3:151.6 / relay_4:150.9 / relay_5:142.5 / relay_6:139.7 / relay_7:149.9 | 3.67 | 0.46 | 21.50% |
| `direct_relay_stations_fixed` | depot:1 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 / relay_5:1 / relay_6:1 / relay_7:1 | depot:100.6 / relay_1:153.6 / relay_2:152.7 / relay_3:151.6 / relay_4:150.9 / relay_5:142.5 / relay_6:139.7 / relay_7:149.9 | 3.67 | 0.46 | 20.62% |

- 部署分布=每个候选起点（depot / relay_<id>）实际部署的 UAV 数，由任务空间分布与时间窗需求动态决定，允许 0 架、2 架甚至更多（不再是一站一机）；`demand 得分`=各候选起点的需求加权分（空间衰减×紧迫度×服务价值）；`初始空飞`=构造解（0 次搜索迭代）各机起点→首个访问的空飞；`就近命中率`=最终解中所在机起点即离该任务取件点最近部署点的任务占比（仅分析指标，不进目标）。

## 4. 与基准（纯 Direct）的逐种子配对比较

### pure_relay vs `pure_direct`

| Seed | 基准得分 | pure_relay 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `[76, 2650.6948685581247, 667.7025871642916]` | `[94, 3816.1221040695136, 789.2751209102729]` | +18 | +1165.43 | +121.57 | 28 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : pure_relay 0** |

### direct_relay vs `pure_direct`

| Seed | 基准得分 | direct_relay 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `[76, 2650.6948685581247, 667.7025871642916]` | `[81, 2727.598547194451, 693.1176868577144]` | +5 | +76.90 | +25.42 | 7 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay 0** |

### direct_stations vs `pure_direct`

| Seed | 基准得分 | direct_stations 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `[76, 2650.6948685581247, 667.7025871642916]` | `[74, 2595.204893706611, 683.7727413909481]` | -2 | -55.49 | +16.07 | 0 | direct_stations |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_stations 1** |

### direct_relay_stations vs `pure_direct`

| Seed | 基准得分 | direct_relay_stations 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `[76, 2650.6948685581247, 667.7025871642916]` | `[77, 2667.6836523095753, 680.2835182346051]` | +1 | +16.99 | +12.58 | 6 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay_stations 0** |

### direct_stations_fixed vs `pure_direct`

| Seed | 基准得分 | direct_stations_fixed 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `[76, 2650.6948685581247, 667.7025871642916]` | `[74, 2579.81483402165, 667.7006104342414]` | -2 | -70.88 | -0.00 | 0 | direct_stations_fixed |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_stations_fixed 1** |

### direct_relay_stations_fixed vs `pure_direct`

| Seed | 基准得分 | direct_relay_stations_fixed 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `[76, 2650.6948685581247, 667.7025871642916]` | `[77, 2667.6836523095753, 680.2835182346051]` | +1 | +16.99 | +12.58 | 6 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay_stations_fixed 0** |

## 5. 全场景两两胜率矩阵（行 vs 列，win/tie/loss）

| 行 \ 列 | `pure_direct` | `pure_relay` | `direct_relay` | `direct_stations` | `direct_relay_stations` | `direct_stations_fixed` | `direct_relay_stations_fixed` |
|---|---|---|---|---|---|---|---|
| `pure_direct` | — | 1/0/0 | 1/0/0 | 0/0/1 | 1/0/0 | 0/0/1 | 1/0/0 |
| `pure_relay` | 0/0/1 | — | 0/0/1 | 0/0/1 | 0/0/1 | 0/0/1 | 0/0/1 |
| `direct_relay` | 0/0/1 | 1/0/0 | — | 0/0/1 | 0/0/1 | 0/0/1 | 0/0/1 |
| `direct_stations` | 1/0/0 | 1/0/0 | 1/0/0 | — | 1/0/0 | 0/0/1 | 1/0/0 |
| `direct_relay_stations` | 0/0/1 | 1/0/0 | 1/0/0 | 0/0/1 | — | 0/0/1 | 0/1/0 |
| `direct_stations_fixed` | 1/0/0 | 1/0/0 | 1/0/0 | 1/0/0 | 1/0/0 | — | 1/0/0 |
| `direct_relay_stations_fixed` | 0/0/1 | 1/0/0 | 1/0/0 | 0/0/1 | 0/1/0 | 0/0/1 | — |

## 6. 逐种子明细

| Seed | 场景 | 得分 | it/s | 迭代 | relay 任务 | 跨机接驳 | 总墙钟(s) |
|---|---|---:|---:|---:|---:|---:|---:|
| 2026080500 | `direct_relay` | `(81, 2727.60, 693.12)` | 22.69 | 591 | 7 | 5 | 28.0 |
| 2026080500 | `direct_relay_stations` | `(77, 2667.68, 680.28)` | 22.73 | 599 | 6 | 6 | 28.0 |
| 2026080500 | `direct_relay_stations_fixed` | `(77, 2667.68, 680.28)` | 22.77 | 602 | 6 | 6 | 28.0 |
| 2026080500 | `direct_stations` | `(74, 2595.20, 683.77)` | 27.25 | 718 | 0 | 0 | 28.0 |
| 2026080500 | `direct_stations_fixed` | `(74, 2579.81, 667.70)` | 27.78 | 732 | 0 | 0 | 28.0 |
| 2026080500 | `pure_direct` | `(76, 2650.69, 667.70)` | 27.17 | 708 | 0 | 0 | 28.0 |
| 2026080500 | `pure_relay` | `(94, 3816.12, 789.28)` | 10.32 | 269 | 28 | 23 | 28.0 |

## 7. 结论与分析

**词典序质量排序（最优在前）：`direct_stations_fixed` > `direct_stations` > `pure_direct` > `direct_relay_stations` > `direct_relay_stations_fixed` > `direct_relay` > `pure_relay`**

- 第一目标（逾期任务数）与第二目标（总逾期分钟）在各场景间的词典序对比，见第 4/5 节配对结果。

### 7.1 预部署（起点不同）的影响

- Direct 场景：预部署使 `direct_stations`：late_count -2，总逾期 -55.49 min，里程 +16.07 km。
- Relay 场景：预部署使 `direct_relay_stations`：late_count -4，总逾期 -59.91 min，里程 -12.83 km。

### 7.2 动态部署 vs 固定一站一机（A/B）

- **direct_stations（动态部署）vs direct_stations_fixed（固定一站一机）**：`direct_stations_fixed`：late_count +0，总逾期 +15.39 min，里程 +16.07 km。
  - 部署分布：动态 `{'depot': 1, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1, 'relay_5': 1, 'relay_6': 1, 'relay_7': 1}` vs 固定 `{'depot': 1, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1, 'relay_5': 1, 'relay_6': 1, 'relay_7': 1}`；初始空飞总里程 3.67 vs 3.67 km；就近命中率 21.50% vs 21.50%。
- **direct_relay_stations（动态部署）vs direct_relay_stations_fixed（固定一站一机）**：`direct_relay_stations_fixed`：late_count +0，总逾期 +0.00 min，里程 +0.00 km。
  - 部署分布：动态 `{'depot': 1, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1, 'relay_5': 1, 'relay_6': 1, 'relay_7': 1}` vs 固定 `{'depot': 1, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1, 'relay_5': 1, 'relay_6': 1, 'relay_7': 1}`；初始空飞总里程 3.67 vs 3.67 km；就近命中率 20.62% vs 20.62%。

### 7.3 纯 Relay（warmup=0）与分阶段（warmup=0.8）

- 纯 Relay 迭代 269（it/s=10.32，全局评估 12,667）远少于分阶段 591（it/s=22.69，全局评估 2,611）。
- 纯 Relay 从弱起点出发并采用 28 个中继任务（跨机 23 次），但 `pure_relay`：late_count +18，总逾期 +1165.43 min，里程 +121.57 km，是唯一词典序劣于基准的场景。
- 分阶段 `direct_relay` 中继阶段（5.2s）把预热得分 `(82, 2735.3, 692.64)` 改进到 `(81, 2727.598547194451, 693.1176868577144)`（逾期 -7.70 min）。

### 7.4 分阶段 Relay 搜索阶段的改进

| 场景 | 预热得分 | 最终得分 | 中继阶段改进 | relay 任务 | 跨机接驳 |
|---|---:|---:|---:|---:|---|
| `direct_relay` | `(82, 2735.3, 692.64)` | `(81, 2727.598547194451, 693.1176868577144)` | 逾期 -7.70 min | 7 | 5 |
| `direct_relay_stations` | `(77, 2694.59, 689.14)` | `(77, 2667.6836523095753, 680.2835182346051)` | 逾期 -26.91 min | 6 | 6 |

### 7.5 综合结论

- 本 seed 下最优场景为 `direct_stations_fixed`（得分 `(74, 2579.81483402165, 667.7006104342414)`）。
- 预部署（`stations` 起点）使 `direct_stations`：late_count -2，总逾期 -55.49 min，里程 +16.07 km；省去「原点→站」空飞是其主要来源（见第 3 节 deadhead 对比）。
- 纯 Relay（warmup=0）在本预算下不可行：中继邻域单次迭代极贵（全局评估次数是分阶段的约 5 倍），迭代数锐减，虽采用大量中继却从弱起点出发，最终词典序最差。分阶段（先 80% 建立强 DIRECT 起点，再 20% 中继搜索）是唯一能发挥中继价值的结构。
- 在预部署下中继边际收益变小（`direct_relay_stations`：late_count +3，总逾期 +72.48 min，里程 -3.49 km）：预部署已把大部分任务调度得足够好，中继主要作为第二/三目标的微调手段。

## 8. 可审计性

- 求解器源码 SHA-256：`ccdf1412942d1c383de00debd2d41b4fc6569cc3e1c6bcf39567f92847b90c46`
- 输入 CSV SHA-256：`386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`
- 中继站选址方法：`weighted_kmedoids`；起点感知开关：home_seed=False，home_displaced=False，home_bias=False，disable_home_aware_init=False（stations 场景默认开启 home 感知初始构造，除非显式禁用）
- 原始记录：`d:\uav_project\algorithm\results\dynamic_deployment_30s`（`scenario_comparison_results.json`、`scenario_comparison_runs.csv`、`run_solutions/` 内每场景每种子完整路线）
- 生成时间：2026-08-15T07:54:29.314182+00:00
