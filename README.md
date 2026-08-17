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

当前主算法、精确 oracle 和最终实验协议集中在
[algorithm/README.md](algorithm/README.md)。主算法严格按“逾期任务数、总逾期
分钟、总航程”三级词典序比较，固定使用 9 个破坏算子和 5 个修复算子；固定
中继站、异步交接、需求驱动预部署与独立完整校验均已实现。

最终实验不复用旧报告：第 5 章运行两任务精确例、小规模 Pareto-DP 对照和
宽松截止期多规模算例；第 6.1 节运行四场景×三个配对种子，其中纯 Direct
为 240 秒、涉及 Relay 或 Station 的场景为 300 秒；第 6.2 节仅对三机制组合
做单种子、三参数各五档的灵敏度分析。旧结果位于 `algorithm/_archive/` 或旧
`algorithm/results/` 下，仅用于审计，不得写入最终结论。
