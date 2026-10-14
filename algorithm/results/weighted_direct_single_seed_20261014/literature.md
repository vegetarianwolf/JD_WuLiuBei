# 加权直送单种子试验：文献检索与方法建议

访问日期：2026-10-14，Asia/Shanghai。使用用户指定的 [paper-lookup skill](/Users/vegetarianwolf/.agent-skills/src/scientific-agent-skills/skills/paper-lookup/SKILL.md) v2.1，先实际检索 Google Scholar，再以 Crossref DOI 元数据和出版社摘要核验。检索范围是有界的主题查找，选出四篇相关论文；不宣称系统综述或全量检索。可机读的端点、响应校验和来源层级保存在 [literature.json](literature.json)。

## 题目与历史思路

只读了根目录题目 DOCX、algorithm/README.md、早期文献研究报告、2026-08-15 优化记录、objective_trials_20261013 及其 compact_trial、relay_trials_20261013/problem_contract.md，并对照当前 Score/ALNS 的评价结构。

原题集群实例为 200 项任务、8 架机、每机累计不超过25项、同时载荷不超过2件、速度0.9 km/min；每项必须同机先取后送，所有任务完成，路线开放且不计末单后的返航。因 N=M×K，最终每机必须恰好承接25项。题目直接考核的是尽可能保障更多任务按时完成、配送总航程及可控求解时间。总逾期分钟是项目增加的次级约定，原题没有规定将其置于航程前。

历史截止期边界算子、第一目标学习奖励和缩小破坏邻域均有短测试退化；不能因为迭代较多或平均航程下降就称整体改善。本轮用户限定只比较当前方案与固定加权候选、直送、一个种子、各240秒；以下尚未执行的方法只作为后续建议，不叠加到本轮，也不把旧实验数字作为本轮基线。

## Google Scholar 的实际检索

使用 Codex In-app Browser 后台访问成功，首屏可见：

| 查询 | 可见约总数 | 本次选读结果 |
| --- | ---: | --- |
| [drone pickup delivery soft time windows multi objective weighted sum](https://scholar.google.com/scholar?q=drone+pickup+delivery+soft+time+windows+multi+objective+weighted+sum) | 17,900 | Hong 等多无人机多目标取送；Luo 等灵活时间窗与 Pareto 局部搜索 |
| [“weighted sum method” “new insights”](https://scholar.google.com/scholar?hl=zh-CN&as_sdt=0%2C5&q=%22weighted+sum+method%22+%22new+insights%22&btnG=) | 2,100 | Marler 与 Arora，第一条结果，Springer DOI |
| [“adaptive large neighborhood search” “pickup and delivery” Ropke Pisinger](https://scholar.google.com/scholar?hl=zh-CN&as_sdt=0%2C5&q=%22adaptive+large+neighborhood+search%22+%22pickup+and+delivery%22+Ropke+Pisinger&btnG=) | 2,730 | Ropke 与 Pisinger，第一条结果，INFORMS DOI |

这些数字是 Google Scholar 显示的近似命中总数，不是本次获取的论文条数；只检查首个结果页、选择四篇，不分页。没有把 Semantic Scholar 称作 Google Scholar，也不把被引次数当算法优劣证据。

访问中，web.open 无法访问 Scholar，沙箱 curl 返回 DNS 失败（退出6，HTTP000）；随后浏览器访问成功。Crossref 使用只读网络 curl 恢复，五次串行调用核验四篇精确 DOI（Marler 连通性检查后在四篇核验中再次读取），均 HTTP200 且 status=ok、message-type=work、DOI精确匹配。未使用 API key 或邮件参数。

## 文献与可迁移方法

| 文献 | 已核验的内容与本题关系 | 本项目可落地的推断 |
| --- | --- | --- |
| Marler, R. T. & Arora, J. S. (2010). *The weighted sum method for multi-objective optimization: new insights*. Structural and Multidisciplinary Optimization, 41(6), 853–862. [DOI](https://doi.org/10.1007/s00158-009-0460-7) | Crossref 元数据与 [Springer 摘要](https://link.springer.com/article/10.1007/s00158-009-0460-7)均核验。摘要讨论权重、偏好与目标函数值的关系及盲目使用权重的不足；未读取付费全文。网上首发2009，期刊卷页2010。 | 先冻结只依赖输入的尺度，再固定有限权重；给出“一项逾期对应多少分钟/公里”的交换关系。具体数值由本项目提出，不是论文给出的最佳参数。 |
| Ropke, S. & Pisinger, D. (2006). *An Adaptive Large Neighborhood Search Heuristic for the Pickup and Delivery Problem with Time Windows*. Transportation Science, 40(4), 455–472. [DOI](https://doi.org/10.1287/trsc.1050.0135) | Crossref 元数据/摘要与 [INFORMS 摘要](https://pubsonline.informs.org/doi/10.1287/trsc.1050.0135)均核验。先取后送、同路线、容量与时间窗结构相近；子启发式依据历史表现竞争。 | 沿用已有破坏/修复框架，只改变评分偏好，有利于解释本轮差异。插入、接受、best保留和学习奖励要使用一致目标，避免一个层级在优化加权值，另一个仍用词典序。 |
| Hong, F., Wu, G., Wang, Y., Luo, Q., Wang, L. & Shi, J. (2025). *Multi-Objective Multi-Drone Collaborative Routing Problem With Heterogeneous Delivery and Pickup Service*. IEEE Transactions on Intelligent Transportation Systems, 26(5), 6084–6100. [DOI](https://doi.org/10.1109/TITS.2025.3548426) | Crossref 元数据与 [IEEE 摘要](https://ieeexplore.ieee.org/document/10937313/)核验。多无人机、软时间窗、取送优先关系，成本和客户满意度双目标，提出ALNSMO。其单件容量、能耗与智能柜设置不同于本题；未读取全文。 | 说明服务与成本的权衡值得研究。未来可保留有限非支配候选，减少单一偏好的遗漏；不能移植其容量或能源约束，也不能凭摘要推断其最优权重。 |
| Luo, Q., Wu, G., Ji, B., Wang, L. & Suganthan, P. N. (2022). *Hybrid Multi-Objective Optimization Approach With Pareto Local Search for Collaborative Truck-Drone Routing Problems Considering Flexible Time Windows*. IEEE Transactions on Intelligent Transportation Systems, 23(8), 13011–13025. [DOI](https://doi.org/10.1109/TITS.2021.3119080) | Crossref 作者、题名、卷页核验；Scholar 标2021而Crossref期刊卷页为2022。题名可确认灵活时间窗与Pareto局部搜索。IEEE页面未返回可读摘要，未取得全文，不能据此声称复现具体算子。 | 本项目可研究零载荷切点间的等任务数尾段交换，配合有限非支配存档；容量2、同机取送和每机25项必须完整复算。当前已有相关默认关闭开关，其历史退化须保留；本轮不同时启用。 |

表中最后一列均为结合仓库结构的工程推断，不是文献在本官方实例上已证明的收益。

## 本轮冻结的加权候选

本轮正式实验已在运行前冻结 seed=2026101451、两版本各240秒、直送场景，采用：

```text
F = 0.50 × 逾期数 / 200
  + 0.25 × 总逾期分钟 / 4464.183540734935
  + 0.25 × 总航程 / 775.7731679286865
```

这些固定尺度的来源以实验 manifest 为准；文献没有给出这些数字，也没有用本轮终值事后选择。每多一项逾期可单独由44.6418354 min总逾期改善或7.75773168 km航程改善抵消，同时变化时按总和计算。所有加权候选的best保留包括末端强化都按加权目标，因此按时数可能变差，必须在报告中直接呈现。实验只跑这一组，不追加权重网格、更多种子或其他算法。

## 后续固定尺度建议

一种只依赖输入的可解释示例为：

```text
F = 0.60 × 逾期数 / N
  + 0.25 × 总逾期分钟 / (N × 截止期中位数)
  + 0.15 × 总航程 / Σ(各任务取件→送达欧氏距离)
```

只读官方 CSV 得到 N=200、截止期中位数36.45 min、第二项尺度7290 min、航程参考尺度754.2129900621 km。输入 SHA-256 为 `386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`。最后这个距离和用于尺度归一化；容量2允许共享航段，它不是配送总航程的下界。

该示例下，减少一项逾期与减少87.48 min总逾期或15.08426 km航程的得分变化相同；同时变化时按线性总和计算。参数是运行前建议，未参考本轮结果调优，实际执行公式以本批 manifest 为准。有限权重允许真实取舍；若用能压倒其他项任何变化的巨大权重，实际上又回到词典序，不回答本次探索问题。

实现时应检查：

1. 原始三元组始终完整计算和保存；hard constraints 独立检查。
2. 加权候选的插入比较、当前解接受、历史best保留、相邻/末端强化采用同一公式。若只改变退火概率而最后按词典序返回，应把它称作“加权引导词典序搜索”。
3. 尺度全程固定；不要用运行中最好/最差值动态改尺度，否则相同路线的分数会随历史漂移。
4. 温度与新的标量差值使用相同单位；修复排序和模拟退火差值改变后，不能把吞吐或轨迹差异只归因于最终择优。
5. HTML 先比较按时数/率、总航程、端到端时间，另展示总逾期分钟及两方案在相同权重下的分解总分。标量较低不自动表示题目的服务优先考核获胜。
6. 一个种子只说明此实例、此预算、此随机轨迹，不能证明统计显著性或跨实例稳定优势；本轮不扩大测试，也不重跑择优。

## 检索重现与限制

元数据端点为 `GET https://api.crossref.org/works/{URL编码后的DOI}`，curl 参数 `-sS -L --max-time 30 -w HTTP_STATUS`，四篇核验调用按顺序执行且间隔0.3秒；完整端点见JSON。精确 DOI 查找不涉及分页。Scholar查询、访问方式、可见首屏结果、网络失败与恢复路径都在JSON记录。四篇中未取得可复用全文；本说明分别按出版社摘要或元数据注明证据层级，不能把缺少摘要/全文理解为论文没有相关方法。

本次检索流程使用的软件参考亦已核验并加入：Kassis, T., Agarwal, V., He, Y., Patel, D. & Brueckner, A. M. (2026). *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*. [arXiv:2609.00065](https://arxiv.org/abs/2609.00065), [DOI](https://doi.org/10.48550/arXiv.2609.00065)。访问时最新为2026-09-02修订的v2，未显示期刊或出版社DOI；引用使用不带版本后缀的稳定标识。此软件参考是检索流程来源，不作为物流算法效果证据。
