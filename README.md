# 物流无人机调度算法项目

本仓库用于小组协作完成“低空经济场景下的物流无人机调度算法”选题，代码统一使用 Python。

## 目录分工

```text
algorithm/                 算法代码设计
├── src/uav_dispatch/      可复用的算法源码
└── tests/                 算法测试
papers/                    论文收集与阅读整理
├── references/            论文原文、书目资料
└── notes/                 阅读笔记、方法对比、文献综述素材
writing/                   论文撰写
├── manuscript/            论文正文与章节草稿
├── figures/               图表源文件与导出图片
└── tables/                表格源文件
data/                      数据与实验结果
├── raw/                   原始数据（不直接修改）
└── processed/             清洗、转换后的数据
```

根目录的命题文档为项目选题依据，请勿覆盖原始文件。

## Python 环境

虚拟环境目录为 `.venv/`，已加入 `.gitignore`，不会提交到仓库。首次使用：

```bash
python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt -e .
```

激活环境后，在项目根目录运行测试：

```bash
python -m pytest algorithm/tests
```

## 协作约定

- 算法代码放在 `algorithm/src/uav_dispatch/`，测试放在 `algorithm/tests/`。
- 论文原文放在 `papers/references/`，阅读笔记放在 `papers/notes/`。
- 论文正文、图表分别放在 `writing/manuscript/`、`writing/figures/` 和 `writing/tables/`。
- 原始数据放入 `data/raw/`，处理后的数据和实验结果放入 `data/processed/`。

## 当前算法成果

容量二词典序 HALNS、ALNS Core 模式、独立校验器、精确小规模 oracle 与可复现实验已集中在 [algorithm/README.md](algorithm/README.md)。所有路线先比较逾期任务数；相同才比较总逾期分钟；仍相同才比较总里程。最新调整配置的 240 秒三种子配对结果（**Direct 1 : Relay 2**，Relay 首次胜出）见 [algorithm/RELAY_ADJUSTED_240S_REPORT.md](algorithm/RELAY_ADJUSTED_240S_REPORT.md)，原始记录位于 `algorithm/results/relay_adjusted_240s/`；旧配置 Direct 3:0 见 [algorithm/RELAY_VS_DIRECT_240S_REPORT.md](algorithm/RELAY_VS_DIRECT_240S_REPORT.md)，30 秒优化更新见 [algorithm/OPTIMIZATION_UPDATE_2026-08-15.md](algorithm/OPTIMIZATION_UPDATE_2026-08-15.md)；原 HALNS、ALNS Core 和邻域消融历史报告仍保留在 `algorithm/` 目录。

**中继点预部署（多机巢）已实现**：8 架无人机默认 1 架驻原点、其余各驻一个中继站（`--drone-homes stations`，`--relay-count 7`），每机从其 home 出发，首段距离按机起点精确计算；30 秒 A/B 显示预部署让 Direct 逾期任务数 -5、Relay -6，总逾期与里程全面改善（数据见 `algorithm/results/predeploy_ab_origin/` 与 `predeploy_ab_stations/`）。`--drone-homes origin` 可回退纯原点模式。

**240 秒五场景同种子配对对比**（纯 Direct / 纯 Relay / Direct+Relay / Direct+起点不同 / Direct+Relay+起点不同，同一 seed 各跑 240s）见 [algorithm/SCENARIO_COMPARISON_240S_REPORT.md](algorithm/SCENARIO_COMPARISON_240S_REPORT.md)，原始记录位于 `algorithm/results/scenario_comparison_240s/`。要点：预部署是收益最大的单一杠杆（Direct 逾期 -4、总逾期约 -156 min、里程约 -15 km）；纯 Relay（warmup=0）在 240s 内不可行（迭代锐减、词典序最差）；分阶段 80/20 是唯一能发挥中继价值的结构。复现命令：

```bash
PYTHONPATH=algorithm/src python algorithm/experiments/run_scenario_comparison.py \
  --output-dir algorithm/results/scenario_comparison_240s \
  --report algorithm/SCENARIO_COMPARISON_240S_REPORT.md \
  --wall-seed-count 1 --seed-base 2026080500 \
  --wall-time-limit 240 --wall-safety-margin 2
```

已跑结果可用 `--render-only` 直接重渲染报告，无需重跑。
