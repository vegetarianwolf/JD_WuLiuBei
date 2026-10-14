# 全局路线调整与选择性中继：本轮文献依据

检索日期：2026-10-14（Asia/Shanghai）。使用用户指定的 [paper-lookup 技能](/Users/vegetarianwolf/.agent-skills/src/scientific-agent-skills/skills/paper-lookup/SKILL.md)，先读 arXiv、Crossref、OpenAlex reference，再通过 curl 做有界 API 查询；补充浏览原始论文和出版社页面。原始元数据、实际参数、失败、计数和指纹见 [provenance.json](provenance.json)。这是定向方法检索，非系统综述。

本轮更适合优先尝试低成本的全局 DIRECT 调整，再给选择性中继保留有限预算。下述文献支持搜索结构和可行性原则，不能保证本实例改善。尤其不能用中继数量作为成功指标：题目直接关注按时完成任务数、总航程及求解时长；总逾期分钟是项目现行次级目标。

## 五篇与本轮决策相关的原始论文

| 文献 | 本轮证据层级 | 可借鉴内容和边界 |
|---|---|---|
| Ropke, S., & Pisinger, D. (2006). *An Adaptive Large Neighborhood Search Heuristic for the Pickup and Delivery Problem with Time Windows*. Transportation Science, 40(4), 455–472. [DOI](https://doi.org/10.1287/trsc.1050.0135) | Crossref 元数据与摘要；未读全文 | 摘要支持多种破坏/修复启发式按历史表现竞争。本仓库已有 ALNS，不能把这一框架本身称为本次新方法。没有从摘要推导任何精确插入公式。 |
| Subramanian, A., Uchoa, E., & Ochi, L. S. (2013). *A hybrid algorithm for a class of vehicle routing problems*. Computers & Operations Research, 40(10), 2519–2531. [DOI](https://doi.org/10.1016/j.cor.2013.01.013) | Crossref/OpenAlex 元数据；出版社首屏检索结果中的摘要。PDF 返回 403，未读全文 | 摘要明确以搜索所得路线为列求受限集合划分，并包含开放路径、多仓库等变体。可支持路线池重组方向，不能据此宣称复现其全部算法。中继耦合路线不能简单当作独立可加的列。 |
| Vidal, T. (2022). *Hybrid genetic search for the CVRP: Open-source implementation and SWAP\* neighborhood*. Computers & Operations Research, 140, 105643. [出版 DOI](https://doi.org/10.1016/j.cor.2021.105643) · [原始预印本全文](https://arxiv.org/html/2012.10384) | Crossref 出版信息；读官方 arXiv 全文第 3–5 节，当前 HTML 对应 v2（2021-10-12） | 第 3 节允许两路线交换客户后重新选插入位置；第 4 节用路线修改时间戳避免重复评价；第 5 节讨论同等预算和收敛轨迹。单节点 CVRP 的前三插入位置完备性结论不能直接用于成对取送、截止期及中继等待。 |
| Masson, R., Lehuédé, F., & Péton, O. (2013). *An Adaptive Large Neighborhood Search for the Pickup and Delivery Problem with Transfers*. Transportation Science, 47(3), 344–355. [DOI](https://doi.org/10.1287/trsc.1120.0432) | 本轮重新取得 Crossref 元数据与摘要；未读全文 | 固定中转点换车与 ALNS 插入是物理接力的直接方法背景。论文问题和本题约束不同，摘要中的外部算例收益不用于预测本题收益。 |
| Coltin, B., & Veloso, M. (2014). *Scheduling for Transfers in Pickup and Delivery Problems with Very Large Neighborhood Search*. AAAI. [DOI](https://doi.org/10.1609/aaai.v28i1.9034) · [官方 PDF](https://ojs.aaai.org/index.php/AAAI/article/download/9034/8893) | **复用前批全文阅读**，并非本轮重新下载；前批指纹绑定在 provenance | 前批 PDF 页 4–5 支持已有路线拆分及沿交接关系更新时序。原文同步交接，仓库扩展采用站点暂存型异步交接，不能混为同一模型。 |

## 可落地候选：哪些来自论文，哪些是本项目推导

**1. 整条 DIRECT 路线与预部署起点的全局重新指派。**

这是本项目从“不同起点 + 固定路线”结构推导的廉价邻域，不是上表某篇论文的现成算法。固定每条路线内部顺序和任务集合，分别用所有无人机起点重算完整路线三级得分，得到 8×8 成本矩阵；用子集动态规划选择一一指派，直接比较可加的词典序得分，无须把三级目标挤成未经证明的大权重标量。每条路线仍保留原任务数与装载序列，故不会触发满额 `8×25=200` 的单任务跨机迁移瓶颈。没有任何中继交接时，路线得分独立可加；存在中继时不再成立。只应在 DIRECT 状态或 DIRECT 预热结束处尝试，最终仍用完整评价器复核。

**2. 两条或少量路线一起释放任务，再作完整任务互换/重插。**

SWAP\* 的可借鉴点是“换机后位置可变”，而非固定原位置交换。本题必须以完整取送任务为单位，同时释放至少两条满额路线的任务槽，再重新选择取货和送货位置。候选可由空间接近、截止期临界程度排序，限定路线对和任务数以控制预算。Vidal 的单节点前三位置定理依赖边成本可加；删除两个事件会改变装载状态与多项截止期代价，不能直接当本题安全剪枝。位置短名单若被使用，只能标为启发式限制，不能声称完整枚举。

**3. 收集不同局部最优路线，按覆盖约束重组 DIRECT 解。**

路线池与受限集合划分来自 Subramanian 等的摘要。工程适配需要每个任务恰好覆盖一次、每架无人机至多一条路线、每条路线符合 K 上限，并按对应起点重新评分。有限历史池只能提供受限候选域，求到该池最优也不等于原题全局最优。若短预算中候选重复、池中路线覆盖互相冲突，精确重组可能无收益而浪费时间，应记录池大小、可组合路线数、重组耗时和实际严格改善次数后决定是否扩大。

**4. 中继继续作为高风险订单的选择性改动。**

固定站点插入有 Masson 的摘要依据；从优质 DIRECT 路线拆分并传播交接时序有 Coltin 的前批全文依据。具体迟到风险排序、等待上限、有限候选束及分阶段预算都属于项目适配。所有候选都需独立检查任务覆盖、取送先后、容量、最终责任 K 计数、交接无环和全局到达时间。两路线变化可能影响第三条路线；不能只看本任务提前了多少或局部航程减少了多少。用户图片说明的是“预部署主要改善、中继补充”，这应被当作待检验的历史现象，不能预设新实验结果。

建议本轮以完整 200 任务和大部分主要场景、多个配对种子覆盖为主，先使用中等单次预算；候选与基线相同起点/种子/总预算，按实际完整三级得分逐种子判胜。若新方法只改善总逾期而增加逾期任务数，必须判为退化。图中应将按时任务数、总航程、用时置于主区，总逾期和中继等待等置于补充区。以上是本次实验建议，不宣称全部已实现或已测试。

## 检索返回、排除与可复现性

终端共 14 次 HTTP 尝试（包括初始 DNS 失败、2 次 arXiv 超时、1 次出版社 403），均为小规模定向请求；同一 API 串行，arXiv 请求间隔超过 3 秒。Crossref 五个首屏分别返回 3、3、5、3、2 条记录，模糊 `total-results` 不是相关论文数，未翻页。

首次 arXiv 搜索返回 16 条匹配中的前 6 条，全部是通信/数据中继，已排除；其标题看似有 drone/relay，但不支持包裹接力。OpenAlex 的组合关键词返回 3 条，均非本次直接方法依据。这些有限检索不证明领域没有相关论文。Crossref 另返回 2026 年无人机–自动配送车中转预印本（`10.2139/ssrn.7317759`），但该摘要涉及回仓、多次出车与时变禁飞区，未将其当本题算法依据；同时出现的强化学习导航、综述及多机服务补充文件也未采用。

arXiv 两个标识查询在 30 秒内返回 0 字节，不能记作“无论文”；改用官方网页成功核实技能软件引用并读到 SWAP\* 全文。OpenAlex 将路线池论文标为 OA，但没有摘要；PDF 实际返回 403，故只使用出版社检索结果显示的摘要，未声称读完全文。所读 SWAP\* 全文仅临时用于核查，保留 URL、字节数、SHA-256 和章节位置，不将完整文章重新发布到仓库。

## 软件方法引用

本轮已实时读取 [Scientific Agent Skills 官方记录](https://arxiv.org/abs/2609.00065)，核实题名、五位作者、2026 年和最新 v2（2026-09-02）；页面未列期刊参考或出版社 DOI。已在本报告加入技能要求的软件方法引用：

Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*. [arXiv:2609.00065](https://arxiv.org/abs/2609.00065)。此条是检索流程来源，不是物流算法性能证据。
