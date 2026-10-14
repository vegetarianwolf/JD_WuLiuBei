# 固定中继站救回窗口选择器：只读复核 v1

日期：2026-10-15。范围：已冻结源码和 batch026/027 保存结果；没有运行优化器、测试、重放求解或修改历史证据。

## 判断

“A/B 分别取一件，经同一固定 H 交给 C，C 双件共载配送，C 原两件转给 A/B 或由普通任务修复配额”的物理结构已经由 batch016–025 的 repooling/pinned 系列尝试。最早 IN、最早 C 空载接货切点、完整 C 删除补偿信用、迟到风险和移动会合点的截止期种子也已有实现。仅重述这些结构或再保一个 early 代表，不能称为新方法。

仍有明确源码依据、值得单独验证的新机制是：**在目的地近邻和接收机几何截断之前，按固定 H 的实际源 IN 时间窗口、C 空载接货窗口以及当前迟到任务能否跨回截止期，联合生成和保留 family。** 这属于新选组/候选保留机制，不是新物理资源，也不是已经证实的收益。

## 旧实现究竟保留了什么

| 已冻结文件与函数 | 已覆盖 | 未覆盖的关键环节 |
| --- | --- | --- |
| `algorithm/experiments/relay_repooling_exchange.py::_select_groups`，102–185 行 | 每个任务异原责任机的目的地最近 3 单；按目的地区域 affinity 留 receiver；原迟到 N/L 风险参与优先级；再选补偿任务 | 113–140 行的前门不读取 H、实际 IN 到站时间、C 空载 gap 时间或可救回截止期；`max_groups*2` 前置截断后才做补偿 |
| `algorithm/experiments/full_score_repool_selector.py::select`，135–158、160–212 行 | 保留旧目的地近邻 3、receiver affinity 前 `receiver_limit`（诊断为 2）、前 `max_groups*2`（8 组时为 16）；随后把补偿信用改为 C 真完整 Score 的双删除、两侧真插入 | 风险只表示原解已经迟到多少，不表示接力后能否救回；完整 C 信用修正发生在旧前门之后，未改变时间可行 family 的入池机会 |
| `algorithm/experiments/quota_balanced_relay_exchange.py::_pairs`，78–106 行 | 单中继任务加整单补偿的有向配额交换；几何、原迟到风险及截止期差排序 | 仍没有固定 H 上的 source/receiver 联合截止窗口 |
| `algorithm/experiments/source_frontier_cache.py::source`，164–229 行 | 容量位置有界展开；超 512 时按几何/早 drop/早 pickup 截断；真实 native 评分；保物理最优、最早 drop、最早 pickup、4 个 drop 时间 bin 的多样性，最终受 `option_count=4` 限制 | 最早是单侧指标；没有按接收方具体交付后缀的截止时刻选择 source option。早代表也可能在之后完整 beam 中落选 |
| `algorithm/experiments/joint_mobile_layouts.py::rank_receiver`，289–349 行 | 物理最优、最早 empty C prefix、接货 prefix 时间 bin；最多 4 个接收候选 | 排名没有两源实际 availability；local 评分没有实际 IN 等待。`_diverse` 另保 H 物理头、移动下界、早 C 和 source frontier 多样性，仍不是固定 H 截止窗口 join |
| `algorithm/experiments/relay_repooling_exchange.py::_bundle`，399–428 行 | 每个共同 H 生成两侧 IN 和 C 双 OUT，再完整组合后进入 global partial 排名；没有用缺腿局部解冒充全解 | 420–424 行先独立生成 `receivers`，其 API 不接收两侧 IN availability，再做 left/right/receiver 笛卡尔积。这正是联合窗口需替换的候选构造位置 |

六任务系列也未补上这一前门：`six_task_sorting.py::_select_families` 68–117 行、`expanded_six_task_sorting.py::_expanded_select` 35–89 行先以真实原路线双删除信用与 home→P→P→H、H→D→D 几何代理保每机每 H 4 对，再保每 H 前 1/64 family。代理不是实际原生任务间隙的时钟或可救回 N。

`six_task_route_costs.py::_gap_options` 201–219 行只保最早零载 gap 和几何插入增量较低的其余 gap（共 4）；`in_options` 243–253 行保真实局部最优与最早 IN 完成（共 2）。这些是已有的早时刻覆盖，不应作为新机制再次申报。

## 已有 deadline 方法必须明确承认

- `mobile_deadline_geometry.py::_targets` 120–173 行已经判断 `eligible_deadline_rescue`，并在 `deadline_points` 中生成单/双截止期椭圆约束种子。
- `mobile_repooling_refinement.py::_spec` 100–126 行已经为选定完整 raw 布局的后缀生成 deadline constraints，并按可行紧度排种子。
- 它们是在既定任务组/切点后优化移动会合位置；新建议只能主张**固定 H 的前置 family 时间窗口生成**，不能主张第一次考虑真实时钟、截止期、救回 N 或多目标截止约束。

## batch026/027 保存完整候选的量化结果

共同输入为 full200、8 机、C2、速度 0.9、每机最终责任 25、原固定四站与起点；输入 N=59、L=1790.632128866067、D=578.5346848588046、J=0.4342157887877544。以下只读已保存完整提案，不扩充候选，也不重新选择种子。

| 批次/方案 | family | 保存 full | N 分布 | 原迟到→准时 / 原准时→迟到 |
| --- | ---: | ---: | --- | --- |
| 026 DIRECT | 4 | 8 | N59：8 | 全部 0 / 0 |
| 026 RELAY | 4 | 8 | N59：8 | 全部 0 / 0 |
| 027 DIRECT | 256 | 512 | N59：412；N58：100 | 410 个 0/0；100 个 1/0；2 个 1/1 |
| 027 RELAY | 256 | 512 | N59：412；N58：100 | 410 个 0/0；100 个 1/0；2 个 1/1 |

027 两臂的 102 次救回全是普通任务 **65**，两次新增迟到全是普通任务 **70**。六个被选任务在所有保存候选中均没有从原迟到变成准时。因此不能笼统说 027 的所有提案 N 不变，也不能把 N58 的改进归因于六件接力任务直接获救。三条新路线改变原生任务时钟，确实可以救回普通任务；这里 DIRECT 也有相同 N 分布。

027 RELAY 最佳 weighted 完整提案：J=0.437288227355、N59、L1814.298546570163、D583.9560634433086。

027 RELAY 在 **N58 子集内**的最佳 weighted 提案：J=0.441032688862383、L1887.4257621490044、D590.6253658441614；family 六任务为 `[26,68,91,124,147,166]`。虽减少一次迟到，总 weighted J 仍差于输入，不能当采用收益。

026 四个 family 共选到 12 个不同任务，全部原迟到；027 共选到 38 个不同任务，其中 37 个原迟到。对这些原迟到 selected 任务，在各自保存的完整 RELAY 提案中，用该提案自己的真实 IN drop 时间再加 H→D 单件直飞时间（已经忽略接货机可用性、另一件货及所有后缀绕行），仍全晚于截止。**这是对所保存提案的到站时间的乐观比较，绝不是对该 family 全部 IN 插入位置、其他 source 角色或全问题可行域的下界证明。** 每 family 只保存最佳 2 个全候选，不能据此声称全 IN 域均不可救回。

数据定位与复算口径：

- `algorithm/results/proactive_relay_diagnostic_20261014/batch026/runs.json` 和 `batch027/runs.json`。
- 选对应 arm 的方法 phase（`phases[1]`）后，基准为 `metadata.input_snapshot.delivery_times_min`；提案为 `metadata.full_candidate_snapshots[*].delivery_times_min`；selected 集为 `snapshot.move.family.task_ids`。
- task 截止期来自官方 CSV `algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv`，编码 GB18030；严格迟到阈值为 `clock > deadline + 1e-9`。
- IN 时刻来自 `snapshot.source_drop_availability_min`，H 坐标来自对应 `manifest.json.topology.stations`，单件乐观飞行时间为欧氏距离除以 0.9。

## 最有依据的下一步

保留旧 A/B→H→C 物理结构，只新增一个 **deadline-indexed joint window** 变体。先按 H、原迟到任务及实际合法 source IN 窗口查候选，不在旧目的地近邻 3/receiver 2/前 16 family 门之后补救；C 必须有真实空载接货 gap。同行 j 可以原准时，不能为了“多救一件”强迫只从两件重迟到货中配对。

对一条确定的完整 C 候选后缀，设两源 drop 时间为 `a_i,a_j`，C 抵 H 时刻为 `c`。第二次 OUT take 后时钟为 `T=max(a_i,a_j,c)`；后缀交付任务 t 的真实飞行偏移为 `ell_t`，则 `delivery_t=T+ell_t`（固定完整布局、纯 DIRECT 原生任务、无其他 relay 等待时）。这让两源和接货窗口可按 `T <= deadline_t-ell_t` 联合 join；固定站原模型允许货物缓存，不应免费增加“接收机必须先到”或修改缓存语义。

具体替换边界：新的 shared selector 产生前置窗口 family/有界窗口记录；新的 `_bundle` 等价构造器按具体两侧 IN availability 联合排名 C 候选，而非只修改 `_rank_receiver` 添加一个最早代表。真实 `State.key` 和 RouteInsertionOption.delta 不能加虚构救回信用；窗口只用于候选生成/排序。若补偿任务尚未插回，缺单窗口只能标为 optimistic，不能当完整可达证书或全局 J 剪枝。所有四任务补回、最终 K25/C2/覆盖/DAG 完整后，才记录实际 selected rescue、普通任务新迟到和真实 J。

匹配 DIRECT 使用相同 selected tasks/final responsibility、补偿政策、总预算与有限候选上限，同时保留完整 DIRECT 取送选择及同样普通任务修复。两臂共同 selector/hash 必须一致。可保少量旧几何代表作覆盖，另分固定窗口名额；不能悄悄扩大总候选或测试预算。

先检查这一有限窗口目录中是否真正出现“至少一件当前迟到 selected 被完整候选救回、其普通任务代价有账可查”的见证，再决定是否值得下一次短诊断。没有见证时，仅能说本次有界窗口构造未找到；找到见证时也仍需用最终 full weighted J 与强 DIRECT 对照，不能先宣称接力已显著胜出。
