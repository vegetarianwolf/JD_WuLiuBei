# 全程联合中继求解器（2026-10-14）

[可视化对照报告](index.html)记录用户选择“更换整套求解器”后的独立实现。`Joint-Relay-VNS` 从迭代一开始联合调整分机、直送/接力模式、既定网络中的站点与访问顺序，不调用 `solve_alns` 或 `solve_relay_staged`，没有 DIRECT 搜索预热阶段。为公平比较，两种方法使用相同的 regret 构造初解、官方输入、起点、站点、随机种子和总预算。

## 实现和结论

新算法的成对邻域同时移除两项原任务的全部直送或中继腿，枚举两种重插顺序、直送/直送、直送/中继和中继/中继组合。它可在满额 K 下互换最终交付责任；其余邻域进行单任务转运/合并和访问次序修改，停滞时允许经过完整核验的有界扰动。全球完整交接时序评分保留最佳解，未完成或超预算的候选不纳入。

满额 2+2 合成回归中，新方法形成真实跨机接力、交换两项最终交付责任，在全部准时前提下航程 `62.198→55.774 km`。这证明功能可用，不是官方数据收益证据。

首批共 12 次运行：4 次 8 任务、80 轮的合法性烟测，8 次官方 200 任务的 10 秒配对。官方四个配对均退化：主场景逾期 `72→102`、`74→98`；原点次场景 `82→105`、`79→105`。因此未进入预定的 30 秒三种子验证。首批累计求解 72.334 秒，全部结构、交接时序、得分和预算独立核验通过。

用户随后明确选择追加少量 30 秒配对，观察新算法是否只是启动慢。追加单独存于 [extension30](../extension30/README.md)：主场景两个新种子逾期 `63→98`、`72→97`，仍为 0 胜 2 负。源码、参数和初解构造与首批一致，两批合计 16 次完整运行、188.391 秒（3.14 分钟），共用 540 秒上限并保留 30 秒余量。结果不支持在所测预算和实例上替换当前默认。新方法保留为 CLI 研究入口，默认仍为 ALNS。

“启动慢”诊断须区别构造和持续搜索成本：两算法共同构造约 0.5–0.65 秒，新算法立即开始搜索。它的两任务组合、候选束与边界任务选择仍有限；持续候选生成/全局核验和邻域覆盖不足是后续假设，不能仅凭本批计数断定唯一退化原因。不同算法的迭代及缓存调用口径不同，不直接比较其迭代吞吐。

## 使用新求解器

```bash
PYTHONPATH=algorithm/src .venv/bin/python -m uav_dispatch solve \
  --input 'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv' \
  --method joint-relay --tasks 200 --drones 8 --max-tasks 25 \
  --relay-count 4 --drone-homes stations --candidate-limit 12 \
  --iterations 10000000 --time-limit 30 --seed 2026101931 \
  --output /private/tmp/joint-relay-solution.json
```

Python API：`from uav_dispatch.joint_relay import JointRelayConfig, solve_joint_relay`。不传初解时提供简单可行构造；本批基准由运行器传入共同冻结构造，故上方 CLI 命令用于功能复现而非逐位再现墙钟成绩。

## 口径与复算

用户选择继续使用“逾期任务数 → 总逾期分钟 → 航程”的三级词典序以隔离算法变化；报告分别展示原题的按时数、航程、时间和项目次级总逾期。沿用最终配送责任占 K、站点存储型异步交接的论文扩展口径。站点坐标和预部署固定，联合搜索不优化站点坐标，也不证明全局最优。

首批源码基线为 `ac625e5a38ec2b92f239df2d5ca1017cbeb3ad9b`，新源码 ZIP 比基线新增 `joint_relay.py` 与 CLI 接口，其他求解器模块保持相同。内部插入候选参数旧版48、新版12，构造参数双方均48。新模块约2毫秒的导入被保守计入候选端到端时间；进程启动和主模块导入不计入。计时包含构造、搜索、完整核验和完整解写出，预留1秒收尾。

追加专用审计器会以只读方式同时复核首批和追加，无需再次求解：

```bash
PYTHONPATH=algorithm/src .venv/bin/python -m algorithm.experiments.audit_joint_relay_extension --output-dir algorithm/results/joint_relay_trials_20261014/extension30
```

追加绑定首批原审计的不可变哈希；不要改写父批的 `validation.json` 收据。独立首批审计命令如需重跑，应在复制的目录中执行以保留追加所绑定的原始收据。报告重建：

```bash
.venv/bin/python -m algorithm.experiments.render_joint_relay_trials_report \
  --output-dir algorithm/results/joint_relay_trials_20261014/batch1 \
  --extension-dir algorithm/results/joint_relay_trials_20261014/extension30
```

`index.initial.html` 保留追加前首批页面；历史中继微调和正式四策略独立展示，不混算不同预算、源码和种子的改善率。源码归档、输入/解哈希、初解指纹、每次完整解、轨迹、阶段决策和独立 DAG 核验均保存。本批文献沿用 [已核实的方法依据](../../../../papers/notes/joint_relay_20261014/reading_notes.md)，不是对 AAAI VLNS-T 的完整复现。
