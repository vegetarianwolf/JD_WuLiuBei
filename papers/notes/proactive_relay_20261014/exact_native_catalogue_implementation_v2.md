# 双来源目录v2：截断前的真实native延误价格

状态：NEW实现合同。只新增`split_feeder_catalogue_v2.py`、对应tiny tests和本文；031、旧目录、旧数学组件、runner及既有证据不修改。不在开发中运行官方数据优化；正式参数与预算由ROOT的单独协议冻结。本合同不是性能成功报告。

## 输入、模型与共同目录

输入是同一真实完整pure DIRECT强解。i<j原最终owner同为C，feeder A/B互异且均不为C，H满足原候选资格和既有完整IN/OUT leg。i/j最终仍归原C；其余198项保持原owner及相对signed-event序。source新增IN不增加最终责任K。原home、任务、四站、机队、同速、C2、K25和加权目标均不变。

`build_catalogue`保留031字典接口、family基础字段及完整witness格式，无arm参数。DIRECT仅把RELAY模型的目录作为共同任务/角色排名证据，不把其路线或时间窗当DIRECT候选。真实donor k1/k2仍从原A/B各取一整单，按本机完整删除信用确定；它们仅用于DIRECT独立12map，RELAY早价及完整方案都不删除donor。

默认family16、base64、一般IN position8/source option3、完整table option3、feeder3、sigma0不变。v2明确只支持sigma0；任意不同协议须另冻结。

## 真实价格在截断之前

### source profiles

所有合法task/H/非owner feeder profile先扫描其原native路线全部合法**连续**IN gap；gap载荷可为0或1。按真实home累计的前缀、`P_i→H`和原下一事件计算drop时刻、插入距离与uniform native suffix shift。

native N/L按冻结`exact_suffix_delay.py`的条件数学模型和守卫价格计入，source完整距离也计入。对不同feeder比较时，用其各自原native分数之上的**ΔN/ΔL/ΔD**，不能比较不同飞机原本绝对迟到数。只有价格都算过后才保留有限source位置和feeder3。

有限位置/feeder渠道依次为真weighted delta、earliest、最小native deltaN、selected首次交付期限方向，去重后受cap3限制；遗漏计数公开。这并不穷尽一般非连续IN，也不保证四个不同渠道在三个槽内都保留。

### C native skeleton与所有接货gap

对同C的每对i/j先删除其整单并从真实home重新计时native skeleton，不能只扣除原i/j自身迟到。对每个原H、每个真实空载gap和两个交付顺序，在base64截断之前全部计算C普通任务uniform shift：

`shift_C=插入完整H→D_first→D_last块的新增距离/速度 + max(ai,aj,c)−c`。

同时按实际第二take时刻加回i/j自身最终N/L，得到完整C分数。全fleet条件价格为：

`原全fleet − 原A − 原B − 原C + 修改后A完整native + 修改后B完整native + 修改后C(native与selected)`。

原其它5架机原样；donor没有删或另加，距离没有按包重复计算。原prefix不变，suffix按delivery事件位置包含gap前已载、gap后交付的native。

## 复用计算不改变允许空间

source和C的suffix索引从真实DIRECT record预建，内部primitive超时会转换为共同`costs.Deadline`。热循环使用同一严格EPS与误差/边界守卫的紧凑查询，避免每次构造大型query字典；定向tiny比较冻结primitive。凡边界、抵消或误差超声明1e-10 min，必须完整`route_cost`回放该真实候选。Euclidean detour出现负数时，只允许在64×ULP(相关路线距离量级)范围内有记录地校正为0，并强制物理回放；超过该尺度或非有限值拒绝，不任意clamp。浮点flags仅描述执行路径，不代替独立物理证明。

source到站始终按`(前缀clock+d(u,P)/V)+d(P,H)/V`逐flight累加；C的selected第二交付按`第一交付clock+d(D_first,D_last)/V`计算，不合并两段除法。selected自己的期限也单独检查EPS边界带，即使native未近边界，也在前池排名前完整重放C并使用其实际selected clocks及native N/L，不能在回退后又覆盖回聚合算式。

同i/j/H下，C在两take连续、同点、处理0时，第二take及其后全部native/selected价格只依赖`ready=max(ai,aj)`。按**未舍入的float ready**缓存全部gap×2扫描后的真weighted/minN/pooled代表；再按`(layout, shift)`复用相同native查询。第一take的实际时刻并不只由ready决定，因此缓存不复用第一take遥测；每个保存完整layout都用真实ai/aj重新生成全部时钟。

上述ready/shift复用只用于远离数值边界且无需物理回放的价格。只要本次任意布局触发selected/native边界或误差回放，本组代表只能按**精确(ai,aj)对**缓存；物理回放结果不能写入ready-only或aggregate-shift缓存。这样不会把另一个相同max但不同第一take等待舍入路径的分类复制过去。统计分别报告far-ready命中与actual-pair命中。

给定某source组合，source价格相对C布局是常数，所以C这三个渠道能在连续早价模型中保住该组合的真J、N与pooled代表；所有实际C gap/order都已计价。统计区分logical ready请求、实际全gap价格、ready命中、shift查询/命中和完整浮点回放。没有沿用旧`(ready,sumΔD)`二维proxy Pareto，也没有先用廉价3gap删去C布局。

## 64→16、一般IN表与早期布局

按条件真J/minN/pooled渠道及四H集合保留，把有限source/feeder组合截到base64。未进入一般完整表的数量仍明确记录，不能说它们或其它非连续IN已被全域否定。

每个base family的早期weighted布局和distinct best-pooled布局均实际完整重放。其≤2条真实source路线强制进入该family各source option3；余槽从一般`source_options(position8, option3)`真实表补入。position8只约束一般IN shortlist，不能冒称早期all-contiguous域也仅8个位置。

同(t,a,H)在不同family可有不同seed gap，因此`family.source_tables`是权威的实际cap3表。顶层`source_tables`为带明确scope的inspection union，可能超过3，不是一张全局共用cap3 surface；不以相同key覆盖后假称相同表。

两early完整布局进入真实full candidate pool，与family source3×3和C全emptygap一般完整表共同评分，再用原weighted/minN/pooled三个渠道保留完整witness≤3。**不为强留两early而挤掉最小N渠道**。早期方案可以被各更好完整渠道替代，但它已完整打分，原始价与路线保存，真J最佳机会不应丢失。

默认source cap3足以保存两early的source路线；较低非默认cap不足会明确记录，不宣称完整保存。每个channel的`seed_retention`中`early_price_model_replayed`只表示完整连续价格模型已真实重放；`early_seed_entered_candidate_pool/early_seed_full_scored`表示来源实际保留后进入完整候选评分池，默认3均为true，cap1不足时可为false。另写明最终同route是否留、未留原因及最佳完整代表key。保存early证据不等于进入候选池或采用该方案。

## 可独立重建的证据

每family保存`early_pricing_evidence`（weighted）与`early_rescue_pricing_evidence`（pooled）。同layout时允许重复描述。固定`pricing_scope='complete_contiguous_IN_and_OUT_block_model_before_base_pool'`。

- `source_layouts`：各自task/aircraft/真实gap、drop时刻、ΔD、native shift、`native_score`及float回放bool。
- `receiver_layout`：删除i/j后真实skeleton gap、交付顺序、到H时刻、第二take、等待、ΔD、native shift、`native_score`及float回放bool。
- `full_score/key`、selected真实交付时刻、pooled救回/新迟到、全fleet `priced_seed_routes`。

`native_score`的N/L只统计该机原native；它的D是该机**整条修改后路线飞行距离**，不是可分离的“native包距离”。C的selected N/L另计，D不会再加一次。`early_priority`只绑定weighted evidence的key，与后续一般IN可能改善后的family priority分开。

独立v2审计从同一真实base/gap重建全部route和DAG，逐项核算这些价格、原责任/198序、source/C shift、额度/容量、source表引用和最终完整witness。它不导入producer、定价primitive来获得答案。

## 时限、采用与对照

全部索引、source profiles、C价格、缓存、完整重放、hash和快照都在同一绝对wall deadline内。任一阶段到期均抛共同Deadline，不发布不完整catalogue prefix；caller只能保留原已验证guard。完成目录的两个digest及completed时间也在时限内。

producer v2只换目录依赖并保存新证据；现有完整canonical guard、所有真实时钟、强输入回退、DIRECT same-owner全空gap×6与独立donor12map保持。后续正式run参数和stage统计由ROOT协议描述，不因结果调整种子或预算。这个真实价格只对当前连续早期模型有效，不是全域可行性/J下界，不承诺中继优于强DIRECT。
