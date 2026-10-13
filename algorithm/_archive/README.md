# 归档目录（_archive）

本目录存放**过时/被取代的中间实验结果与历史报告**（2026-08-15 清理时归档）。
这些内容已不再被 `algorithm/README.md`、实验脚本或当前结论引用，但作为研究演进记录保留，
**如确认不再需要可手动删除整个 `_archive` 目录**。

## 内容说明

| 子目录 | 内容 | 归档原因 |
|---|---|---|
| `dynamic_deployment_intermediate/` | 动态部署单 seed 中间版报告 + 结果（`DYNAMIC_DEPLOYMENT_30S_REPORT/RELAY3/RELAY4/AUTO`） | 已被最终多 seed 版 `DYNAMIC_DEPLOYMENT_30S_AUTO_3SEEDS_REPORT.md`（留在 `algorithm/` 根目录）取代 |
| `station_leverage_intermediate/` | 站点杠杆（k-means 起点）系列报告 + 结果（`STATION_LEVERAGE_30S` 及 HOMEAWARE/SITE_MEDOID/SITE_MIDPOINT 变体） | 已被需求驱动的动态部署（`deployment.py`）取代 |
| `phase_20s_intermediate/` | 20 秒分阶段实验报告 + 结果（`PHASE_A_20S`、`PHASE_ABC_20S`） | 中间探索，已被 240s 正式配置结果取代 |
| `orphan_runs/` | 无对应报告的临时运行（`relay_smoke_adjusted`、`predeploy_smoke`、`relay_adjusted_30s`、`relay_adjusted_30s_warmup060`、`station_leverage_30s_site_pickup`） | 冒烟/中间/残缺运行，无文档引用 |
| `historical_reports/` | 无引用的历史报告（`RELAY_REFACTOR_REPORT.md`、`PERF_OPTIMIZATION_REPORT.md`）及性能调参数据（`perf_tuning_data/`：`direct_*.json`、`direct_tuning_60s/240s/240s_mild`） | 内容已被后续优化与 `OPTIMIZATION_UPDATE_2026-08-15.md` 取代 |

## 说明

- 归档采用移动而非删除，全部内容可一键恢复到 `algorithm/results/` 与 `algorithm/` 根目录。
- `algorithm/` 根目录当前保留的报告：`RESULTS_REPORT`、`ALNS_CORE_COMPARISON_REPORT`、
  `NEIGHBORHOOD_ABLATION_REPORT`（README 明确保留的历史 HALNS/ALNS 系列）、
  `RELAY_VS_DIRECT_240S_REPORT`、`OPTIMIZATION_UPDATE_2026-08-15`、`RELAY_ADJUSTED_240S_REPORT`、
  `SCENARIO_COMPARISON_240S_REPORT`、`DYNAMIC_DEPLOYMENT_30S_AUTO_3SEEDS_REPORT`。
