# 30 秒 7 场景配对墙钟对比实验报告

日期：2026-08-15
环境：Windows-11-10.0.22621-SP0 / Python 3.13.3；200 tasks / 8 UAV / capacity=2 / K=25
配置：seed 2026080500–2026080500（1 种子，7 个场景共用同一 seed），candidate_limit=48，初始解 = regret-2 直送构造（构造时间计入预算）
Relay 网络：**4 个中继站，每任务 2 个候选站，绕行比 2.0，兜底开启**，选址方法 `weighted_kmedoids`，选址种子 42
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
| `pure_direct` | 77.00 | 2683.62 | 682.56 | 26.51 | 0.00 | 0.00 |
| `pure_relay` | 90.00 | 3383.73 | 732.09 | 9.87 | 20.00 | 19.00 |
| `direct_relay` | 82.00 | 2728.35 | 693.73 | 21.08 | 5.00 | 3.00 |
| `direct_stations` | 74.00 | 2706.92 | 661.41 | 27.72 | 0.00 | 0.00 |
| `direct_relay_stations` | 77.00 | 2746.36 | 684.13 | 22.65 | 6.00 | 5.00 |
| `direct_stations_fixed` | 72.00 | 2346.33 | 655.97 | 26.39 | 0.00 | 0.00 |
| `direct_relay_stations_fixed` | 75.00 | 2408.61 | 665.48 | 22.00 | 8.00 | 7.00 |

基准：`pure_direct`（纯 Direct，原点起点）。

## 2. 构造阶段 vs 最终得分

| 场景 | 构造得分（0 迭代） | 最终得分 | 构造→最终改进（逾期 min / 里程 km） |
|---|---:|---:|---:|
| `pure_direct` | `(108, 4464.18, 775.77)` | `(77, 2683.6187136534213, 682.5571037563097)` | -1780.56 / -93.22 |
| `pure_relay` | `(108, 4383.54, 780.89)` | `(90, 3383.7333479008394, 732.0920388879251)` | -999.80 / -48.80 |
| `direct_relay` | `(108, 4464.18, 775.77)` | `(82, 2728.3453819489423, 693.7317704745764)` | -1735.84 / -82.04 |
| `direct_stations` | `(103, 3900.23, 733.14)` | `(74, 2706.9158347595553, 661.4134778976975)` | -1193.31 / -71.73 |
| `direct_relay_stations` | `(103, 3900.23, 733.14)` | `(77, 2746.356712965192, 684.1260540807642)` | -1153.87 / -49.02 |
| `direct_stations_fixed` | `(111, 4324.83, 760.07)` | `(72, 2346.326735961088, 655.9737650902077)` | -1978.50 / -104.10 |
| `direct_relay_stations_fixed` | `(111, 4324.83, 760.07)` | `(75, 2408.6130413058704, 665.4786772162935)` | -1916.22 / -94.59 |

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
| `direct_relay_stations_fixed` | 11.12 | 22.89 | 157 | 8.29 | 13.98 |

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
| `direct_relay_stations_fixed` | depot:4 / relay_1:1 / relay_2:1 / relay_3:1 / relay_4:1 | depot:100.5 / relay_1:153.2 / relay_2:151.1 / relay_3:151.0 / relay_4:150.8 | 9.34 | 1.17 | 18.23% |

- 部署分布=每个候选起点（depot / relay_<id>）实际部署的 UAV 数，由任务空间分布与时间窗需求动态决定，允许 0 架、2 架甚至更多（不再是一站一机）；`demand 得分`=各候选起点的需求加权分（空间衰减×紧迫度×服务价值）；`初始空飞`=构造解（0 次搜索迭代）各机起点→首个访问的空飞；`就近命中率`=最终解中所在机起点即离该任务取件点最近部署点的任务占比（仅分析指标，不进目标）。

## 4. 与基准（纯 Direct）的逐种子配对比较

### pure_relay vs `pure_direct`

| Seed | 基准得分 | pure_relay 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(77, 2683.6187136534213, 682.5571037563097)` | `(90, 3383.7333479008394, 732.0920388879251)` | +13 | +700.11 | +49.53 | 20 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : pure_relay 0** |

### direct_relay vs `pure_direct`

| Seed | 基准得分 | direct_relay 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(77, 2683.6187136534213, 682.5571037563097)` | `(82, 2728.3453819489423, 693.7317704745764)` | +5 | +44.73 | +11.17 | 5 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay 0** |

### direct_stations vs `pure_direct`

| Seed | 基准得分 | direct_stations 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(77, 2683.6187136534213, 682.5571037563097)` | `(74, 2706.9158347595553, 661.4134778976975)` | -3 | +23.30 | -21.14 | 0 | direct_stations |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_stations 1** |

### direct_relay_stations vs `pure_direct`

| Seed | 基准得分 | direct_relay_stations 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(77, 2683.6187136534213, 682.5571037563097)` | `(77, 2746.356712965192, 684.1260540807642)` | +0 | +62.74 | +1.57 | 6 | pure_direct |
| **小计** | | | | | | | **pure_direct 1 : tie 0 : direct_relay_stations 0** |

### direct_stations_fixed vs `pure_direct`

| Seed | 基准得分 | direct_stations_fixed 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(77, 2683.6187136534213, 682.5571037563097)` | `(72, 2346.326735961088, 655.9737650902077)` | -5 | -337.29 | -26.58 | 0 | direct_stations_fixed |
| **小计** | | | | | | | **pure_direct 0 : tie 0 : direct_stations_fixed 1** |

### direct_relay_stations_fixed vs `pure_direct`

| Seed | 基准得分 | direct_relay_stations_fixed 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026080500 | `(77, 2683.6187136534213, 682.5571037563097)` | `(75, 2408.6130413058704, 665.4786772162935)` | -2 | -275.01 | -17.08 | 8 | direct_relay_stations_fixed |
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
| 2026080500 | `direct_relay` | `(82, 2728.35, 693.73)` | 21.08 | 546 | 5 | 3 | 28.0 |
| 2026080500 | `direct_relay_stations` | `(77, 2746.36, 684.13)` | 22.65 | 599 | 6 | 5 | 28.0 |
| 2026080500 | `direct_relay_stations_fixed` | `(75, 2408.61, 665.48)` | 22.00 | 580 | 8 | 7 | 28.0 |
| 2026080500 | `direct_stations` | `(74, 2706.92, 661.41)` | 27.72 | 733 | 0 | 0 | 28.0 |
| 2026080500 | `direct_stations_fixed` | `(72, 2346.33, 655.97)` | 26.39 | 695 | 0 | 0 | 28.0 |
| 2026080500 | `pure_direct` | `(77, 2683.62, 682.56)` | 26.51 | 690 | 0 | 0 | 28.0 |
| 2026080500 | `pure_relay` | `(90, 3383.73, 732.09)` | 9.87 | 257 | 20 | 19 | 28.0 |

## 7. 结论与分析

**词典序质量排序（最优在前）：`direct_stations_fixed` > `direct_stations` > `direct_relay_stations_fixed` > `pure_direct` > `direct_relay_stations` > `direct_relay` > `pure_relay`**

- 第一目标（逾期任务数）与第二目标（总逾期分钟）在各场景间的词典序对比，见第 4/5 节配对结果。

### 7.1 预部署（起点不同）的影响

- Direct 场景：预部署使 `direct_stations`：late_count -3，总逾期 +23.30 min，里程 -21.14 km。
- Relay 场景：预部署使 `direct_relay_stations`：late_count -5，总逾期 +18.01 min，里程 -9.61 km。

### 7.2 动态部署 vs 固定一站一机（A/B）

- **direct_stations（动态部署）vs direct_stations_fixed（固定一站一机）**：`direct_stations_fixed`：late_count +2，总逾期 +360.59 min，里程 +5.44 km。
  - 部署分布：动态 `{'depot': 1, 'relay_1': 2, 'relay_2': 2, 'relay_3': 2, 'relay_4': 1}` vs 固定 `{'depot': 4, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1}`；初始空飞总里程 5.15 vs 9.34 km；就近命中率 29.50% vs 19.00%。
- **direct_relay_stations（动态部署）vs direct_relay_stations_fixed（固定一站一机）**：`direct_relay_stations_fixed`：late_count +2，总逾期 +337.74 min，里程 +18.65 km。
  - 部署分布：动态 `{'depot': 1, 'relay_1': 2, 'relay_2': 2, 'relay_3': 2, 'relay_4': 1}` vs 固定 `{'depot': 4, 'relay_1': 1, 'relay_2': 1, 'relay_3': 1, 'relay_4': 1}`；初始空飞总里程 5.15 vs 9.34 km；就近命中率 27.32% vs 18.23%。

### 7.3 纯 Relay（warmup=0）与分阶段（warmup=0.8）

- 纯 Relay 迭代 257（it/s=9.87，全局评估 12,284）远少于分阶段 546（it/s=21.08，全局评估 2,598）。
- 纯 Relay 从弱起点出发并采用 20 个中继任务（跨机 19 次），但 `pure_relay`：late_count +13，总逾期 +700.11 min，里程 +49.53 km，是唯一词典序劣于基准的场景。
- 分阶段 `direct_relay` 中继阶段（5.2s）把预热得分 `(82, 2735.3, 692.64)` 改进到 `(82, 2728.3453819489423, 693.7317704745764)`（逾期 -6.95 min）。

### 7.4 分阶段 Relay 搜索阶段的改进

| 场景 | 预热得分 | 最终得分 | 中继阶段改进 | relay 任务 | 跨机接驳 |
|---|---:|---:|---:|---:|---|
| `direct_relay` | `(82, 2735.3, 692.64)` | `(82, 2728.3453819489423, 693.7317704745764)` | 逾期 -6.95 min | 5 | 3 |
| `direct_relay_stations` | `(77, 2784.54, 686.85)` | `(77, 2746.356712965192, 684.1260540807642)` | 逾期 -38.18 min | 6 | 5 |

### 7.5 综合结论

- 本 seed 下最优场景为 `direct_stations_fixed`（得分 `(72, 2346.326735961088, 655.9737650902077)`）。
- 预部署（`stations` 起点）使 `direct_stations`：late_count -3，总逾期 +23.30 min，里程 -21.14 km；省去「原点→站」空飞是其主要来源（见第 3 节 deadhead 对比）。
- 纯 Relay（warmup=0）在本预算下不可行：中继邻域单次迭代极贵（全局评估次数是分阶段的约 5 倍），迭代数锐减，虽采用大量中继却从弱起点出发，最终词典序最差。分阶段（先 80% 建立强 DIRECT 起点，再 20% 中继搜索）是唯一能发挥中继价值的结构。
- 在预部署下中继边际收益变小（`direct_relay_stations`：late_count +3，总逾期 +39.44 min，里程 +22.71 km）：预部署已把大部分任务调度得足够好，中继主要作为第二/三目标的微调手段。

## 8. 可审计性

- 求解器源码 SHA-256：`ccdf1412942d1c383de00debd2d41b4fc6569cc3e1c6bcf39567f92847b90c46`
- 输入 CSV SHA-256：`386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`
- 中继站选址方法：`weighted_kmedoids`；起点感知开关：home_seed=False，home_displaced=False，home_bias=False，disable_home_aware_init=False（stations 场景默认开启 home 感知初始构造，除非显式禁用）
- 原始记录：`d:\uav_project\algorithm\results\dynamic_deployment_30s_relay4`（`scenario_comparison_results.json`、`scenario_comparison_runs.csv`、`run_solutions/` 内每场景每种子完整路线）
- 生成时间：2026-08-15T08:13:12.793339+00:00
