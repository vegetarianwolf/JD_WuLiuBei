# 中继接力文献核查与本题适配

检索日期：2026-10-13（Asia/Shanghai）。使用用户指定的 [paper-lookup 技能](/Users/vegetarianwolf/.agent-skills/src/scientific-agent-skills/skills/paper-lookup/SKILL.md)。这是定向、有界检索，未穷尽文献；原始首屏元数据、限流响应及指纹保存在本目录，具体参数见 `provenance.json`。

## 先明确原题及现有证据

已读取原题 Word 文档：官方直接考核尽可能多订单按时、完成全部任务的总航程，以及分钟级求解时长；不计末单后返航，载荷上限 2，官方附件为 200 项任务、8 架无人机、每机累计最多 25 项，速度 0.9 km/min。总逾期分钟是项目采用的次级目标，不能写成题目单列指标。

原题没有详细规定中继仓储、站点配置、交接耗时及跨机任务计数，因此中继是项目研究扩展。当前实现采用站点存储型接力：包裹先卸货，接手机随后取货，约束为 `drop_time <= pickup_time`；`K` 按最终交付责任计算。引用同步交接文献时，应说明该计数与存储假设，不把扩展模型自动等同于题目原始措辞。

历史 `RELAY_ADJUSTED_240S_REPORT.md` 中，扩大至 4 站/每任务 2 候选、增加播种和精化后，中继对直送配对胜率为 2:1，逾期数均值均为 66；总逾期少 3.11 min、里程少 0.88 km，但每秒迭代数由 27.70 降至 22.97。被采用中继约 4.33 项/解，等待仅均值 0.50 min。该证据支持选择性精化，不能推导“更多中继必然更好”或单独以中继数量衡量成败。

## 实际核实的论文

| 文献 | 已取得证据 | 对本题的用途与边界 |
|---|---|---|
| Masson, R., Lehuédé, F., & Péton, O. (2013). *An Adaptive Large Neighborhood Search for the Pickup and Delivery Problem with Transfers*. Transportation Science. [DOI](https://doi.org/10.1287/trsc.1120.0432) | Crossref 题名、作者、年份、摘要匹配；未读全文 | 摘要明确研究请求在固定中转点换车，以及中转插入启发式嵌入 ALNS；只能支持方法方向，不能据此复现插入公式或借用其收益数字。 |
| Coltin, B., & Veloso, M. (2014). *Scheduling for Transfers in Pickup and Delivery Problems with Very Large Neighborhood Search*. AAAI. [DOI](https://doi.org/10.1609/aaai.v28i1.9034) · [官方 PDF](https://ojs.aaai.org/index.php/AAAI/article/download/9034/8893) | Crossref 元数据及 AAAI 官方 7 页全文；重点读 PDF 页 4–5（出版页 2253–2254） | 原文有先直送再拆分路径、从一开始插入一段转运两类机制；在交接前后传播时序、检查死锁。其即时交接要求双方同时到场，当前仓储型接力的可行域与之不同。 |
| Masson, R., Lehuédé, F., & Péton, O. (2013). *Efficient feasibility testing for request insertion in the pickup and delivery problem with transfers*. Operations Research Letters. [DOI](https://doi.org/10.1016/j.orl.2013.01.007) | Crossref 题名、作者、年份匹配；未取得摘要或全文 | 是后续快速可行性检查的阅读线索；本轮不能归因任何具体剪枝公式。 |
| Wen, X., Wu, G., Liu, J., & Ong, Y.-S. (2025). *Transfer Optimization for Heterogeneous Drone Delivery and Pickup Problem*. IEEE TETCI. [DOI](https://doi.org/10.1109/TETCI.2024.3406689) | Crossref 元数据与仓库本地全文页 347–349 匹配 | **Transfer 指跨实例知识迁移，不是包裹中继。** 模型是母机携带小机的异构两级物流；不能将题名中的 Transfer 当作本次物理接力的直接依据。 |

还读了仓库 Zhu 等（2024）[locker-based drone delivery 论文](https://doi.org/10.1016/j.trb.2024.102950)第 1–3 页及结论。其重点为载荷一件的无人机在 locker 之间运输、空载调位与停车资源，不能把其规模和求解速度搬到容量二、软截止期、中继 ALNS。本轮不采用其性能数字。

## 全文能支持什么

Coltin 与 Veloso 的 PDF 页 4 将已有取送路径拆分为两架车辆完成，分别尝试改变取件侧和送达侧；还说明先直送后拆分比为四个事件、所有车辆对同时枚举的完整转运插入便宜。每个拟议插入都检查时序和交接环，最后按目标判优。

PDF 页 5 的 `earliest_times` 更新会沿交接关系影响其他车辆，`fslack` 递归计算后续约束能承受的推迟，并吸收已有等待。这支持“交接评价必须覆盖关联路线和下游”的实现原则。该文还含最大运输时长、最大路线时长等本题没有的约束，其完整 `update_times` 不能逐字移植；当前题目只有送达软截止期时，最早到达传播已经足够计算成绩。

本轮基线固定为 `bca2fbfd6f9aca9818005213cf2a03a696ada0f0`。基线已有按任务 ID 顺序的构造后播种、修复中的风险排序、迟到中继优先精化、交接等待 surrogate、站点候选多样性及两路线变动后的全局时间增量传播。它没有本轮新增的**全局风险优先播种**：该开关用完整 startup 时序对播种顺序重新排序，区别于修复排序。基线的全局 leg beam 仍可能被同一架 UAV 的多个位置占满，本轮**跨机候选多样性**为不同 UAV 保留候选，区别于已有的站点多样性。阅读工作树时可见这些新开关，不能据此反推为基线已有；另一本轮新开关是播种预算耗尽时保留已完成严格改善。三者应分别以冻结基线进行独立小测。

## 优先考虑的 3 个独立小试验

1. **成对互换最终交付责任。** 在 `8×25=200` 的满额条件下，移除单个任务只释放原路线的一个 K 槽，其他路线仍满额。可同时移除分属两路线的两个 DIRECT 任务，分别尝试中继两腿，让两任务 OUT 责任互换；两条路线最终交付计数不变。这是本项目对 K 瓶颈的推演，非论文现成算子。首先限定少量路线对、少量临近迟到任务及既有站点，完整全局评价后只接受严格词典序改善；失败则保留原解。

2. **把下游准时余量加入中继候选排序。** 现在 surrogate 主要修正该中继订单自身的等待后逾期，没有完整反映接手机后续准时订单变迟到的风险。可用该路线后继原本准时任务的最小 deadline slack，估计一段额外交接等待会影响多少下游订单，对 shortlist 排名进行修正。它只是排名启发式：原有等待会吸收推迟，已有中继可能将影响传给第三条路线，不能把这项近似当作普适安全硬剪枝。仍需现有全局评价确认。

3. **给 DIRECT 起点后的拆分搜索单独小预算。** 以已取得的直送/预部署解为起点，在限定预算内枚举少量 near-deadline 请求的 pickup 侧或 delivery 侧拆分，保留严格改善并立即保存 checkpoint。记录首次改善时间、有效候选率和每次全局评价成本，再决定是否扩大试验。此方向由全文 split-first 思路支持；具体风险排序、预算及 checkpoint 是项目适配。本轮的新风险优先和 checkpoint 开关属于这一方向，基线虽已有播种，却按任务 ID 先后扫描，并在该遍播种超时异常时丢弃此前改善，不能说三者等价。

每个候选应独立与相同起点、相同种子和相同预算的现有算法比较，先小测后扩展。不引用其他问题的实验收益预测本题提升；官方准时任务数、航程、求解时间应优先展示，总逾期注明项目次级目标，中继数、等待、跨机交接和吞吐仅作解释性诊断。

## 检索完整性与软件方法引用

3 个 Crossref 模糊查询首屏分别返回 3、5、3 条记录。`total-results` 是模糊检索池，不能当相关文献数量。筛选保留上表 4 篇确切匹配文献，未翻页。arXiv 的无人机 relay 搜索先返回 HTTP 429，间隔后仅重试一次仍 429；原始均为 `Rate exceeded.`，官方 `arxiv_atom.py` 均以 5 退出，没有取得任何 arXiv 方法文献，不能据此判断该库没有相关论文。首次沙箱内 Crossref/arXiv 请求 DNS 失败，经联网授权后恢复；具体失败也列入来源记录。

按技能要求已实时读取 [Scientific Agent Skills 当前记录](https://arxiv.org/abs/2609.00065)，题名、5 位作者及 2026 年匹配，提交历史最新为 v2（2026-09-02），页面未列期刊参考或出版商 DOI。软件方法引用加入：Kassis, T., Agarwal, V., He, Y., Patel, D., & Brueckner, A. M. (2026). *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*. [arXiv:2609.00065](https://arxiv.org/abs/2609.00065)。对外链接保持无版本后缀。该引用是检索流程来源，不是中继性能证据。

官方 PDF 全文仅临时用于核查，不随本目录重新发布；`provenance.json` 保留 URL、字节数、SHA-256、正文位置和形状校验。仓库既有 PDF 的路径及指纹也已绑定，未复制长段原文。
