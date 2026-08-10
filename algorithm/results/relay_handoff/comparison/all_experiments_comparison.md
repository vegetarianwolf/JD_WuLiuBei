# 全部历史实验与新正式 Relay 对比

本表逐条保留 58 个历史配置，并追加 3 个 200 单、240 秒、primary-owner Relay 正式配置。

每个分组独立排名，名次均从 1 重新开始；不同 experiment_stage、任务规模或停止条件之间不得按名次直接比较。
表格只展示并按题面评分指标排序：先按时订单数（等价准时率），再总里程。

## constructive_n200 \| n=200 \| constructive-only

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | Regret-2 | no-relay | 1/1 | 92.000 [92, 92] | 46.000% [46.000%, 46.000%] | 775.773 [775.773, 775.773] |
| 2 | history / uav_benchmark | Greedy-full-position | no-relay | 1/1 | 89.000 [89, 89] | 44.500% [44.500%, 44.500%] | 852.054 [852.054, 852.054] |
| 3 | history / uav_benchmark | Nearest-adjacent | no-relay | 1/1 | 71.000 [71, 71] | 35.500% [35.500%, 35.500%] | 943.493 [943.493, 943.493] |
| 4 | history / uav_benchmark | EDD-adjacent | no-relay | 1/1 | 36.000 [36, 36] | 18.000% [18.000%, 18.000%] | 1293.988 [1293.988, 1293.988] |

## development_smoke_10s \| n=200 \| wall-clock 10 s

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / adaptive_smoke_wall | A2 + rejection_pool | no-relay | 1/1 | 98.000 [98, 98] | 49.000% [49.000%, 49.000%] | 753.697 [753.697, 753.697] |
| 2 | history / adaptive_smoke_wall | A2 + late_risk_destroy + rejection_pool + soft_deadline | no-relay | 1/1 | 98.000 [98, 98] | 49.000% [49.000%, 49.000%] | 779.569 [779.569, 779.569] |
| 3 | history / adaptive_smoke_wall | A2 + late_risk_destroy | no-relay | 1/1 | 96.000 [96, 96] | 48.000% [48.000%, 48.000%] | 760.736 [760.736, 760.736] |
| 4 | history / adaptive_smoke_wall | A2 | no-relay | 1/1 | 96.000 [96, 96] | 48.000% [48.000%, 48.000%] | 781.925 [781.925, 781.925] |
| 5 | history / adaptive_smoke_wall | A2 + soft_deadline | no-relay | 1/1 | 95.000 [95, 95] | 47.500% [47.500%, 47.500%] | 776.409 [776.409, 776.409] |

## development_smoke_5_iterations \| n=200 \| fixed 5 iterations

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / adaptive_smoke_iterations | A2 + late_risk_destroy + rejection_pool + soft_deadline | no-relay | 1/1 | 96.000 [96, 96] | 48.000% [48.000%, 48.000%] | 766.850 [766.850, 766.850] |
| 2 | history / adaptive_smoke_iterations | A2 + rejection_pool | no-relay | 1/1 | 96.000 [96, 96] | 48.000% [48.000%, 48.000%] | 794.142 [794.142, 794.142] |
| 3 | history / adaptive_smoke_iterations | A2 | no-relay | 1/1 | 95.000 [95, 95] | 47.500% [47.500%, 47.500%] | 765.386 [765.386, 765.386] |
| 4 | history / adaptive_smoke_iterations | A2 + late_risk_destroy | no-relay | 1/1 | 95.000 [95, 95] | 47.500% [47.500%, 47.500%] | 765.386 [765.386, 765.386] |
| 5 | history / adaptive_smoke_iterations | A2 + soft_deadline | no-relay | 1/1 | 95.000 [95, 95] | 47.500% [47.500%, 47.500%] | 776.409 [776.409, 776.409] |

## fixed_iterations_400 \| n=200 \| fixed 400 iterations

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | C2-Lex-HALNS | no-relay | 5/5 | 119.400 [116, 124] | 59.700% [58.000%, 62.000%] | 714.338 [694.695, 736.800] |
| 2 | history / uav_benchmark | C2-Lex-ALNS | no-relay | 5/5 | 116.600 [111, 120] | 58.300% [55.500%, 60.000%] | 716.337 [687.629, 768.929] |
| 3 | history / core_comparison | C2-Lex-ALNS-Core | no-relay | 5/5 | 116.600 [111, 120] | 58.300% [55.500%, 60.000%] | 716.337 [687.629, 768.929] |

## formal_240s \| n=200 \| wall-clock 240 s

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / adaptive_comparison | A2 + late_risk_destroy | no-relay | 3/3 | 131.333 [130, 134] | 65.667% [65.000%, 67.000%] | 645.854 [642.621, 650.177] |
| 2 | history / capacity_comparison | Current best A2 configuration | no-relay | 3/3 | 130.667 [129, 132] | 65.333% [64.500%, 66.000%] | 640.149 [629.812, 650.251] |
| 3 | history / adaptive_comparison | A2 | no-relay | 3/3 | 130.333 [130, 131] | 65.167% [65.000%, 65.500%] | 645.747 [633.590, 652.038] |
| 4 | history / neighborhood_ablation | A2 +risk | no-relay | 3/3 | 130.000 [129, 131] | 65.000% [64.500%, 65.500%] | 643.948 [634.746, 650.251] |
| 5 | history / neighborhood_ablation | A1 +assignment | no-relay | 3/3 | 130.000 [128, 131] | 65.000% [64.000%, 65.500%] | 649.241 [641.209, 663.422] |
| 6 | history / core_comparison | C2-Lex-ALNS-Core | no-relay | 3/3 | 129.667 [128, 133] | 64.833% [64.000%, 66.500%] | 646.561 [631.417, 654.568] |
| 7 | history / neighborhood_ablation | A0 baseline_core | no-relay | 3/3 | 129.667 [128, 133] | 64.833% [64.000%, 66.500%] | 647.610 [631.018, 657.245] |
| 8 | history / core_comparison | C2-Lex-HALNS | no-relay | 3/3 | 129.333 [127, 131] | 64.667% [63.500%, 65.500%] | 663.445 [656.310, 667.120] |
| 9 | history / capacity_comparison | Capacity-aware HALNS | no-relay | 3/3 | 129.000 [128, 131] | 64.500% [64.000%, 65.500%] | 646.874 [639.910, 660.485] |
| 10 | history / neighborhood_ablation | A5 +ejection | no-relay | 3/3 | 129.000 [128, 130] | 64.500% [64.000%, 65.000%] | 666.290 [648.628, 684.177] |
| 11 | history / neighborhood_ablation | A3 +VND | no-relay | 3/3 | 128.333 [127, 129] | 64.167% [63.500%, 64.500%] | 648.016 [630.903, 656.965] |
| 12 | history / neighborhood_ablation | A4 +cluster | no-relay | 3/3 | 126.000 [124, 129] | 63.000% [62.000%, 64.500%] | 649.880 [644.086, 653.314] |
| 13 | history / adaptive_comparison | A2 + rejection_pool | no-relay | 3/3 | 126.000 [125, 127] | 63.000% [62.500%, 63.500%] | 669.156 [663.654, 679.246] |
| 14 | history / adaptive_comparison | A2 + soft_deadline | no-relay | 3/3 | 125.667 [125, 126] | 62.833% [62.500%, 63.000%] | 653.681 [648.984, 657.845] |
| 15 | history / neighborhood_ablation | A6 +route_pool | no-relay | 3/3 | 125.000 [125, 125] | 62.500% [62.500%, 62.500%] | 683.648 [671.198, 691.192] |
| 16 | history / adaptive_comparison | A2 + late_risk_destroy + rejection_pool + soft_deadline | no-relay | 3/3 | 123.667 [123, 125] | 61.833% [61.500%, 62.500%] | 679.202 [666.966, 689.406] |

## new_formal_240s \| n=200 \| wall-clock 240 s

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_on_time_treatment | C2-Lex-HALNS [halns_control] | no-relay | 3/3 | 128.667 [125, 131] | 64.333% [62.500%, 65.500%] | 657.127 [654.118, 662.220] |
| 2 | history / capacity_on_time_treatment | Capacity-aware HALNS punctuality-first sacrifice treatment | no-relay | 3/3 | 128.333 [125, 131] | 64.167% [62.500%, 65.500%] | 670.553 [659.060, 678.933] |
| 3 | history / core_on_time_treatment | C2-Lex-ALNS-Core [core_treatment] | no-relay | 3/3 | 128.000 [126, 131] | 64.000% [63.000%, 65.500%] | 656.150 [645.086, 674.801] |
| 4 | history / uav_on_time_treatment | C2-Lex-HALNS [halns_treatment] | no-relay | 3/3 | 128.000 [125, 131] | 64.000% [62.500%, 65.500%] | 665.666 [656.588, 671.447] |
| 5 | history / adaptive_on_time_treatment | A2 + late_risk_destroy + deferred sacrifice pool | no-relay | 3/3 | 127.000 [126, 128] | 63.500% [63.000%, 64.000%] | 667.008 [657.377, 674.061] |
| 6 | history / stronger_on_time_treatment | A7 +risk +on-time-distance +deferred | no-relay | 3/3 | 125.333 [123, 127] | 62.667% [61.500%, 63.500%] | 649.100 [634.991, 662.802] |

## over_limit_extended \| n=200 \| extended run (>240 s; non-formal)

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | C2-Lex-HALNS-extended | no-relay | 1/1 | 133.000 [133, 133] | 66.500% [66.500%, 66.500%] | 641.636 [641.636, 641.636] |

## relay_formal_240s \| n=200 \| wall-clock 240 s

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | relay / relay_handoff | baseline | primary-owner | 3/3 | 130.333 [129, 132] | 65.167% [64.500%, 66.000%] | 650.583 [639.441, 658.158] |
| 2 | relay / relay_handoff | static-1hub | primary-owner | 3/3 | 129.000 [127, 132] | 64.500% [63.500%, 66.000%] | 641.248 [630.466, 652.429] |
| 3 | relay / relay_handoff | static-multihub | primary-owner | 3/3 | 128.667 [127, 132] | 64.333% [63.500%, 66.000%] | 639.733 [630.369, 647.206] |

## single_drone_n25 \| n=25 \| method-specific single-drone run

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | C2-Lex-ALNS | no-relay | 5/5 | 11.200 [11, 12] | 44.800% [44.000%, 48.000%] | 100.106 [97.491, 103.988] |
| 2 | history / uav_benchmark | C2-Lex-HALNS | no-relay | 5/5 | 11.200 [11, 12] | 44.800% [44.000%, 48.000%] | 100.106 [97.491, 103.988] |
| 3 | history / uav_benchmark | Greedy-full-position | no-relay | 1/1 | 10.000 [10, 10] | 40.000% [40.000%, 40.000%] | 117.666 [117.666, 117.666] |
| 4 | history / uav_benchmark | Nearest-adjacent | no-relay | 1/1 | 8.000 [8, 8] | 32.000% [32.000%, 32.000%] | 152.763 [152.763, 152.763] |
| 5 | history / uav_benchmark | Regret-2 | no-relay | 1/1 | 7.000 [7, 7] | 28.000% [28.000%, 28.000%] | 126.282 [126.282, 126.282] |
| 6 | history / uav_benchmark | EDD-adjacent | no-relay | 1/1 | 4.000 [4, 4] | 16.000% [16.000%, 16.000%] | 197.327 [197.327, 197.327] |

## small_exact \| n=5 \| method-specific exact control

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | C2-Lex-HALNS | no-relay | 1/1 | 5.000 [5, 5] | 100.000% [100.000%, 100.000%] | 28.953 [28.953, 28.953] |
| 2 | history / uav_benchmark | Pareto-DP | no-relay | 1/1 | 5.000 [5, 5] | 100.000% [100.000%, 100.000%] | 28.953 [28.953, 28.953] |
| 3 | history / uav_benchmark | Regret-2 | no-relay | 1/1 | 4.000 [4, 4] | 80.000% [80.000%, 80.000%] | 33.275 [33.275, 33.275] |

## small_exact \| n=8 \| method-specific exact control

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | C2-Lex-HALNS | no-relay | 1/1 | 7.000 [7, 7] | 87.500% [87.500%, 87.500%] | 46.404 [46.404, 46.404] |
| 2 | history / uav_benchmark | Pareto-DP | no-relay | 1/1 | 7.000 [7, 7] | 87.500% [87.500%, 87.500%] | 46.404 [46.404, 46.404] |
| 3 | history / uav_benchmark | Regret-2 | no-relay | 1/1 | 5.000 [5, 5] | 62.500% [62.500%, 62.500%] | 59.903 [59.903, 59.903] |

## small_exact \| n=10 \| method-specific exact control

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | C2-Lex-HALNS | no-relay | 1/1 | 8.000 [8, 8] | 80.000% [80.000%, 80.000%] | 54.646 [54.646, 54.646] |
| 2 | history / uav_benchmark | Pareto-DP | no-relay | 1/1 | 8.000 [8, 8] | 80.000% [80.000%, 80.000%] | 54.646 [54.646, 54.646] |
| 3 | history / uav_benchmark | Regret-2 | no-relay | 1/1 | 7.000 [7, 7] | 70.000% [70.000%, 70.000%] | 48.400 [48.400, 48.400] |

## small_exact \| n=12 \| method-specific exact control

| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |
|---:|---|---|---|---:|---:|---:|---:|
| 1 | history / uav_benchmark | Pareto-DP | no-relay | 1/1 | 9.000 [9, 9] | 75.000% [75.000%, 75.000%] | 62.411 [62.411, 62.411] |
| 2 | history / uav_benchmark | C2-Lex-HALNS | no-relay | 1/1 | 9.000 [9, 9] | 75.000% [75.000%, 75.000%] | 64.185 [64.185, 64.185] |
| 3 | history / uav_benchmark | Regret-2 | no-relay | 1/1 | 6.000 [6, 6] | 50.000% [50.000%, 50.000%] | 62.853 [62.853, 62.853] |
