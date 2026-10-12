# 四策略灵敏度分析

四种策略采用同一随机种子和（逾期任务数、总逾期时间、总航程）三级词典序目标。本次每次独立求解均为 300 s 总预算；两种 Relay 策略的阶段占比均为 80% / 20%。
每个配置仅采用一个随机种子（n=1），属于探索性比较，不提供置信区间或统计显著性结论。
纯 Direct 不使用站点，因此站点数量分析不适用。相同策略的基准点在不同参数轴中复用同一个本次生成的解；这些重复展示不构成独立重复试验。

| 策略 | 参数 | 水平 | 无人机 | 截止期倍率 | 站点 | 逾期数 | 总逾期/min | 航程/km | Relay任务 | 复用来源 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Direct（原点起点） | drone_count | 8 | 8 | 1.0 | 0 | 62 | 2058.969 | 621.249 | 0 | — |
| Direct（原点起点） | drone_count | 9 | 9 | 1.0 | 0 | 52 | 1427.862 | 626.603 | 0 | — |
| Direct（原点起点） | drone_count | 10 | 10 | 1.0 | 0 | 42 | 1154.369 | 651.873 | 0 | — |
| Direct（原点起点） | drone_count | 12 | 12 | 1.0 | 0 | 22 | 481.418 | 655.456 | 0 | — |
| Direct（原点起点） | drone_count | 16 | 16 | 1.0 | 0 | 0 | 0.000 | 628.815 | 0 | — |
| Direct（原点起点） | deadline_multiplier | 0.8 | 8 | 0.8 | 0 | 94 | 3083.775 | 630.460 | 0 | — |
| Direct（原点起点） | deadline_multiplier | 0.9 | 8 | 0.9 | 0 | 77 | 2390.873 | 618.474 | 0 | — |
| Direct（原点起点） | deadline_multiplier | 1.0 | 8 | 1.0 | 0 | 62 | 2058.969 | 621.249 | 0 | pure_direct__drone_count__8 |
| Direct（原点起点） | deadline_multiplier | 1.1 | 8 | 1.1 | 0 | 56 | 1817.503 | 635.387 | 0 | — |
| Direct（原点起点） | deadline_multiplier | 1.2 | 8 | 1.2 | 0 | 44 | 1402.155 | 638.844 | 0 | — |
| Direct + Relay（原点起点） | drone_count | 8 | 8 | 1.0 | 4 | 64 | 2085.305 | 610.034 | 6 | — |
| Direct + Relay（原点起点） | drone_count | 9 | 9 | 1.0 | 4 | 52 | 1438.988 | 625.810 | 2 | — |
| Direct + Relay（原点起点） | drone_count | 10 | 10 | 1.0 | 4 | 45 | 1157.975 | 643.588 | 3 | — |
| Direct + Relay（原点起点） | drone_count | 12 | 12 | 1.0 | 4 | 24 | 527.063 | 655.305 | 0 | — |
| Direct + Relay（原点起点） | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 628.294 | 12 | — |
| Direct + Relay（原点起点） | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 94 | 3101.773 | 622.210 | 0 | — |
| Direct + Relay（原点起点） | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 78 | 2425.309 | 618.810 | 0 | — |
| Direct + Relay（原点起点） | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 64 | 2085.305 | 610.034 | 6 | direct_relay__drone_count__8 |
| Direct + Relay（原点起点） | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 57 | 1842.821 | 632.774 | 2 | — |
| Direct + Relay（原点起点） | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 45 | 1388.580 | 640.131 | 5 | — |
| Direct + Relay（原点起点） | station_count | 2 | 8 | 1.0 | 2 | 64 | 2147.610 | 632.661 | 2 | — |
| Direct + Relay（原点起点） | station_count | 3 | 8 | 1.0 | 3 | 64 | 2125.616 | 627.063 | 2 | — |
| Direct + Relay（原点起点） | station_count | 4 | 8 | 1.0 | 4 | 64 | 2085.305 | 610.034 | 6 | direct_relay__drone_count__8 |
| Direct + Relay（原点起点） | station_count | 5 | 8 | 1.0 | 5 | 64 | 2145.534 | 635.538 | 4 | — |
| Direct + Relay（原点起点） | station_count | 6 | 8 | 1.0 | 6 | 64 | 2122.937 | 628.014 | 7 | — |
| Direct + Station（需求驱动预部署） | drone_count | 8 | 8 | 1.0 | 4 | 62 | 2022.395 | 623.884 | 0 | — |
| Direct + Station（需求驱动预部署） | drone_count | 9 | 9 | 1.0 | 4 | 50 | 1395.133 | 610.334 | 0 | — |
| Direct + Station（需求驱动预部署） | drone_count | 10 | 10 | 1.0 | 4 | 35 | 781.665 | 612.564 | 0 | — |
| Direct + Station（需求驱动预部署） | drone_count | 12 | 12 | 1.0 | 4 | 15 | 239.551 | 614.571 | 0 | — |
| Direct + Station（需求驱动预部署） | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 576.531 | 0 | — |
| Direct + Station（需求驱动预部署） | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 83 | 2893.735 | 615.965 | 0 | — |
| Direct + Station（需求驱动预部署） | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 71 | 2311.293 | 614.185 | 0 | — |
| Direct + Station（需求驱动预部署） | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 62 | 2022.395 | 623.884 | 0 | direct_stations__drone_count__8 |
| Direct + Station（需求驱动预部署） | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 49 | 1500.320 | 614.547 | 0 | — |
| Direct + Station（需求驱动预部署） | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 40 | 1184.011 | 608.848 | 0 | — |
| Direct + Station（需求驱动预部署） | station_count | 2 | 8 | 1.0 | 2 | 62 | 2049.330 | 618.503 | 0 | — |
| Direct + Station（需求驱动预部署） | station_count | 3 | 8 | 1.0 | 3 | 65 | 2038.922 | 626.650 | 0 | — |
| Direct + Station（需求驱动预部署） | station_count | 4 | 8 | 1.0 | 4 | 62 | 2022.395 | 623.884 | 0 | direct_stations__drone_count__8 |
| Direct + Station（需求驱动预部署） | station_count | 5 | 8 | 1.0 | 5 | 61 | 1899.042 | 616.150 | 0 | — |
| Direct + Station（需求驱动预部署） | station_count | 6 | 8 | 1.0 | 6 | 61 | 1956.591 | 615.942 | 0 | — |
| Direct + Relay + Station | drone_count | 8 | 8 | 1.0 | 4 | 63 | 1997.157 | 621.492 | 2 | — |
| Direct + Relay + Station | drone_count | 9 | 9 | 1.0 | 4 | 50 | 1382.201 | 608.725 | 3 | — |
| Direct + Relay + Station | drone_count | 10 | 10 | 1.0 | 4 | 35 | 799.838 | 618.620 | 0 | — |
| Direct + Relay + Station | drone_count | 12 | 12 | 1.0 | 4 | 15 | 245.014 | 610.118 | 0 | — |
| Direct + Relay + Station | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 576.955 | 6 | — |
| Direct + Relay + Station | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 84 | 2912.327 | 622.953 | 2 | — |
| Direct + Relay + Station | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 72 | 2295.954 | 618.881 | 2 | — |
| Direct + Relay + Station | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 63 | 1997.157 | 621.492 | 2 | direct_relay_stations__drone_count__8 |
| Direct + Relay + Station | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 49 | 1554.997 | 625.693 | 4 | — |
| Direct + Relay + Station | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 40 | 1236.880 | 632.393 | 4 | — |
| Direct + Relay + Station | station_count | 2 | 8 | 1.0 | 2 | 62 | 2110.357 | 630.543 | 0 | — |
| Direct + Relay + Station | station_count | 3 | 8 | 1.0 | 3 | 65 | 2077.909 | 633.290 | 3 | — |
| Direct + Relay + Station | station_count | 4 | 8 | 1.0 | 4 | 63 | 1997.157 | 621.492 | 2 | direct_relay_stations__drone_count__8 |
| Direct + Relay + Station | station_count | 5 | 8 | 1.0 | 5 | 61 | 1905.188 | 615.512 | 2 | — |
| Direct + Relay + Station | station_count | 6 | 8 | 1.0 | 6 | 61 | 1998.309 | 616.753 | 4 | — |
