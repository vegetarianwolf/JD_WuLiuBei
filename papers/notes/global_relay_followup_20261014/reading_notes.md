# 全局搜索后续：把局部改进放在合适的时间

访问日期：2026-10-14，Asia/Shanghai。使用用户指定的 `paper-lookup` 技能；这是定向检索，非穷尽综述。原始 API 响应、请求参数和访问范围见 [provenance.json](provenance.json)。本笔记不报告新算法测量结果。

## 先更正上一轮诊断

本地证据来自 `algorithm/results/global_relay_trials_20261014/runs.json`，SHA256 `3a16344d89232b2547d43953ff347408216ac06e6310abdef31500ad37b86c21`。逐对复算保存在 [previous_result_diagnostics.json](previous_result_diagnostics.json)。采用项目 `run_optimization_experiments.score` 的严格词典序 `(late_count, total_lateness_min, distance_km)`，没有近似容差。

- `global` 对同预算 `baseline` 为 **5 胜、0 平、7 负**，12 对逾期任务数差之和为 −10；逾期数总和改善不能代替配对稳定性门槛。
- `downstream` 对共享的 `global` 为 **2 胜、1 平、3 负**。旧说明把 6 对中除 2 胜外的结果都写成负，漏掉了完全相等的一对。平局是原点中继场景、种子 `2026102411`，两版原始三元组均为 `(71, 2277.2406003144697, 641.8729680494072)`；不是四舍五入或容差造成的平局。旧 `decisions.json` 只记录 `wins=2,pairs=6`，没有给出错误的平负拆分，故不修改冻结决策或原始测量。
- 六对的初始路线指纹相同，但 **5/6 的 DIRECT 预热得分已经不同**。因此终局数据可用于整体版本对比，不能将差异纯归因于下游中继排序；这六对也不能当成新增加的独立 `global` 样本。
- 全局候选的迭代次数相对基线为 6 对减少、1 对相同、5 对增加。现有证据不支持“局部搜索一概拖慢”这个单一解释。尾段与起点算子接受了局部改善，但它们同时改变后续搜索轨迹、时间分配和接力起点。非独占 CPU 环境也限制速度归因。

## 三篇最相关的原始来源

### FILO：分层调用与局部作用范围

Luca Accorsi、Daniele Vigo (2021). *A Fast and Scalable Heuristic for the Solution of Large-Scale Capacitated Vehicle Routing Problems*. Transportation Science 55(4):832–856. [DOI](https://doi.org/10.1287/trsc.2021.1059)。

**阅读范围：全文可访问，实际查读 §2.2.1–2.2.4 和 §2.3 开头；不是只看摘要。** Crossref 元数据与摘要、大学研究门户的论文 PDF 相互核验。重点是分层局部搜索：便宜邻域达到局部最优后才检查更贵层；成功后回到便宜层。Selective Vertex Caching 将近期被改变的位置作为检查起点，降低对未变化区域的重复工作，同时允许搜索影响向外扩展。静态移动描述符帮助组织检查与避免重复。本文针对 CVRP；其距离增量性质不能直接套到本项目取送先后约束、中继依赖和逾期词典序。

**本项目推论：** 当前固定每若干次迭代插入昂贵全局重排，可能在廉价 ALNS 尚有收益时过早花费预算。最低成本的可检验改动是把已有全局算子移至最后一段有界后处理，保留已经验证的最好解，并统计是否实际接受改进；下一步才考虑“停滞 + 路线签名变化”触发。这是从分层预算思想导出的工程假设，论文没有证明本项目最后一次调用或某个时间比例最优。

### ILS：搜索引擎、扰动和接受规则需要分别评价

Helena Ramalhinho Dias Lourenço、Olivier C. Martin、Thomas Stutzle (2001). *Iterated Local Search*. [DOI](https://doi.org/10.2139/ssrn.273397)。

**阅读范围：Crossref 收录的作者摘要及元数据；没有获取该文全文。** 摘要将 ILS 表述为在给定局部优化引擎产生的局部最优解集合上进行有偏采样，并指出表现取决于局部搜索、扰动和接受准则的选择。检索还找到 2010 年 *Iterated Local Search: Framework and Applications*（DOI `10.1007/978-1-4419-1665-5_12`），但没有该章摘要；不把 2001 年摘要假称为 2010 年章节全文。

**本项目推论：** 上轮局部改善次数不能证明加入该算子后整套算法变好。当前应分开测试“末端后处理”和“DIRECT 预热由现有比例提高到 95%”，避免同时更换接受规则。一次末端优化只是后处理，并非实现了完整 ILS。若后续研究 late acceptance，应独立成一条实验臂，仍以严格三元组保留最优可行解；本轮不继续叠加该机制。

### SISR：有边界的相邻任务破坏重建，保留为后续方向

Jan Christiaens、Greet Vanden Berghe (2020). *Slack Induction by String Removals for Vehicle Routing Problems*. Transportation Science 54(2):417–433. [DOI](https://doi.org/10.1287/trsc.2019.0914)。

**阅读范围：Crossref 的论文摘要与元数据；没有全文。** 摘要描述相邻串移除所提供的空间余量、带跳过机制的贪心重插，以及独立的车队数量优化组件；列举了取送和时间窗变体。本项目无人机数量固定，不照搬其车辆数目标或相应接受准则。

**本项目推论：** 如果末端优化无效，优先对逾期临界任务附近少量完整取送请求做跨机重插或交换，胜过立即扩大整段全局搜索。所有候选仍须完整时序评价；不能拆开请求、把容量可行误当时序可行，或假设 CVRP 的局部距离缓存足以判定中继可行性。这是待测方向，未在当前测量中实现。

## 下一轮怎样使结果可解释

1. 保持基线与候选端到端预算一致；构造、DIRECT 优化、中继、末端优化和最终检查都计入预算。用末端保留时间换取局部搜索，会减少前面的探索时间，因此只能保证接受后的解不劣于该候选自己的前态，不能保证击败用满时间的独立基线。
2. 两条实验臂分别测试末端后处理，以及在其上增加 95% DIRECT 预热与播种 checkpoint 的组合。后者同时包含预算变化和截止前已核验播种解保留，不能纯归因于 95% 比例；95% 是项目实验参数，不是上述论文给出的推荐数值。末端方案不改主循环随机数调用和奖励规则，但预算截断点可能不同；实际轨迹前缀是否相同需检查运行记录，不推断最终路线或 DIRECT 预热分数相同。
3. 延用完整任务规模、多场景、多配对种子的中等范围筛查；逐种子列出胜、平、负及逾期任务数、航程、用时，并把总逾期分钟标为项目次级目标。符合冻结门槛后才扩展验证。
4. 末端动作遇到截止时刻、无可行改进或内部失败时，保留截止前已验证的 incumbent。记录前后分数、调用次数、尝试次数、接受次数、实际耗时；这些诊断用于理解收益来源，不替代最终考核指标。

已有路线池/集合划分文献见上一轮笔记，不在此重复检索。更换 route pool、接受规则、跨机交换和预算比例会同时改变多个因素，因此将这些方向留作独立后续实验。

## 检索边界与软件引用

本轮共 7 次有界终端 HTTP 请求：4 次 Crossref、1 次 OpenAlex、2 次 PDF 获取。FILO 的博洛尼亚站点返回 HTTP 403 HTML，另一个大学门户返回真实 PDF。LAHC 的 OpenAlex 记录没有 `abstract_inverted_index`，不能视为摘要获取成功；其索引年 2016 与期刊卷期年 2017 分开记录。LAHC 在范围收窄后未作为本轮三篇主要依据。完整论文仅临时阅读，仓库保留链接、散列和阅读范围，未加入 PDF。

使用的 `paper-lookup` 技能属于 Scientific Agent Skills。沿用同日上一轮已经核验的当前文献记录：Timothy Kassis、Vinayak Agarwal、Yuhuan He、Darshil Patel、Aubrey M. Brueckner (2026), *Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents*, [arXiv:2609.00065](https://arxiv.org/abs/2609.00065)。核验时最新为 v2、未列正式期刊出版 DOI；本轮不重复声称重新获取。旧记录的文件指纹见本轮 provenance。
