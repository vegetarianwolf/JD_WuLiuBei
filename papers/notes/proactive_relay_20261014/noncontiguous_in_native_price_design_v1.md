# 非连续单IN在目录截断前的真实native价格：设计v1

状态：下一轮设计，**尚未实现、尚未进行官方优化验证**。本轮仅读取032既有结果、旧函数和原始route的载荷序列；没有生成或求解新的官方候选，没有修改032及任何冻结源码。下一轮需独立冻结新源码、测试、审计、参数和预算。本设计不承诺中继能优于当前强DIRECT。

## 1. 032证据与本次新增的范围

来源为`algorithm/results/proactive_relay_diagnostic_20261014/batch032/runs.json`，文件SHA256：

`2405b1d062d5f7a4241e708773903f5865245056e500a57c88f6722db6bf616e`。

使用其中`native_price_relay.phases[1].metadata.input_snapshot.routes`的真实pureDIRECT输入，`initial_routes_sha256`为：

`6f7399b87ee175ae2804365bf5e6bc78f069003a73295bf0042cf8568478ac1e`。

该输入初始解文件SHA256为`c3a163888afb60098b0359b12e6aad1a54aa5da159e302132100cb06ff1330c8`。032的`audit.json`已记录`passed=true`，manifest SHA为`ae667085339e73267ede9a45a562502a807c598d803e38e0fe73b44c0dd45661`；这只证明032已保存方案及其遥测，不证明本设计的新邻域有效。

032对5600个task/H/other-source profiles的176400个合法**连续**P→H位置，在feeder3之前计了native真价。之后保留2400个profiles、省略3200个；位置渠道省略850次，176400个位置仅保16800个代表。73077个完整连续早价family进入base64截断，随后只有77次一般source表构造，每张表先以geometry/early/期限B方向缩到position8，再按native真实分数留source option3。`source_latest_bound_position_omissions=0`只表示已声明的B代表未漏，不是任意native真价最佳位置均已保留。

早价变准确后，16个最终family中15个weighted early布局的两个source gap均为50、receiver gap为46，即源先完成自己的25单再去接别人的货；另一个family的source gaps为41/45、receiver gap42。真实native价倾向避开前缀损伤，但也把新交接推迟。最佳完整RELAY仍未超过原strong：N59，L1786.3481545541754，D585.9528237282855，J0.43636644384604817；原输入N59，L1790.632129、D578.534685，J约0.4342157888。N58通道救的是native65，DIRECT同family也能达到N58，不能归为pooled i/j中继独有收益。

因此下一假设是：某机可以沿现有native路径先取一件别人的货，在不妨碍C2的途中继续做自己的任务，稍后顺路交H；这种非连续IN可能比任何连续P→H绕行便宜。它若在feeder3之前没有计价，后面的position8不能恢复已删掉的feeder。此为**候选生成层级的覆盖缺口假设**，没有证明当前官方数据确有更优解。

旧`deadline_window_costs._single_layouts/source_options`已经允许g<h，只是先缩position8后才计native真价；010的单feeder分散双pickup/jointdrop也尝试过沿途捎带。新增点是把**不同feeder各单IN的所有容量合法两gap位置，提前计分段native真价后再做feeder3/base64**，不是首次提出途中载货，也不是改变交接物理模型。

## 2. 原gap、候选路线与容量充要条件

原机a的真实pureDIRECT route为`v[0:n]`，gap k表示在原事件k之前插入，k=n为开放尾段。原gap载荷为L_k：L_0=0，处理原事件k后得到L_(k+1)。原事件clock、每次最终delivery clock和原home都来自同一真实完整route回放。

对不属于a原最终责任的任务t，固定原站H及其RELAY_IN identifier `in_t`。g<h的候选为：

```
v[:g] + (+in_t,) + v[g:h] + (-in_t,) + v[h:]
```

真实新pickup position为g，新drop position为h+1；**h始终指原gap**，不可混为插入后索引。pickup/drop均完整且只出现一次。g<h意味着g<n；h可为n。

在原native已合法且C2的前提下，新货从P到H始终占一个槽，容量合法当且仅当：

`L_k <= 1` 对全部 `k=g,g+1,...,h`。

必须包括h：到H卸下新货之前，原gap h载荷仍存在。只检查两个端点会漏掉区间内native曾同时载2的非法情况。区间外原载荷不变；IN不计最终责任，所以原native全部仍交付、K保持原数。pickup时原本已载1是合法情况，不要求source空载。所有原native owner和signed-event相对序保持。

g=h单独沿用连续块`u→P→H→v`公式。P和H同时替换同一原边，不能把该边的两个单插入detour相加。两类候选都要保留，避免新非连续通道覆盖掉连续最优。

## 3. 两次detour与三段时钟

记u_k为gap k前位置（k=0用真实home），w_k为原事件k位置。速度为V，原距离为D_a。g<h时两次插入修改的是两条不同原边：

```
deltaP = d(u_g,P) + d(P,w_g) - d(u_g,w_g)
deltaH = d(u_h,H) + d(H,w_h) - d(u_h,w_h)   (h<n)
deltaH = d(u_n,H)                            (h=n)
sP = deltaP/V
s12 = (deltaP+deltaH)/V
D_new = D_a + deltaP + deltaH
```

Euclidean、无release/handling、原路线全DIRECT，因此在实数模型下deltaP/deltaH均非负。原delivery事件位置r的native时钟变化恰好为：

| 原delivery位置 | 增加时间 |
| --- | --- |
| r<g | 0 |
| g<=r<h | sP |
| r>=h | s12 |

分区按**最终delivery事件的位置**，不是native pickup位置。因此g前已取、g后才交的native也必须计入受影响区间。所有native的N/L均重新计入，IN的负事件只记录drop可用时刻，不算final delivery、N/L或K。

代数上的H到达/drop时刻为：

`a_t = original_clock[h-1] + sP + d(u_h,H)/V`。

该式只用于数学推导。建议实现时对每个g，从原gap g前真实clock，先飞P，再沿原事件g…h-1逐flight累加，最后飞H，直接得到真实float ready；固定g可增量推进各合法h。这样不用靠source native是否接近deadline来发现ready舍入误差，也不把近似source availability传给C的deadline排名。后缀分数仍可用索引加速，完整候选最终再真实回放。

source只有IN和原native DIRECT，没有OUT等待；各source drop独立，不依赖receiver。H保持原异步缓存语义，允许先到的源卸下后继续原任务；不额外要求C先到，不引入mobile/no-cache资源变化。C等待仍由两个真实drop和其到站clock计算，并全量计入native后缀和selected delivery。

## 4. N/L两种等价公式与稳定计算

官方EPS为1e-9 min。每项只在`(clock-due)>EPS`时同时计N和**整个**lateness值；不能改成`max(0,lateness)`后把EPS小项加进L。所有权重、scales、home和任务不变。

公式A：令S_k(s)是原delivery位置>=k的任务在统一shift s后的`(N,L)`，不含前缀；B为原整机`(N,L)`，则：

`new = B - S_g(0) + S_g(sP) - S_h(sP) + S_h(s12)`。

公式B：令F_k(s)是原gap k前native分数原样、gap k后统一shift后的**整机**`(N,L)`。冻结`exact_suffix_delay.shift_direct_suffix`采用此完整口径，因此：

`new = F_g(sP) + F_h(s12) - F_h(sP)`。

两式在实数模型等价。实现必须明确索引口径，不能把S/F混用造成原前缀重复计分。N使用整数加减；L采用有误差界的和。三个查询各自误差<=1e-10，**不代表组合误差仍<=1e-10**；必须把各项误差及组合运算误差相加，并处理两个大suffix相减的抵消。

更稳的等价方案是直接预建middle delivery集合`g<=r<h`的索引M_(g,h)，用：

`prefix_before_g_original + M_(g,h)(sP) + S_h(s12)`。

这不需两个大正数相减。middle含0至若干native delivery，按合法L<=1区间枚举可共享索引；不能把纯pickup事件数量当delivery数量。尾gap h=n时S_h为空，所有原delivery只受前两段影响；deltaH仍增加D与drop时刻，即使其native N/L不变也不能省略。

本轮不选择或实现哪种索引。下一轮可按实际误差和内存冻结一种；两个公式及真实route replay应在tiny中相互核验。

## 5. 浮点、fallback与deadline合同

这里只保证上述受限实数模型；不承诺任何IEEE加法路径bitwise相同。两次detour求差、除法、多个原flight时钟累加，以及suffix减法都可能在EPS附近改变N。正确处理至少包括：

1. 沿用显式的非负几何检查。仅在已声明ULP误差带内允许tiny negative校正0并强制真实route回放，实质负值或非有限值拒绝，不能任意clamp。
2. 每段查询使用实际`x+shift>EPS`谓词及边界标志，不改写成不等价的`x>EPS-shift`。误差带须包含这一次两detour/多flight累计的时钟误差；不能未经推导就假定旧连续模型的常数ULP带足够。
3. 组合L误差超过声明容差、抵消、非有限或任何native靠近EPS时，在**feeder截断之前**完整`route_cost`重放新source，使用其真实N/L/D和availability。只做前64之后回放无法纠正已经错排的feeder。
4. 推荐所有候选ready直接用第3节的真实局部flight链计算。若未来为性能改用代数ready，必须传播其误差区间到C selected/native的EPS判断；必要时先回放真实source前缀再回放C，不能只检查source native期限。
5. C仍遵守032的selected-only EPS fallback、逐flight交付clock、实际(ai,aj)回放缓存；物理回放结果不进仅max-ready缓存。所有完整保存证据用真实source availability重新生成，不复用聚合式clock。
6. primitive和catalogue的Deadline类若不同须转换。索引构造、查询、误差检查、真实回放、全部证据与hash均在共同绝对deadline内；过期不发布不完整共同目录。

存在旧relay等待、release date、处理时间、速度差异、多个新增IN、原native重排/跨机改变等情形时，这个两段suffix公式不直接适用。不能当任意任务重构的全域可行性/J下界。

## 6. 当前真实load域规模：纯静态计数

以下只读取第1节input routes的signed事件，没有调用optimizer、候选几何评价器或分数函数。对每route构造L_0…L_n；连续合法gap数为`count(L<=1)`；把L<=1的最大连续段长度记m，合法g<h数为`sum(m*(m-1)//2)`。

| aircraft（0-based） | 原事件数 | 合法g=h | 合法g<h | 最大h-g |
| --- | ---: | ---: | ---: | ---: |
| 0 | 50 | 33 | 20 | 2 |
| 1 | 50 | 35 | 30 | 4 |
| 2 | 50 | 31 | 14 | 2 |
| 3 | 50 | 31 | 16 | 3 |
| 4 | 50 | 32 | 21 | 4 |
| 5 | 50 | 29 | 8 | 2 |
| 6 | 50 | 31 | 14 | 2 |
| 7 | 50 | 30 | 11 | 2 |
| 合计 | 400 | 252 | 134 | 4 |

每机原最终25单，其他175个任务×4H对应700 profiles，故新g<h位置为`134*700=93800`。与连续`252*700=176400`相比，合计270200个位置，增加约53.2%。这只是在当前输入和原事件相对序固定下的容量域大小，不含geometry/后续source代表数量，也不是新算法的实测耗时。每新位置可能需要三个suffix查询或middle索引，且新的ready会改变C缓存命中；不能由位置数保证40s内完成。

此静态域比盲目每profile枚举51选2小，足以支持下一次有界实现的可行性。另一方面，原C2载2区间阻断沿途持货，最长仅跨4个原事件；不能把该域说成无限长携带或已穷尽全部协作路线。

## 7. 手算合法示例：新域为何可能改变feeder排序

速度1，source原home及第一native pickup在P=(0,0)。原两native wholeDIRECT路线为：

```
pickup u:(0,0), deliver u:(0,10), pickup v:(10,10), deliver v:H=(10,0)
```

原D=30，native u/v的deadline为10/30，均准时；原load序列为[0,1,0,1,0]。新增别机任务t也在P取货，于H交接。

- 非连续g=0,h=4：取t后沿原路线做u/v，capacity依次不超过2，最后在H交t；deltaP=deltaH=0，source D仍30、native N/L仍0、真实drop=30、原final K仍2。
- 所有连续g=h可放0…4。各额外D为20、10sqrt(2)、20、10sqrt(2)、20；前几个位置给native增加迟到，尾位置虽不改native时钟，drop=50。若t的receiver能在H等候并需于31交至(11,0)，非连续drop30可满足该局部期限，尾连续方案不能。

这只是解析source/receiver条件例子，说明连续native真价可以低估某feeder在非连续域中的竞争力；不是200单实例的可行完整胜出方案，也不证明新目录必超DIRECT。旧后置一般IN若已保留这个feeder，geometry可能找到它；本次改动针对它在前面的feeder3已被删掉的风险。

## 8. 建议接口、同cap与独立审计

下一轮可新增纯source组件，建议合同（名称未实施）：

```
price_source_profile(problem, native_record, aircraft, task_id, station_id,
    *, deadline, weights, scales, option_limit=3, latest_drop_bounds=())
 -> {records, statistics, scope}
```

输入native_record必须是真实home、完整wholeDIRECT本机路线，不删除native，不插补偿；record不是全200可行性证书。先枚举g=h与所有容量合法g<h并计真实native价格，**然后**沿用已冻结的有限真J/earliest/最小N/selected期限渠道与sourcecap3、feeder3。渠道超过cap的省略仍显式计数，不声称四个不同渠道必全保。

每实际代表至少保存`original_pickup_gap=g`、`original_drop_gap=h`、实际pickup/drop positions、deltaP/deltaH、sP/s12、actual drop、native完整score、相对本机delta key、每段native delivery IDs/分类、FP回放flag及误差scope。全profile只需轻量统计，完整选中证据可保存实际route以独立重建；不能为全部临时候选保存巨量重复dict。

共同目录仍是same-original-owner C的i/j、两个不同feeder、原H，donor k1/k2仅DIRECT参照，RELAY不移动donor。base64→16、四H渠道、C删除i/j重计时后的所有空gap×两delivery顺序、完整witness≤3真J/minN/pooled、198native owner/subseq、原home/8机/C2/K25、输入与权重保持。非连续early exact source布局需进入同family sourcecap3及完整候选评分池；只改变排名却在后置pos8重建中丢掉它，不能称实际覆盖。保留/省略理由和实际表引用独立核验。

DIRECT使用相同共同目录和总wall，不被迫走H或采用RELAY角色映射；same-owner i/j独立全空gap×6合法PD块、donor四单独立12map、强原解始终作为guard，规则保持032。完整采用仍须严格优于原输入J，所有200任务与真实clock/N/L/D经canonical和独立审计，不拿source局部分数当完整改善。

新独立审计从同一真实base和原g/h重建source，核整个load区间、原native owner/序、IN pair、K、drop、两段delivery分区与真实source完整分数；再核C实际等待和全fleet。它不导入producer/定价primitive获得答案。至少tiny覆盖：端点load合法但中间载2拒绝、已有载1、h=n、g=h两detour不能相加、middle无delivery、跨中段native delivery、上节零detour机会、native/selected-only EPS、大suffix抵消、内部deadline和early seed真实入表。

**优先建议**：下一轮只新增这一项前置非连续source真价，不同时改变C模型、source/feeder/base/family caps、目标或repair策略。032的尾端选择和93800个尚未前置计价的位置提供了可检验理由；是否改善仍需下一轮同预算完整实验。它不是唯一剩余方向，更不构成远胜DIRECT的保证。
