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

## 最新论文

[低空物流网络中多无人机一对一取送的协同调度](低空物流网络中多无人机一对一取送的协同调度.docx)
是当前作者稿。第 6.2 节已整合四策略、五个配对随机种子的敏感性分析：
55 组均值±样本标准差、图 10—12 和逐种子唯一最优频次表，同时更新中英文摘要、
管理启示和结论。第 5 章及第 6.1 节保留作者稿既有实验，并明确其与新增
300 秒敏感性批次的协议边界；不要用不同批次的同名策略结果交叉计算改善率。

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

最终实验不复用旧报告：第 5 章运行两任务精确例、小规模 Pareto-DP 对照（当前
5、10 任务匹配，8、12 任务未匹配）和
宽松截止期多规模算例；第 6.1 节运行四场景×三个配对种子，Relay 与 Position
各对应一个 240 秒模块，四场景预算依次为 240/480/480/720 秒；第 6.2 节对
四种策略统一采用独立 300 秒总预算，采用 `2026081701` 至 `2026081705`
五个配对种子，对无人机数和截止期倍率各扫描五档，对使用站点的三种策略另扫描
五档站点数。Pure Direct 的站点数敏感性不适用。共 55 个配置、275 条原始
记录、240 次唯一求解，同策略同种子跨轴复用基准解。图表报告均值与样本标准差
（n=5，ddof=1，非置信区间）；稳定性依据原始逐种子的配对胜平负、端点变化、
单调性及最优档位频次，不按均值三元组评选最优，也不作显著性推断。旧结果
位于 `algorithm/_archive/` 或旧 `algorithm/results/` 下，仅用于审计，不得写入
最终结论。

本次五种子结果表明：在这个实例和所测水平内，四策略随无人机数增加、截止期
放宽的三级得分均在 5/5 个种子中单调不劣；16 架无人机时四策略均无逾期任务。
站点数的影响则不单调，最优站点数与部分配置下的最优策略随种子变化，不能将
单种子的最优配置推广为稳定结论。完整均值、样本 SD 和最小—最大值见
[敏感性汇总表](algorithm/results/final_sensitivity/summary.csv)，逐种子配对结果见
[稳定性记录](algorithm/results/final_sensitivity/stability.json)。
