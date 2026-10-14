# 固定站接力的联合救期窗口：下一步设计 v1

2026-10-15。**仅设计；未实现本方法，未运行新优化器，未取得官方改善证据。** 本文只读现有源码、022/025/027/028 保存结果，并用标准库对 027 已保存时钟作分类。旧源码、审计、参数和实验记录不改。现有证据没有完成“中继远胜强 DIRECT”的目标，也没有保证当前数据一定存在此类优势。

## 已做过什么，真正未覆盖什么

A/B 分别把 i/j 交到同一 H、C 接两件共同配送、C 的 k1/k2 补偿给 A/B 的非对称四任务结构，已经在 016–025 使用。它不是本设计的新结构。早入站、早空载接收、完整 C 删单 N/L 信用、移动点 deadline 几何也已经尝试，不能重新命名后当成新方法。

已核实的有限覆盖如下（行号按当前冻结原文件）：

| 位置 | 已有规则 | 尚未做的联合判断 |
|---|---|---|
| `full_score_repool_selector.py:135–158` | 各任务的异 owner 送达点近邻 3；receiver 按目的地 affinity 取前 2；先保留 `max_groups*2` 个基础 family；priority 含原 late/L 风险信用 | 这些前置门之前，不用 H、实际 IN drop、C 空载抵 H 时刻判断 i/j 能否按时 |
| 同文件 `160–212` | 补偿候选取 geometry6 ∪ 完整 C 单删信用6；distinct k1/k2 用真实 C 联合删2分数加两源实际 DIRECT 插入 delta 排序 | 完整删单信用修复了排名，未改变上述基础 family 前门 |
| `source_frontier_cache.py:199–219` | 原物理 best、最早 drop、最早 pickup、时间/事件 bins；最终通常保留 4 个真实 IN 选项 | 保早时间代表，不按接收任务截止期与 C 窗口联合筛选 |
| `joint_mobile_layouts.py:289–344` | 原 receiver best、最早空载 C prefix、若干 clock bins；`_diverse` 又保站点与移动几何代表 | receiver 先独立生成、排名，随后才与 IN 选项做组合；local 排名不计对方真实到货等待 |
| `six_task_route_costs.py:201–253` | gap4 为“最早零载 gap + 按真实插入航程增量排序的其余 gap”；IN 候选保真实局部 best 与最早 drop | **不是只枚举路线尾部**；但未以联合救期窗口建立 task family |
| `mobile_deadline_geometry.py:_targets`、`mobile_repooling_refinement.py:_spec` | 已有 `eligible_deadline_rescue`、真实 suffix deadline 约束和 ellipse seeds | 作用于此前已选中的移动宏/后缀，不是固定 H 基础 family 前的 task/source/C-gap join |

因此唯一明确的新搜索假设是：**在旧目的地近邻/receiver affinity/基础 family 截断之前，按固定 H 的任务、源机真实可行 IN 窗口和 C 真实空载 gap 联合生成 family；保留有实际救期机会的组合，而非只独立保留“早”代表。** 不新增站、不改初始位置、不再扩原尾段池、不把代理排名当 J。

## 证据支持的范围

022 的最佳加权 RELAY raw 为 N59/L1833.269755/D588.587118/J0.439843039，输入 J0.434223725。任务68/147都仍迟到；到 H 的时刻约64.37/72.37，C 在72.24附近接货。原六个受损 native 的时钟、空/单载绕行和末段交付已见 `algorithm/results/proactive_relay_diagnostic_20261014/batch022_raw_clock_diagnosis_v1.md`。025 换成只 pin 两个接力任务后，RELAY仍未超过输入；不能把“接收机更早”单独当成充分解释。已核实源码覆盖的交叉检查另见 `fixed_h_rescue_selector_review_v1.md`（SHA256 `d854c57da994e0a86355c7bb5e3f4ec4754f6ad82a07a6d8620d4971ab95af9e`）。

027/028 最佳加权六任务 RELAY raw 均为 N59/L1814.298547/D583.956063/J0.437288227，仍差于共同输入 J0.434215789。所选六件原来全迟到、方案中仍全迟到；三个源 drop 约77.95/84.05/82.35，全部 OUT 双取在84.05前后完成。详见 `batch027_search_diagnosis_v1.md`。028 两臂约39.21秒，均返回原输入；各 raw 保存512份、各运行两次 polish。这是开发诊断，不是最终多种子性能结论。

不能由最佳加权宏推广到全部候选。只读 `batch027/runs.json` 的 `wide_six_relay` 保存时钟，按原 CSV 的 `delivery > deadline + 1e-9` 分类得到：512份中 N59为412份、N58为100份；102份救回 native任务65，其中2份同时把任务70从准时变迟到；**选中六任务被救回次数为0**。最好 N58 候选 J0.441032689，仍差于输入。少一个迟到件也不保证当前加权 J 更好。

该分类未运行优化器或重新生成路线；源 `batch027/runs.json` SHA256 为 `93ace6b94d9960e4033be3fe1645220c53f9b9aee1770f4b966d68d975fb9d7c`。它只描述保存的候选，不证明所有 IN/OUT 组合、未选任务或全部固定站接力都没有救期机会。

## 固定完整候选下的精确时钟

适用本设计初版的清晰边界：输入为当前完整 pure DIRECT；8机、200任务、C2、最终每机25、等速0.9、原 homes/4站/腿注册表/原截止期保持；直线 Euclidean 飞行，release/service/handling 都为0；只有 i/j 两项转换为 A/B→H→C 单跳中继；其余均 whole DIRECT。固定站允许异步缓存，**不增加移动 receiver-first 或零缓存条件**。

先真正选择四个不同任务 i/j/k1/k2 和三个不同 UAV A/B/C：原 i:A、j:B、k1/k2:C；最终 i/j:C、k1:A、k2:B。删除四项完整原计划后形成 residual 只是候选构造材料，不能当完整200任务方案评分。IN 可在容量允许的位置与一件 native 共载，源机不额外要求零载；C 连续两次 OUT take 前必须空载，取后为2。

对一个**固定完整候选**，包括 k1/k2 的真实位置以及全部 native 次序：

- `a_i`、`a_j`：A/B 全路线从实际 home 累计飞行后，IN_i/IN_j 的真实 drop 时刻。源路线没有 OUT 或其他 relay 等待，因此时钟独立于 C。
- `c`：C 在所选空载 gap 从真实前缀最后节点飞到 H 的抵达时刻；gap0也从该 UAV 原 home 起飞。不是从 H 免费起步。
- C 两个 OUT 正事件相邻、同点、handling0，最后一次 take 时刻精确为
  `T = max(c, a_i, a_j)`。两次 take 之间可有载1等待；总 receiver 等待 `T−c`，必须进入全部后续时钟。源先到时包裹可在原固定站缓存。
- 对 C 第二次 take 之后的每项最终交付 q，令 `ell_q` 为当前真实路线从 H 沿固定 suffix 到该交付的累计飞行时间，**包含期间所有 native 事件和绕行**。此段无其他 relay/release/handling，则
  `delivery_q = T + ell_q` 精确。前缀交付保持其实际 prefix 时钟。

连续双取后紧接送 i、再送 j 的特例：

```text
ell_i = distance(H,D_i)/v
ell_j = [distance(H,D_i)+distance(D_i,D_j)]/v
```

反序同样计算。若 native 插在两次 delivery 中间，必须用真实 `ell`，不能继续套两段直飞式。若以后允许旧 relay、变速、非零 handling/release 或更多交接，本节公式不再直接适用，须重新全局 DAG，而不是给常数补丁。

## 救期窗口、必要式与局部充分条件

官方迟到判断沿用 `delivery > due + epsilon`，`epsilon=1e-9 min`。候选发现可预声明额外安全余量 `sigma`（例如1e-8 min）；最终真实评分不修改 epsilon。令安全按时阈值 `b_q = due_q + epsilon − sigma`。

对固定 C suffix，需要保准时的交付集合 Q（至少包括希望救回的一项原 late，以及不愿损失的原 ontime selected/native）定义

```text
B_Q = min(q in Q) [b_q − ell_q]
```

则所有 Q 满足上述**声明安全阈值**的精确局部充要条件为 `max(a_i,a_j,c) <= B_Q`。当 sigma>0，它是官方按时判断的充分条件而非必要条件；只有 sigma=0 才与官方按时界限一致。它只覆盖这个固定 suffix 的 Q，不声称其他飞机/前缀/补偿也准时。若 Q 为空不定义此救期窗口。

该式把三侧 join 变为同一 latest-second-take 上界：源 i 的 drop、源 j 的 drop、C 抵 H 都必须落在 `B_Q` 之前。可以按 H 和 B 做索引，保留真实来源位置、路线和当前 native 代价。原 late 的余量 `B_Q−T` 衡量该组合的时间可行性；不把“原迟到程度很大”当不可救的证明，也不要求 i/j 都原 late。一件相邻 ontime 货可以协助配载，但必须计其时钟损失。

以下必要式相对于所声明的按时阈值。若用 sigma>0，只能证明无法满足额外安全余量，不能据此声称官方 epsilon 下必然迟到；要做严格官方救期不可行筛，应使用 sigma=0 并加保守浮点方向。必要式分三种，不能混用：

1. 对固定 `a_i/a_j/c`，即使忽略全部 native 绕行，`T + distance(H,D_i)/v` 都超过 i 的按时阈值，则 i 在这一固定双取窗口内无法满足预声明安全余量的救期通道；官方 epsilon 边界仍由完整真实评分判断。这是单件 optimistic 运输下界，不是完整 J 下界。
2. 两件都要按时、且 OUT delivery 块不含 native 时，对两种 delivery 顺序分别求 `B_{i,j}`；两种均 `T>B` 才能排除该双件直送块。不能据一个顺序失败排掉另一个，不能把额外插 native 的更长路径当成更短。
3. 若 `a_i/a_j/c` 来自**尚缺 k1/k2 的 partial**，它们只是在固定 native/pinned 次序、固定 cuts、以后仅向 A/B 插入 whole DIRECT 补偿且不删除/重排现有事件时的乐观下界：Euclidean 三角不等式使保留事件时钟非减。这一有限 completion 分支可作必要筛，需逐条标注假设。普通 ALNS 会删除、跨机重分配或改变 prefix，便可能让这些量下降，**不得拿 partial 失败剪掉全模型或后续 ALNS**。相关证明边界见冻结的 `mobile_partial_completion_bound_v1.md`；此处固定站分支也不能套移动“receiver-first不可行”筛。

安全的局部充分证书必须更完整：四任务全部恢复；每机最终数与输入相同；所有 native/compensation 实际容量和完整取送合法；只有两项真实 cross 且同H；源只有 IN无OUT、C只有这对 OUT，图分层因此没有新交接环；A/B 全部最终交付按真实路线重新评分，C 按上述精确 `T+ell`，其他5机原路线不变。再明确计算原 ontime→late损失和 late→ontime收益。

如果所有原 ontime 都保持准时，且至少一项原 late 按真实完整时钟被救回，可证明 N至少下降1；但**仍不能证明 J改善**。最终必须对完整200任务用冻结真实 DAG/canonical evaluator 和独立 reference 重算 N/L/D，并以原固定加权 J 严格胜过强输入才采用。不能更改权重、把救期排名当词典序目标或只报告救回件数。

## 有限实现建议：改变 family 生成入口，而非再加早代表

本轮不实现；下列是后续 protocol 冻结前需细化的候选流程。

1. 固定同一强 DIRECT 输入与全局时钟，记录原 ontime mask。对所有原 late 的 task/H 和所有合法 receiver C，建立宽松运输必要条件；可用真实窗口，或明确标注的 home→pickup→H / home→H 乐观值作廉价初筛。它们不提供可达路线，更不提供完整J。
2. 在旧送达近邻3、receiver affinity2、`max_groups*2` **之前**，从 station-specific time feasibility 生成 i/j/C，而非只对旧 family 再追加窗口检查。partner 的送达距离可作同等时间机会的次级排名，不能先固定三近邻后声称已做新的 task 覆盖。C 比较实际空载抵H时刻，不只比较 home/目的地空间 affinity。
3. 补偿仍可复用已验证的 geo6 ∪ fullC-removal-credit6 候选思想；它不是新贡献。但 k1/k2 一旦选定，要删除全部四个原计划、按实际 residual 重建 C 空载 gap 和两个源的可行 IN positions。不能把删除前的 event index/clock、删一项的信用和另一项的信用简单拼成真实完整时刻。
4. 对每个固定 family/H/补偿组合，先获取源窗口及 receiver 实际 gap/suffix，再按 `a_i,a_j,c <= B_Q` 联合 join。旧实现 `_receiver_relay` 不接受 IN availability，receiver 候选先选好才做三侧 Cartesian；新入口必须显式传递真实 `a_i/a_j` 或生成携带它们的 joint record。保留联合可救回代表和真实物理best，而非只保各轴 earliest/timebins。
5. `Q` 可分为“救 i 并保 j”“救 j 并保 i”“两件均救”等有限通道；对真实 suffix 的原 ontime native 也形成 deadline 上界。源 native 的损失必须来自源完整路线，不能由 `B_Q`替代。排序记录可包含 potential rescue、protected loss、时间余量和真实 native score，**只用于有限候选排序**。
6. 坚持同一总 wall deadline。预先冻结 task/receiver/补偿/window/family 各上限及优先顺序，记录 generated/feasible/retained/filtered/fully_checked 数、截断原因、time window证据、shared selector records/hash。发现窗口失败只排除声明的 rescue channel；保留原强 DIRECT guard和预声明的普通物理best通道，不把未救N的候选称作无法改善J。
7. 如继续 ordinary polish，两臂放开相同的普通任务 owner/order 自由，实际补偿身份用 trace历史与当前 owner分开。post 所有 availability/suffix/shared/score/clock须当前重算，不引用 raw window证书作为post可行证明。缓存键必须包含使时窗改变的完整源/receiver路线、补偿集、H、availability、目标截止/ontime mask、objective和配置；旧 receiver-only memo 的“不依赖A/B时钟”前提在此新排名中已不成立，不能直接沿用旧键。

时窗算术 witness：假设固定候选真实 `a_i=4,a_j=7,c=1`，固定 suffix偏移 `ell_i=2,ell_j=3`，截止 `10,10.5`；T=7，实际交付9/10，可救回原 late。另一 receiver 的几何 affinity更好但实际抵H为11，则不能救回。这只说明“空间更近”和“独立保最早单侧”不能替代三方 join；不是完整任务方案，不构成官方收益证据。

## DIRECT 对照和实验解释

保留相同200输入、原8机/homes/4站、C2/K25、速度、目标权重尺度、wall预算、seed与sourceproof。共享 family selector 输出同一批 i/j/A/B/C/k1/k2，不按实验结果调换任务或种子。

强 DIRECT 对照不能只继承 RELAY 的唯一 `i/j:C、k1:A、k2:B` final map。对共同四项，DIRECT至少独立搜索保持 A1/B1/C2 最终责任数的 **12种 whole DIRECT 分配**（先选给A的4种，再选给B的3种，余两件给C），或提供不弱于此的同预算构造；每个 map 仍允许原有完整 P/D 两插入顺序、六种合法四事件块及普通任务修复。其他原任务和每机最终25保持。DIRECT选自己的最佳 map 与完整路线，不套 RELAY 的角色限制。

**不要求 DIRECT去H、不强加relay的take gap/等待、也不以relay的窗口失败拒绝更好的DIRECT插入**。若 post采用pin2，两臂用同一pin2政策，各自锁定真实选中宏中 i/j的完整计划与当前责任，其余任务具有相同修复自由；不强迫DIRECT的i/j最终也归C。保存每臂实际评估的map/完整叶、pin身份与最终责任。只搜索同一 final map 的DIRECT仍可作为 matched-map ablation，但不能冒充最强 DIRECT对照。

本设计暂止于窗口与候选目录，尚未承诺12-map源码已存在；后续正式验证的公平gate必须确认该强DIRECT搜索已实现并冻结。两个方法内部搜索量不必相同，但总wall和所有guard开销必须相同口径，原strong DIRECT checkpoint始终保留。

由于共享 selector 有意寻找中继救期机会，这属于有明确候选策略的机制开发，不能拿事后挑选的小场景代替官方整体比较。当前强DIRECT始终是每臂不可丢失的起点；强DIRECT本身已有的重新分单和共载自由不能被削弱。任何最终收益须同时核验选中任务、补偿和其余任务的变化，不能把native改善全部记作交接收益；DIRECT同样改善时需如实报告。

下一步只在该新入口、有限范围、tiny正反例、独立审计与正式计时协议冻结后实施。tiny至少覆盖：前置目的地/receiver门遗漏的真救期窗口、源两侧早选项分别存在但组合等货失败、补偿把乐观窗口推迟、native按时损失抵消救回、固定站缓存等待、两种delivery顺序、临界epsilon、deadline后不得采用、两臂source/hash一致、DIRECT实际覆盖12责任分配且不受H约束。失败也保留完整证据，不承诺中继必胜。
