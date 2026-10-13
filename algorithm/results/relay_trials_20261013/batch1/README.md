# 中继接力小预算实验（2026-10-13）

[离线可视化报告](index.html)比较本轮开始时 main `bca2fbf` 与三个独立中继候选：保留播种改善、跨机腿候选多样性、全局风险优先播种。每组使用相同官方数据、随机种子、初解、起点和端到端预算。此批次独立于此前论文及 2026-10-13 的其他批次。

## 结果与采用决定

跨机候选多样性进入 30 秒验证，主场景“预部署 + 中继”与次场景“原点 + 中继”均为 **1 胜、0 平、2 负**。主场景逐种子逾期数为 `68→68`、`67→68`、`67→67`；首个胜利来自项目次级总逾期时间下降。官方按时任务数没有获益，第二组减少一项。主场景按时率均值 `66.333%→66.167%`，航程均值 `634.098→632.215 km`，总逾期均值 `2137.138→2115.382 min`。不能用后两项均值改善抵消新增逾期，也不能按均值三元组评判配对胜负。

按用户选择的三个验证种子多数获胜门槛，本轮未替换默认。三个研究开关 `enable_relay_seed_checkpoint`、`enable_relay_route_diversity`、`enable_relay_risk_seed` 保持默认关闭。其余两个候选虽满足宽松初筛门槛，但按预先记录的主场景优先排序未选入验证；不能将它们称为已确认失败或已证实改善。

35 次完整运行（4 次固定迭代兼容、16 次初筛、12 次验证、3 次同总预算直送参照），全部合法且预算内，累计求解 586.633 秒（9.78 分钟）。最长单次预算 30 秒，没有启动长测或敏感性矩阵。最终完整结构复算和独立字典式 Kahn 调度器核对全部事件时间、最终交付、交接等待、得分与中继统计；112 项相关回归/协议/审计测试通过。

## 用户选择与口径

用户选择轻量验证、以可视化与先前结果对比为主、任一有效配对胜利即可扩大、保留所有退化、三种子多数胜即可设默认。为控制投入，按冻结排序只验证一个候选；两个场景各使用三个预定新种子，出现退化仍跑完，不更换种子或择优重跑。

沿用现有论文的中继扩展：每机 K=25 只统计最终交付责任，前半程承运不占额度；站点可暂存货物，交接为卸货时间不晚于取货时间。原题没有明确这些扩展假设。原题直接考核按时任务数、航程和求解时长；工程增加总逾期分钟为次级目标，报告明确标注。

历史四策略结果单独来自 `final_scenario_comparison`，名义预算 240/480/480/720 秒，不能与本批 10/30 秒、新源码和新种子混算改善率。附图作为思路来源，其数值与冻结历史摘要不同，报告采用可追溯的仓库摘要。30 秒纯直送参照使用本批原始有效行单独统计；预部署与原点起飞的差值不能全部归因于中继。

## 复算与查看

从仓库根目录执行，无需再次求解：

```bash
PYTHONPATH=algorithm/src .venv/bin/python -m algorithm.experiments.audit_relay_trials --output-dir algorithm/results/relay_trials_20261013/batch1
.venv/bin/python -m algorithm.experiments.render_relay_trials_report --output-dir algorithm/results/relay_trials_20261013/batch1
```

直接打开 `index.html` 可离线查看。完整路线、冻结源码 ZIP、运行器和场景构造 helper、输入/解哈希、逐种子轨迹与晋级规则位于本目录。审计绑定 Git 基线、源码归档、执行源码和结果指纹，不采用搜索器缓存验证其自身结果。

## 重放

另建独立检出并把本批 `sources/` 中三个执行脚本恢复到 `algorithm/experiments/`，把 `candidate.zip` 解包至 `algorithm/src/`。基线来自 `bca2fbfd6f9aca9818005213cf2a03a696ada0f0`。候选参数及固定种子见 `manifest.json`。使用新的空目录，禁止覆盖本批：

```bash
.venv/bin/python -m algorithm.experiments.run_relay_trials prepare --output-dir algorithm/results/relay_replay
.venv/bin/python -m algorithm.experiments.run_relay_trials run --output-dir algorithm/results/relay_replay
```

墙钟预算下，机器负载会影响完成迭代数，相同种子不保证得分逐位重复；固定迭代 DIRECT 兼容四版本的决策指纹完全一致。计时从 CSV 读取前至完整校验和解 JSON 写出后，不包含 Python 启动/导入；预处理扣除总预算并预留 1 秒收尾。

文献检索、全文/摘要边界、API 请求参数和失败记录见 [阅读笔记](../../../../papers/notes/relay_20261013/reading_notes.md) 与 [检索来源](../../../../papers/notes/relay_20261013/provenance.json)。按 paper-lookup 要求已加入经在线核实的 Scientific Agent Skills 软件方法引用；其不是中继性能证据。
