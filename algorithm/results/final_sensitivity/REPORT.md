# 四策略多种子灵敏度分析

四种策略采用相同的配对随机种子和（逾期任务数、总逾期时间、总航程）三级词典序目标。本次每次独立求解均为 300 s 总预算；两种 Relay 策略的阶段占比均为 80% / 20%。
每个配置采用 5 个配对随机种子（n=5）：2026081701、2026081702、2026081703、2026081704、2026081705。本表保留逐种子原始结果。
种子间变动反映本实例与本次计算环境下的求解随机性，不代表需求实例间的不确定性。
纯 Direct 不使用站点，因此站点数量分析不适用。同策略、同种子的基准点在不同参数轴中复用同一个本次生成的解；这些重复展示不构成独立重复试验。

| 策略 | 种子 | 参数 | 水平 | 无人机 | 截止期倍率 | 站点 | 逾期数 | 总逾期/min | 航程/km | Relay任务 | 复用来源 |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Direct（原点起点） | 2026081701 | drone_count | 8 | 8 | 1.0 | 0 | 63 | 2070.568 | 616.733 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | drone_count | 8 | 8 | 1.0 | 4 | 64 | 2104.583 | 618.708 | 5 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | drone_count | 8 | 8 | 1.0 | 4 | 62 | 2020.643 | 620.270 | 0 | — |
| Direct + Relay + Station | 2026081701 | drone_count | 8 | 8 | 1.0 | 4 | 63 | 1997.157 | 621.492 | 2 | — |
| Direct（原点起点） | 2026081701 | drone_count | 9 | 9 | 1.0 | 0 | 52 | 1427.862 | 626.603 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | drone_count | 9 | 9 | 1.0 | 4 | 52 | 1438.988 | 625.810 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | drone_count | 9 | 9 | 1.0 | 4 | 50 | 1395.133 | 610.334 | 0 | — |
| Direct + Relay + Station | 2026081701 | drone_count | 9 | 9 | 1.0 | 4 | 50 | 1382.201 | 608.725 | 3 | — |
| Direct（原点起点） | 2026081701 | drone_count | 10 | 10 | 1.0 | 0 | 45 | 1141.279 | 636.186 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | drone_count | 10 | 10 | 1.0 | 4 | 46 | 1206.777 | 641.878 | 5 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 781.665 | 612.564 | 0 | — |
| Direct + Relay + Station | 2026081701 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 799.838 | 618.620 | 0 | — |
| Direct（原点起点） | 2026081701 | drone_count | 12 | 12 | 1.0 | 0 | 23 | 514.701 | 648.207 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | drone_count | 12 | 12 | 1.0 | 4 | 24 | 555.500 | 652.081 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | drone_count | 12 | 12 | 1.0 | 4 | 15 | 241.727 | 610.990 | 0 | — |
| Direct + Relay + Station | 2026081701 | drone_count | 12 | 12 | 1.0 | 4 | 15 | 247.063 | 620.642 | 0 | — |
| Direct（原点起点） | 2026081701 | drone_count | 16 | 16 | 1.0 | 0 | 0 | 0.000 | 632.828 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 628.294 | 12 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 576.531 | 0 | — |
| Direct + Relay + Station | 2026081701 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 577.278 | 7 | — |
| Direct（原点起点） | 2026081701 | deadline_multiplier | 0.8 | 8 | 0.8 | 0 | 94 | 3089.147 | 618.724 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 94 | 3099.062 | 621.355 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 83 | 2893.735 | 615.965 | 0 | — |
| Direct + Relay + Station | 2026081701 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 84 | 2912.327 | 622.953 | 2 | — |
| Direct（原点起点） | 2026081701 | deadline_multiplier | 0.9 | 8 | 0.9 | 0 | 78 | 2425.309 | 618.810 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 78 | 2467.647 | 613.917 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 72 | 2283.799 | 616.710 | 0 | — |
| Direct + Relay + Station | 2026081701 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 72 | 2307.505 | 623.647 | 3 | — |
| Direct（原点起点） | 2026081701 | deadline_multiplier | 1.0 | 8 | 1.0 | 0 | 63 | 2070.568 | 616.733 | 0 | pure_direct__seed_2026081701__drone_count__8 |
| Direct + Relay（原点起点） | 2026081701 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 64 | 2104.583 | 618.708 | 5 | direct_relay__seed_2026081701__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081701 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 62 | 2020.643 | 620.270 | 0 | direct_stations__seed_2026081701__drone_count__8 |
| Direct + Relay + Station | 2026081701 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 63 | 1997.157 | 621.492 | 2 | direct_relay_stations__seed_2026081701__drone_count__8 |
| Direct（原点起点） | 2026081701 | deadline_multiplier | 1.1 | 8 | 1.1 | 0 | 57 | 1829.041 | 632.496 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 58 | 1853.712 | 645.162 | 6 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 49 | 1513.330 | 610.695 | 0 | — |
| Direct + Relay + Station | 2026081701 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 49 | 1584.933 | 626.917 | 2 | — |
| Direct（原点起点） | 2026081701 | deadline_multiplier | 1.2 | 8 | 1.2 | 0 | 45 | 1390.804 | 640.179 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 46 | 1393.036 | 632.112 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 40 | 1186.969 | 617.650 | 0 | — |
| Direct + Relay + Station | 2026081701 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 40 | 1236.880 | 632.393 | 3 | — |
| Direct + Relay（原点起点） | 2026081701 | station_count | 2 | 8 | 1.0 | 2 | 66 | 2156.840 | 640.744 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | station_count | 2 | 8 | 1.0 | 2 | 62 | 2049.330 | 618.503 | 0 | — |
| Direct + Relay + Station | 2026081701 | station_count | 2 | 8 | 1.0 | 2 | 62 | 2110.357 | 630.543 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | station_count | 3 | 8 | 1.0 | 3 | 66 | 2137.866 | 636.995 | 6 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | station_count | 3 | 8 | 1.0 | 3 | 65 | 2057.567 | 635.361 | 0 | — |
| Direct + Relay + Station | 2026081701 | station_count | 3 | 8 | 1.0 | 3 | 65 | 2089.248 | 628.626 | 0 | — |
| Direct + Relay（原点起点） | 2026081701 | station_count | 4 | 8 | 1.0 | 4 | 64 | 2104.583 | 618.708 | 5 | direct_relay__seed_2026081701__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081701 | station_count | 4 | 8 | 1.0 | 4 | 62 | 2020.643 | 620.270 | 0 | direct_stations__seed_2026081701__drone_count__8 |
| Direct + Relay + Station | 2026081701 | station_count | 4 | 8 | 1.0 | 4 | 63 | 1997.157 | 621.492 | 2 | direct_relay_stations__seed_2026081701__drone_count__8 |
| Direct + Relay（原点起点） | 2026081701 | station_count | 5 | 8 | 1.0 | 5 | 66 | 2186.626 | 647.405 | 5 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | station_count | 5 | 8 | 1.0 | 5 | 61 | 1899.042 | 616.150 | 0 | — |
| Direct + Relay + Station | 2026081701 | station_count | 5 | 8 | 1.0 | 5 | 61 | 1908.521 | 624.456 | 4 | — |
| Direct + Relay（原点起点） | 2026081701 | station_count | 6 | 8 | 1.0 | 6 | 66 | 2185.628 | 646.946 | 5 | — |
| Direct + Station（需求驱动预部署） | 2026081701 | station_count | 6 | 8 | 1.0 | 6 | 61 | 2005.743 | 620.510 | 0 | — |
| Direct + Relay + Station | 2026081701 | station_count | 6 | 8 | 1.0 | 6 | 62 | 2017.431 | 620.376 | 7 | — |
| Direct + Relay（原点起点） | 2026081702 | drone_count | 8 | 8 | 1.0 | 4 | 67 | 2200.221 | 639.032 | 0 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | drone_count | 8 | 8 | 1.0 | 4 | 61 | 1895.888 | 624.070 | 0 | — |
| Direct + Relay + Station | 2026081702 | drone_count | 8 | 8 | 1.0 | 4 | 62 | 1894.570 | 620.990 | 3 | — |
| Direct（原点起点） | 2026081702 | drone_count | 8 | 8 | 1.0 | 0 | 67 | 2172.540 | 632.476 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | drone_count | 9 | 9 | 1.0 | 4 | 58 | 1571.713 | 643.075 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | drone_count | 9 | 9 | 1.0 | 4 | 47 | 1306.722 | 618.778 | 0 | — |
| Direct + Relay + Station | 2026081702 | drone_count | 9 | 9 | 1.0 | 4 | 48 | 1276.729 | 617.436 | 1 | — |
| Direct（原点起点） | 2026081702 | drone_count | 9 | 9 | 1.0 | 0 | 57 | 1503.590 | 631.766 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | drone_count | 10 | 10 | 1.0 | 4 | 46 | 1102.273 | 636.218 | 0 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 799.726 | 606.676 | 0 | — |
| Direct + Relay + Station | 2026081702 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 833.245 | 609.983 | 3 | — |
| Direct（原点起点） | 2026081702 | drone_count | 10 | 10 | 1.0 | 0 | 45 | 1084.038 | 633.825 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | drone_count | 12 | 12 | 1.0 | 4 | 23 | 509.532 | 654.495 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | drone_count | 12 | 12 | 1.0 | 4 | 14 | 305.514 | 636.687 | 0 | — |
| Direct + Relay + Station | 2026081702 | drone_count | 12 | 12 | 1.0 | 4 | 15 | 316.635 | 629.223 | 0 | — |
| Direct（原点起点） | 2026081702 | drone_count | 12 | 12 | 1.0 | 0 | 23 | 479.979 | 651.897 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 640.666 | 7 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 572.898 | 0 | — |
| Direct + Relay + Station | 2026081702 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 572.757 | 4 | — |
| Direct（原点起点） | 2026081702 | drone_count | 16 | 16 | 1.0 | 0 | 0 | 0.000 | 636.208 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 94 | 3112.839 | 624.389 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 88 | 2848.891 | 607.938 | 0 | — |
| Direct + Relay + Station | 2026081702 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 89 | 2907.943 | 607.969 | 4 | — |
| Direct（原点起点） | 2026081702 | deadline_multiplier | 0.8 | 8 | 0.8 | 0 | 93 | 3089.150 | 616.505 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 81 | 2556.035 | 624.758 | 6 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 70 | 2293.625 | 606.885 | 0 | — |
| Direct + Relay + Station | 2026081702 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 70 | 2289.327 | 596.849 | 1 | — |
| Direct（原点起点） | 2026081702 | deadline_multiplier | 0.9 | 8 | 0.9 | 0 | 81 | 2540.612 | 622.516 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 67 | 2200.221 | 639.032 | 0 | direct_relay__seed_2026081702__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081702 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 61 | 1895.888 | 624.070 | 0 | direct_stations__seed_2026081702__drone_count__8 |
| Direct + Relay + Station | 2026081702 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 62 | 1894.570 | 620.990 | 3 | direct_relay_stations__seed_2026081702__drone_count__8 |
| Direct（原点起点） | 2026081702 | deadline_multiplier | 1.0 | 8 | 1.0 | 0 | 67 | 2172.540 | 632.476 | 0 | pure_direct__seed_2026081702__drone_count__8 |
| Direct + Relay（原点起点） | 2026081702 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 55 | 1746.289 | 614.233 | 0 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 51 | 1656.812 | 630.589 | 0 | — |
| Direct + Relay + Station | 2026081702 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 53 | 1764.601 | 640.053 | 4 | — |
| Direct（原点起点） | 2026081702 | deadline_multiplier | 1.1 | 8 | 1.1 | 0 | 55 | 1738.701 | 625.407 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 44 | 1364.955 | 632.978 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 37 | 1100.897 | 603.171 | 0 | — |
| Direct + Relay + Station | 2026081702 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 37 | 1139.190 | 612.184 | 6 | — |
| Direct（原点起点） | 2026081702 | deadline_multiplier | 1.2 | 8 | 1.2 | 0 | 43 | 1329.062 | 625.174 | 0 | — |
| Direct + Relay（原点起点） | 2026081702 | station_count | 2 | 8 | 1.0 | 2 | 67 | 2189.683 | 638.009 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | station_count | 2 | 8 | 1.0 | 2 | 61 | 1932.517 | 609.124 | 0 | — |
| Direct + Relay + Station | 2026081702 | station_count | 2 | 8 | 1.0 | 2 | 61 | 2007.412 | 621.589 | 1 | — |
| Direct + Relay（原点起点） | 2026081702 | station_count | 3 | 8 | 1.0 | 3 | 67 | 2188.862 | 637.822 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | station_count | 3 | 8 | 1.0 | 3 | 63 | 2045.138 | 624.939 | 0 | — |
| Direct + Relay + Station | 2026081702 | station_count | 3 | 8 | 1.0 | 3 | 63 | 2062.381 | 634.373 | 6 | — |
| Direct + Relay（原点起点） | 2026081702 | station_count | 4 | 8 | 1.0 | 4 | 67 | 2200.221 | 639.032 | 0 | direct_relay__seed_2026081702__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081702 | station_count | 4 | 8 | 1.0 | 4 | 61 | 1895.888 | 624.070 | 0 | direct_stations__seed_2026081702__drone_count__8 |
| Direct + Relay + Station | 2026081702 | station_count | 4 | 8 | 1.0 | 4 | 62 | 1894.570 | 620.990 | 3 | direct_relay_stations__seed_2026081702__drone_count__8 |
| Direct + Relay（原点起点） | 2026081702 | station_count | 5 | 8 | 1.0 | 5 | 67 | 2138.475 | 630.281 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | station_count | 5 | 8 | 1.0 | 5 | 58 | 1792.855 | 612.172 | 0 | — |
| Direct + Relay + Station | 2026081702 | station_count | 5 | 8 | 1.0 | 5 | 58 | 1820.816 | 618.010 | 1 | — |
| Direct + Relay（原点起点） | 2026081702 | station_count | 6 | 8 | 1.0 | 6 | 67 | 2136.245 | 626.820 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081702 | station_count | 6 | 8 | 1.0 | 6 | 57 | 1887.092 | 617.355 | 0 | — |
| Direct + Relay + Station | 2026081702 | station_count | 6 | 8 | 1.0 | 6 | 57 | 1882.960 | 610.839 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | drone_count | 8 | 8 | 1.0 | 4 | 61 | 1971.891 | 624.020 | 0 | — |
| Direct + Relay + Station | 2026081703 | drone_count | 8 | 8 | 1.0 | 4 | 62 | 2002.810 | 619.078 | 2 | — |
| Direct（原点起点） | 2026081703 | drone_count | 8 | 8 | 1.0 | 0 | 64 | 2164.122 | 635.245 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | drone_count | 8 | 8 | 1.0 | 4 | 66 | 2206.027 | 649.379 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | drone_count | 9 | 9 | 1.0 | 4 | 48 | 1291.298 | 616.810 | 0 | — |
| Direct + Relay + Station | 2026081703 | drone_count | 9 | 9 | 1.0 | 4 | 51 | 1334.954 | 615.390 | 2 | — |
| Direct（原点起点） | 2026081703 | drone_count | 9 | 9 | 1.0 | 0 | 52 | 1451.768 | 624.515 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | drone_count | 9 | 9 | 1.0 | 4 | 54 | 1480.632 | 631.604 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 791.281 | 610.115 | 0 | — |
| Direct + Relay + Station | 2026081703 | drone_count | 10 | 10 | 1.0 | 4 | 36 | 785.978 | 601.184 | 3 | — |
| Direct（原点起点） | 2026081703 | drone_count | 10 | 10 | 1.0 | 0 | 42 | 1050.288 | 637.755 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | drone_count | 10 | 10 | 1.0 | 4 | 42 | 1051.559 | 628.742 | 5 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | drone_count | 12 | 12 | 1.0 | 4 | 13 | 251.888 | 609.564 | 0 | — |
| Direct + Relay + Station | 2026081703 | drone_count | 12 | 12 | 1.0 | 4 | 13 | 260.872 | 613.667 | 0 | — |
| Direct（原点起点） | 2026081703 | drone_count | 12 | 12 | 1.0 | 0 | 23 | 509.624 | 644.416 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | drone_count | 12 | 12 | 1.0 | 4 | 23 | 509.932 | 649.814 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 566.744 | 0 | — |
| Direct + Relay + Station | 2026081703 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 573.432 | 3 | — |
| Direct（原点起点） | 2026081703 | drone_count | 16 | 16 | 1.0 | 0 | 0 | 0.000 | 633.846 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 633.353 | 11 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 86 | 2797.556 | 605.917 | 0 | — |
| Direct + Relay + Station | 2026081703 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 87 | 2803.873 | 597.005 | 1 | — |
| Direct（原点起点） | 2026081703 | deadline_multiplier | 0.8 | 8 | 0.8 | 0 | 91 | 3085.924 | 628.775 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 91 | 3111.392 | 629.170 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 70 | 2300.768 | 610.797 | 0 | — |
| Direct + Relay + Station | 2026081703 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 72 | 2297.114 | 612.869 | 5 | — |
| Direct（原点起点） | 2026081703 | deadline_multiplier | 0.9 | 8 | 0.9 | 0 | 82 | 2611.498 | 624.691 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 82 | 2616.411 | 617.464 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 61 | 1971.891 | 624.020 | 0 | direct_stations__seed_2026081703__drone_count__8 |
| Direct + Relay + Station | 2026081703 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 62 | 2002.810 | 619.078 | 2 | direct_relay_stations__seed_2026081703__drone_count__8 |
| Direct（原点起点） | 2026081703 | deadline_multiplier | 1.0 | 8 | 1.0 | 0 | 64 | 2164.122 | 635.245 | 0 | pure_direct__seed_2026081703__drone_count__8 |
| Direct + Relay（原点起点） | 2026081703 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 66 | 2206.027 | 649.379 | 1 | direct_relay__seed_2026081703__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081703 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 52 | 1507.918 | 619.311 | 0 | — |
| Direct + Relay + Station | 2026081703 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 52 | 1602.689 | 628.568 | 6 | — |
| Direct（原点起点） | 2026081703 | deadline_multiplier | 1.1 | 8 | 1.1 | 0 | 55 | 1806.086 | 634.262 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 56 | 1862.759 | 650.079 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 41 | 1253.035 | 612.699 | 0 | — |
| Direct + Relay + Station | 2026081703 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 42 | 1264.769 | 623.357 | 2 | — |
| Direct（原点起点） | 2026081703 | deadline_multiplier | 1.2 | 8 | 1.2 | 0 | 42 | 1349.146 | 629.979 | 0 | — |
| Direct + Relay（原点起点） | 2026081703 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 42 | 1355.794 | 633.840 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | station_count | 2 | 8 | 1.0 | 2 | 61 | 1961.173 | 603.304 | 0 | — |
| Direct + Relay + Station | 2026081703 | station_count | 2 | 8 | 1.0 | 2 | 62 | 2088.033 | 627.353 | 2 | — |
| Direct + Relay（原点起点） | 2026081703 | station_count | 2 | 8 | 1.0 | 2 | 67 | 2260.428 | 640.057 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | station_count | 3 | 8 | 1.0 | 3 | 60 | 1973.815 | 618.289 | 0 | — |
| Direct + Relay + Station | 2026081703 | station_count | 3 | 8 | 1.0 | 3 | 60 | 1998.969 | 630.348 | 5 | — |
| Direct + Relay（原点起点） | 2026081703 | station_count | 3 | 8 | 1.0 | 3 | 67 | 2232.116 | 637.462 | 5 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | station_count | 4 | 8 | 1.0 | 4 | 61 | 1971.891 | 624.020 | 0 | direct_stations__seed_2026081703__drone_count__8 |
| Direct + Relay + Station | 2026081703 | station_count | 4 | 8 | 1.0 | 4 | 62 | 2002.810 | 619.078 | 2 | direct_relay_stations__seed_2026081703__drone_count__8 |
| Direct + Relay（原点起点） | 2026081703 | station_count | 4 | 8 | 1.0 | 4 | 66 | 2206.027 | 649.379 | 1 | direct_relay__seed_2026081703__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081703 | station_count | 5 | 8 | 1.0 | 5 | 59 | 1846.591 | 608.179 | 0 | — |
| Direct + Relay + Station | 2026081703 | station_count | 5 | 8 | 1.0 | 5 | 59 | 1864.216 | 609.254 | 2 | — |
| Direct + Relay（原点起点） | 2026081703 | station_count | 5 | 8 | 1.0 | 5 | 67 | 2232.572 | 637.021 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081703 | station_count | 6 | 8 | 1.0 | 6 | 64 | 1932.933 | 618.541 | 0 | — |
| Direct + Relay + Station | 2026081703 | station_count | 6 | 8 | 1.0 | 6 | 65 | 1928.661 | 606.250 | 3 | — |
| Direct + Relay（原点起点） | 2026081703 | station_count | 6 | 8 | 1.0 | 6 | 67 | 2231.825 | 636.662 | 4 | — |
| Direct + Relay + Station | 2026081704 | drone_count | 8 | 8 | 1.0 | 4 | 61 | 1919.730 | 614.083 | 2 | — |
| Direct（原点起点） | 2026081704 | drone_count | 8 | 8 | 1.0 | 0 | 65 | 2206.306 | 644.432 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | drone_count | 8 | 8 | 1.0 | 4 | 66 | 2212.477 | 641.783 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | drone_count | 8 | 8 | 1.0 | 4 | 61 | 1878.278 | 614.021 | 0 | — |
| Direct + Relay + Station | 2026081704 | drone_count | 9 | 9 | 1.0 | 4 | 49 | 1370.856 | 619.095 | 2 | — |
| Direct（原点起点） | 2026081704 | drone_count | 9 | 9 | 1.0 | 0 | 57 | 1648.942 | 647.606 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | drone_count | 9 | 9 | 1.0 | 4 | 59 | 1651.392 | 643.800 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | drone_count | 9 | 9 | 1.0 | 4 | 49 | 1338.663 | 619.946 | 0 | — |
| Direct + Relay + Station | 2026081704 | drone_count | 10 | 10 | 1.0 | 4 | 36 | 847.965 | 618.190 | 1 | — |
| Direct（原点起点） | 2026081704 | drone_count | 10 | 10 | 1.0 | 0 | 45 | 1155.739 | 640.660 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | drone_count | 10 | 10 | 1.0 | 4 | 46 | 1170.346 | 654.711 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | drone_count | 10 | 10 | 1.0 | 4 | 36 | 811.864 | 619.314 | 0 | — |
| Direct + Relay + Station | 2026081704 | drone_count | 12 | 12 | 1.0 | 4 | 19 | 365.383 | 634.635 | 3 | — |
| Direct（原点起点） | 2026081704 | drone_count | 12 | 12 | 1.0 | 0 | 24 | 513.815 | 652.989 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | drone_count | 12 | 12 | 1.0 | 4 | 27 | 528.718 | 655.110 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | drone_count | 12 | 12 | 1.0 | 4 | 19 | 367.142 | 635.226 | 0 | — |
| Direct + Relay + Station | 2026081704 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 578.003 | 7 | — |
| Direct（原点起点） | 2026081704 | drone_count | 16 | 16 | 1.0 | 0 | 0 | 0.000 | 638.461 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 640.630 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 576.827 | 0 | — |
| Direct + Relay + Station | 2026081704 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 83 | 2835.318 | 616.277 | 3 | — |
| Direct（原点起点） | 2026081704 | deadline_multiplier | 0.8 | 8 | 0.8 | 0 | 95 | 3142.160 | 637.782 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 95 | 3226.684 | 631.756 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 82 | 2814.605 | 618.039 | 0 | — |
| Direct + Relay + Station | 2026081704 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 72 | 2253.336 | 606.527 | 3 | — |
| Direct（原点起点） | 2026081704 | deadline_multiplier | 0.9 | 8 | 0.9 | 0 | 77 | 2565.741 | 618.997 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 78 | 2626.549 | 636.744 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 72 | 2227.264 | 601.120 | 0 | — |
| Direct + Relay + Station | 2026081704 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 61 | 1919.730 | 614.083 | 2 | direct_relay_stations__seed_2026081704__drone_count__8 |
| Direct（原点起点） | 2026081704 | deadline_multiplier | 1.0 | 8 | 1.0 | 0 | 65 | 2206.306 | 644.432 | 0 | pure_direct__seed_2026081704__drone_count__8 |
| Direct + Relay（原点起点） | 2026081704 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 66 | 2212.477 | 641.783 | 2 | direct_relay__seed_2026081704__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081704 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 61 | 1878.278 | 614.021 | 0 | direct_stations__seed_2026081704__drone_count__8 |
| Direct + Relay + Station | 2026081704 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 50 | 1422.164 | 617.624 | 5 | — |
| Direct（原点起点） | 2026081704 | deadline_multiplier | 1.1 | 8 | 1.1 | 0 | 59 | 1811.025 | 638.159 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 59 | 1808.447 | 637.301 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 50 | 1412.738 | 608.244 | 0 | — |
| Direct + Relay + Station | 2026081704 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 39 | 1255.578 | 632.654 | 4 | — |
| Direct（原点起点） | 2026081704 | deadline_multiplier | 1.2 | 8 | 1.2 | 0 | 45 | 1334.294 | 630.730 | 0 | — |
| Direct + Relay（原点起点） | 2026081704 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 45 | 1335.800 | 629.319 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 39 | 1219.164 | 627.508 | 0 | — |
| Direct + Relay + Station | 2026081704 | station_count | 2 | 8 | 1.0 | 2 | 63 | 2000.951 | 618.002 | 2 | — |
| Direct + Relay（原点起点） | 2026081704 | station_count | 2 | 8 | 1.0 | 2 | 65 | 2204.663 | 649.525 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | station_count | 2 | 8 | 1.0 | 2 | 61 | 1971.949 | 612.493 | 0 | — |
| Direct + Relay + Station | 2026081704 | station_count | 3 | 8 | 1.0 | 3 | 64 | 2005.076 | 619.606 | 3 | — |
| Direct + Relay（原点起点） | 2026081704 | station_count | 3 | 8 | 1.0 | 3 | 65 | 2202.485 | 645.431 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | station_count | 3 | 8 | 1.0 | 3 | 60 | 1910.911 | 620.228 | 0 | — |
| Direct + Relay + Station | 2026081704 | station_count | 4 | 8 | 1.0 | 4 | 61 | 1919.730 | 614.083 | 2 | direct_relay_stations__seed_2026081704__drone_count__8 |
| Direct + Relay（原点起点） | 2026081704 | station_count | 4 | 8 | 1.0 | 4 | 66 | 2212.477 | 641.783 | 2 | direct_relay__seed_2026081704__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081704 | station_count | 4 | 8 | 1.0 | 4 | 61 | 1878.278 | 614.021 | 0 | direct_stations__seed_2026081704__drone_count__8 |
| Direct + Relay + Station | 2026081704 | station_count | 5 | 8 | 1.0 | 5 | 63 | 1966.009 | 612.968 | 2 | — |
| Direct + Relay（原点起点） | 2026081704 | station_count | 5 | 8 | 1.0 | 5 | 65 | 2204.663 | 649.525 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | station_count | 5 | 8 | 1.0 | 5 | 63 | 1966.165 | 611.314 | 0 | — |
| Direct + Relay + Station | 2026081704 | station_count | 6 | 8 | 1.0 | 6 | 62 | 2000.738 | 619.224 | 4 | — |
| Direct + Relay（原点起点） | 2026081704 | station_count | 6 | 8 | 1.0 | 6 | 65 | 2204.318 | 645.342 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081704 | station_count | 6 | 8 | 1.0 | 6 | 62 | 1995.861 | 615.146 | 0 | — |
| Direct（原点起点） | 2026081705 | drone_count | 8 | 8 | 1.0 | 0 | 65 | 2015.721 | 626.005 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | drone_count | 8 | 8 | 1.0 | 4 | 66 | 2057.329 | 616.565 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | drone_count | 8 | 8 | 1.0 | 4 | 59 | 1818.409 | 608.906 | 0 | — |
| Direct + Relay + Station | 2026081705 | drone_count | 8 | 8 | 1.0 | 4 | 59 | 1821.952 | 611.494 | 4 | — |
| Direct（原点起点） | 2026081705 | drone_count | 9 | 9 | 1.0 | 0 | 51 | 1575.710 | 627.306 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | drone_count | 9 | 9 | 1.0 | 4 | 54 | 1587.787 | 639.792 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | drone_count | 9 | 9 | 1.0 | 4 | 52 | 1311.768 | 605.241 | 0 | — |
| Direct + Relay + Station | 2026081705 | drone_count | 9 | 9 | 1.0 | 4 | 52 | 1317.439 | 607.895 | 6 | — |
| Direct（原点起点） | 2026081705 | drone_count | 10 | 10 | 1.0 | 0 | 42 | 1031.258 | 633.898 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | drone_count | 10 | 10 | 1.0 | 4 | 42 | 1073.360 | 644.604 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 765.415 | 594.753 | 0 | — |
| Direct + Relay + Station | 2026081705 | drone_count | 10 | 10 | 1.0 | 4 | 35 | 766.536 | 595.727 | 1 | — |
| Direct（原点起点） | 2026081705 | drone_count | 12 | 12 | 1.0 | 0 | 25 | 435.582 | 649.736 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | drone_count | 12 | 12 | 1.0 | 4 | 26 | 428.569 | 646.776 | 3 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | drone_count | 12 | 12 | 1.0 | 4 | 19 | 373.392 | 628.452 | 0 | — |
| Direct + Relay + Station | 2026081705 | drone_count | 12 | 12 | 1.0 | 4 | 19 | 372.814 | 627.980 | 2 | — |
| Direct（原点起点） | 2026081705 | drone_count | 16 | 16 | 1.0 | 0 | 0 | 0.000 | 635.259 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 637.133 | 6 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 571.192 | 0 | — |
| Direct + Relay + Station | 2026081705 | drone_count | 16 | 16 | 1.0 | 4 | 0 | 0.000 | 572.406 | 3 | — |
| Direct（原点起点） | 2026081705 | deadline_multiplier | 0.8 | 8 | 0.8 | 0 | 91 | 2967.889 | 614.393 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 92 | 3014.482 | 627.021 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 83 | 2860.213 | 607.925 | 0 | — |
| Direct + Relay + Station | 2026081705 | deadline_multiplier | 0.8 | 8 | 0.8 | 4 | 84 | 2946.436 | 625.999 | 3 | — |
| Direct（原点起点） | 2026081705 | deadline_multiplier | 0.9 | 8 | 0.9 | 0 | 81 | 2857.572 | 653.128 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 81 | 2932.213 | 668.569 | 7 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 73 | 2324.882 | 602.655 | 0 | — |
| Direct + Relay + Station | 2026081705 | deadline_multiplier | 0.9 | 8 | 0.9 | 4 | 73 | 2331.173 | 603.993 | 1 | — |
| Direct（原点起点） | 2026081705 | deadline_multiplier | 1.0 | 8 | 1.0 | 0 | 65 | 2015.721 | 626.005 | 0 | pure_direct__seed_2026081705__drone_count__8 |
| Direct + Relay（原点起点） | 2026081705 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 66 | 2057.329 | 616.565 | 1 | direct_relay__seed_2026081705__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081705 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 59 | 1818.409 | 608.906 | 0 | direct_stations__seed_2026081705__drone_count__8 |
| Direct + Relay + Station | 2026081705 | deadline_multiplier | 1.0 | 8 | 1.0 | 4 | 59 | 1821.952 | 611.494 | 4 | direct_relay_stations__seed_2026081705__drone_count__8 |
| Direct（原点起点） | 2026081705 | deadline_multiplier | 1.1 | 8 | 1.1 | 0 | 55 | 1800.246 | 635.428 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 56 | 1825.023 | 635.723 | 2 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 48 | 1511.665 | 606.506 | 0 | — |
| Direct + Relay + Station | 2026081705 | deadline_multiplier | 1.1 | 8 | 1.1 | 4 | 48 | 1511.313 | 611.503 | 1 | — |
| Direct（原点起点） | 2026081705 | deadline_multiplier | 1.2 | 8 | 1.2 | 0 | 47 | 1447.507 | 646.418 | 0 | — |
| Direct + Relay（原点起点） | 2026081705 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 48 | 1522.151 | 644.750 | 4 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 41 | 1198.988 | 617.043 | 0 | — |
| Direct + Relay + Station | 2026081705 | deadline_multiplier | 1.2 | 8 | 1.2 | 4 | 41 | 1203.183 | 621.735 | 2 | — |
| Direct + Relay（原点起点） | 2026081705 | station_count | 2 | 8 | 1.0 | 2 | 66 | 2047.167 | 615.245 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | station_count | 2 | 8 | 1.0 | 2 | 59 | 1885.633 | 622.363 | 0 | — |
| Direct + Relay + Station | 2026081705 | station_count | 2 | 8 | 1.0 | 2 | 61 | 1937.628 | 626.783 | 2 | — |
| Direct + Relay（原点起点） | 2026081705 | station_count | 3 | 8 | 1.0 | 3 | 66 | 2045.796 | 618.478 | 0 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | station_count | 3 | 8 | 1.0 | 3 | 60 | 1861.461 | 605.798 | 0 | — |
| Direct + Relay + Station | 2026081705 | station_count | 3 | 8 | 1.0 | 3 | 61 | 1862.125 | 601.680 | 1 | — |
| Direct + Relay（原点起点） | 2026081705 | station_count | 4 | 8 | 1.0 | 4 | 66 | 2057.329 | 616.565 | 1 | direct_relay__seed_2026081705__drone_count__8 |
| Direct + Station（需求驱动预部署） | 2026081705 | station_count | 4 | 8 | 1.0 | 4 | 59 | 1818.409 | 608.906 | 0 | direct_stations__seed_2026081705__drone_count__8 |
| Direct + Relay + Station | 2026081705 | station_count | 4 | 8 | 1.0 | 4 | 59 | 1821.952 | 611.494 | 4 | direct_relay_stations__seed_2026081705__drone_count__8 |
| Direct + Relay（原点起点） | 2026081705 | station_count | 5 | 8 | 1.0 | 5 | 66 | 2056.185 | 617.175 | 1 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | station_count | 5 | 8 | 1.0 | 5 | 59 | 1839.795 | 599.701 | 0 | — |
| Direct + Relay + Station | 2026081705 | station_count | 5 | 8 | 1.0 | 5 | 59 | 1862.088 | 607.262 | 1 | — |
| Direct + Relay（原点起点） | 2026081705 | station_count | 6 | 8 | 1.0 | 6 | 66 | 2051.846 | 617.368 | 0 | — |
| Direct + Station（需求驱动预部署） | 2026081705 | station_count | 6 | 8 | 1.0 | 6 | 60 | 1968.748 | 607.912 | 0 | — |
| Direct + Relay + Station | 2026081705 | station_count | 6 | 8 | 1.0 | 6 | 62 | 1977.950 | 618.959 | 2 | — |
