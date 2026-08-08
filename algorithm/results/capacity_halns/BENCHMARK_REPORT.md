# Capacity-Aware HALNS Benchmark

## Protocol

Fresh matched runs on `/Users/vegetarianwolf/Projects/JD_WuLiuBei/algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv` with 200 tasks, 8 UAVs, capacity 2, seeds [2026080500, 2026080501, 2026080502], and a 240-second total wall-clock budget per run. Every run shares the same regret-2 initial solution. A2 enables assignment destroy and deadline risk; capacity-aware HALNS changes only pair regret repair and hypergraph destroy.

Results are compared strictly as `(late tasks, total lateness, distance)`; no weighted score is used.

## Matched runs

| Seed | Variant | Late tasks | Total lateness (min) | Distance (km) | Runtime (s) | Iterations |
|---:|---|---:|---:|---:|---:|---:|
| 2026080500 | a2 | 71 | 2413.883076 | 650.251044 | 238.001 | 1829 |
| 2026080500 | capacity_halns | 69 | 2299.505134 | 640.225982 | 238.001 | 1420 |
| 2026080501 | capacity_halns | 72 | 2346.709342 | 639.910145 | 238.003 | 1557 |
| 2026080501 | a2 | 69 | 2248.676749 | 629.812100 | 238.000 | 1935 |
| 2026080502 | a2 | 68 | 2290.577018 | 640.383094 | 238.001 | 1863 |
| 2026080502 | capacity_halns | 72 | 2338.832654 | 660.484981 | 238.000 | 1592 |

## Aggregate comparison

| Variant | Mean late tasks | Mean lateness (min) | Mean distance (km) | Mean runtime (s) | Mean iterations | Best lexicographic score |
|---|---:|---:|---:|---:|---:|---|
| a2 | 69.333 | 2317.712281 | 640.148746 | 238.001 | 1875.7 | (68, 2290.577018, 640.383094) |
| capacity_halns | 71.000 | 2328.349043 | 646.873703 | 238.001 | 1523.0 | (69, 2299.505134, 640.225982) |

## Paired lexicographic outcome

Capacity-aware HALNS wins 1 seed(s), A2 wins 2, and 0 tie(s).

| Seed | Winner | Δ late tasks | Δ lateness (min) | Δ distance (km) | Δ runtime (s) | Δ iterations |
|---:|---|---:|---:|---:|---:|---:|
| 2026080500 | capacity_halns | -2 | -114.377942 | -10.025063 | 0.000 | -409 |
| 2026080501 | a2 | 3 | 98.032593 | 10.098046 | 0.003 | -378 |
| 2026080502 | a2 | 4 | 48.255636 | 20.101887 | -0.000 | -271 |

Deltas are capacity-aware HALNS minus A2. Means are descriptive only; paired winners use the strict objective tuple.
