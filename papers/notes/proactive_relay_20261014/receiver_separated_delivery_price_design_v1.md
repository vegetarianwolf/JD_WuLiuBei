# 接收机分散两次交付的前置真价：设计v1

状态：NEW设计，尚未实现、测试或进行官方优化验证。只新增本文，不修改033及任何冻结源码、数据、结果或旧设计。提出下一轮可检验的单项覆盖修正，不承诺中继优于当前strongDIRECT。

## 1. 新增范围及既有覆盖

033已在source/feeder截断前计入全部容量合法g<=h单IN源路线，source保持这一模型不变。其source价格、原home、同速、原固定H、C2、最终K25、200任务及weighted J不改变。

当前split-feeder receiver仍在C空gap插入完整四事件`+OUT_i,+OUT_j,-OUT_first,-OUT_second`，两次交付之间不能做原native任务。候选即使完成准确native计价，也可能因这次完整往返拖延后缀；需要检验C能否先送一件、沿现有native路线继续携带另一件，顺路完成第二次交付。

**交付穿插本身不是新物理机制。** 旧`relay_repooling_exchange._receiver_relay`（379行起）已用`_negative_positions`分别插入两个delivery，允许native夹在两次交付之间；`_rank_receiver`（338行起）先以geometry及早完成位置缩到position_limit，再作缺少source实际等待的local评分，`_bundle`（约424行）先生成receiver再与source表笛卡尔组合。旧delivery reconstruction及relay-preserving polish也曾开放交付附近的native插入。不能把这次工作写成首次允许接收机穿插配送。

本次新增仅是：在**同原owner、无补偿的split目录**中，对固定真实source availability，把这一受限接收机域的实际等待、selected交付及所有native损失放在base64截断之前计价，并保留实际early路线进入完整候选池。它不是再次增加同一四事件块的cap，也不将旧geometry/local候选当完整真价。

## 2. 原native skeleton、原gap与候选

i/j原最终owner同为C。先从真实base C路线删除i/j两个完整DIRECT计划，并从**原home重新计时**得到native skeleton `v[0:n]`；不能只从原C分数扣掉i/j自身迟到。其余native owner及signed-event相对序保持。

gap k表示在原skeleton事件k之前，k=n是开放尾部。原gap load为L_k。两个OUT pickup始终在同原H连续发生，C起始必须空载。考虑两种selected交付顺序`(first,second)=(i,j)`或`(j,i)`。

对g<h，实际路线为：

```
v[:g]
+ (+OUT_i,+OUT_j,-OUT_first)
+ v[g:h]
+ (-OUT_second,)
+ v[h:]
```

这里g/h均为插入前的skeleton gap。两个pickup实际position为g/g+1，first delivery为g+2，second delivery为h+3。原事件r的新position为`r + 3*(r>=g) + (r>=h)`。

对g=h则是原四事件连续块，second delivery position为g+3，原事件r>=g整体后移4；保持单独公式，不把同一原边的两次插入detour重复相加。

## 3. 容量、共载与原额度

候选要求：

- L_g=0，两个OUT pickup连续且零处理时间；
- 全部L_k<=1，k=g…h，包含h；
- 第一段实际共同持有两件的飞行距离`d(H,D_first)>1e-10 km`；
- i/j仍是各自原source的单跳IN接C的OUT，两个真实跨机handoff、相同原H，完整leg与覆盖不缺失。

C从空载双取后持2件，下一原native事件必为pickup，因此在继续native之前必须先交一件；第一件delivery立即在双take后完成。之后持second，native最大只能载1，所以上述区间条件恰好控制本域的容量。只检查g/h两个端点会漏掉中间native载2。h=n时仍需检查尾部gap的载荷；该尾部没有后续native，但第二件交付不可省略。

原native最终责任不变，C恢复i/j后仍负责原来的25项；A/B原native也不删除，IN不计最终责任。198项native保持owner/序，全部200任务仍须完整取送。此设计不改home、站、机队、速度、容量或K，不引入预释放、额外资源或空中缓存模型。

H仍用题目既有异步缓存：源按033实际IN路线到站drop后可以离开；C可先取第一件再在H等第二件。不得偷偷套用receiver-first/no-cache约束，也不得在没收到货时起飞。

## 4. 实际等待、两条distinct原边与分段native延误

固定033两个真实source drop availability为a_i/a_j。原skeleton gap g前位置u_g（g=0为原home），后继位置w_g；C到H的无等待时刻为：

`c = original_clock[g-1] + d(u_g,H)/V`，g=0以前缀clock0代替。

实数模型中双take完成时刻和等待为：

`T=max(a_i,a_j,c)`，`W=T-c>=0`。

实际浮点路径须依take顺序i、j逐次执行`clock += max(0,availability-clock)`，不能假定与单次max bitwise相同；保存真实两个take clock及空载/持1等待。零handling和同点take不改变飞行距离。

g<h意味着g<n，头部替换原边g为`u_g→H→D_first→w_g`；尾部在原边h插入D_second：

```
deltaHead = d(u_g,H)+d(H,D_first)+d(D_first,w_g)-d(u_g,w_g)
deltaTail = d(u_h,D_second)+d(D_second,w_h)-d(u_h,w_h)   (h<n)
deltaTail = d(u_n,D_second)                              (h=n)
D_new = D_skeleton + deltaHead + deltaTail
sMiddle = deltaHead/V + W
sTail = (deltaHead+deltaTail)/V + W
```

两次detour修改的是不同原边，Euclidean实数下非负。等待W在两段各自总shift中都只出现**一次**，不能把W随第二个detour再加一遍。原native最终delivery事件位置r的变化为：

| delivery位置 | 增加时间 |
| --- | --- |
| r<g | 0 |
| g<=r<h | sMiddle |
| r>=h | sTail |

分区看delivery事件，不按native pickup分区；h前已经取、h后才交的native也必须计入tail。h=n时tail集合为空，尾部D及selected交付仍完整计算。

selected第一交付代数时刻为`T+d(H,D_first)/V`。第二件不能沿用旧`T+d(H,D_first)/V+d(D_first,D_second)/V`直达offset：它实际先走完native[g:h]。建议每固定g/first，从实际take clock飞D_first，再逐flight推进原native事件到各合法h，最后飞D_second，取得真实selected clocks。g=h则只需依次飞两交付点。没有把“原prefix+sMiddle”的代数时钟冒充实际加法路径。

g=h继续用完整块一次替边：

```
deltaBlock = d(u_g,H)+d(H,D_first)+d(D_first,D_second)
           + d(D_second,w_g)-d(u_g,w_g)      (g<n)
deltaBlock = d(u_n,H)+d(H,D_first)+d(D_first,D_second)     (g=n)
```

原r>=g的native统一shift为deltaBlock/V+W；不能把deltaHead/deltaTail两个同边公式直接相加。

## 5. 真实价格、数值守卫与缓存

native采用稳定的`原g前prefix + middle[g,h)(sMiddle) + tail[h,n)(sTail)`，N用严格`(clock-due)>EPS`，L只对这些late项加整个lateness，EPS=1e-9 min。native距离为修改后整条C路线D，selected i/j只另加其最终N/L，不能再按包重复加D。

完整fleet价仍是原全fleet减原A/B/C，再加033修改后的两个source完整分数，以及C删除i/j后重计时并插本receiver的完整分数。其他5机原样；donor k1/k2在RELAY没有删除或额外加信用。此价只是固定原序、固定两source实际表的有限路线模型，不是全域J下界。

浮点要求沿用033并扩到新C双段：detour tiny negative仅在声明ULP带内可记录地校正0且强制真实route回放；实质负或非有限值拒绝。middle/tail的sum误差、最终合并误差和物理clock累计误差共同预算，不能把每段合格误报成整体合格。native或selected任何任务接近EPS、误差超限、抵消或非有限，都必须在base64截断前完整`route_cost`回放真实source/C并采用actual分数和时钟，不能留到已选目录后才发现错排。

selected第二件使用真实native飞行链，所以即使native全部远离期限，selected自身EPS也须单独保护；source实际a_i/a_j保持033真实逐flight结果。完整保存布局和最终采用还须独立DAG/physics重放。

缓存只允许复用相同truebase/skeleton、task pair、H、g/h、交付顺序、目标参数下的数学相同查询。远离边界且没有回放时可借鉴033按max-ready及shift复用；任何physical fallback结果只按真实(a_i,a_j)缓存，不复制另一相同max但不同第一take等待的路径。缓存计数分logical/actual/hit，不把缓存命中当未枚举或省略的物理证据。

## 6. 原seed实际入池、同cap与强DIRECT

033的source表及general position8不变；其每family实际early g/h source路线继续在cap3内保留。新receiver早价必须保存其真实g/h、交付顺序、完整route和当前wait/native/selected时钟：不能只换前门价格，随后仍调用旧连续receiver重建而丢掉原机会。

两个early完整布局（weighted、pooled）实际进入完整候选评分池；在同source/table/full cap3内透明保留必要source/receiver见证，余候选共同按真实J、最小N、最佳pooled三渠道保最终<=3。不为强留两个较差early而挤掉minN；保存source成员、receiver实际layout、入池与最终同route保留/被更好完整渠道省略的事实。family source表仍权威，顶层inspection union不能冒称单一cap3通用表。

保持family16/base64、四H渠道、feeder3、source3、一般position8、table3/full3、原strong025 guard、原weighted/scales；未来单独预注册seed，两臂相同seed及总wall。未进入有限代表或前64的路线不能说已被完整否定。

**DIRECT同步增强接收机对应自由。** 同原C的whole pair应独立保留两种整单插入次序，允许其pickup/delivery与native合法穿插，可借鉴旧`_receiver_direct`（357行起）的两种顺序构造，但要真实native/当前clock评分并遵守同候选cap/总wall。它不经过H、不使用RELAY availability或被迫采用RELAY分工。现有全空gap×6合法PD连续块作为原候选继续保留，donor四任务独立12map仍可选自己的finalmap并使用对应灵活wholeDIRECT构造，强原解始终保留。

DIRECT的这种有限构造也不等于全域最优；但不能给RELAY允许第二件沿native配送、却把DIRECT锁在四事件连续块，然后把该限制产生的差距写成中继收益。匹配same-map ablation与独立强DIRECT应分清。

## 7. 需要的新验证与到期合同

未来只新增组件/producer/auditor，不能改已冻结034之前的历史证据。正式实现前冻结具体接口、完整pipeline和同预算协议，开发不以官方数据反复探测参数。

至少需要下列tiny，而非大量官方重跑：

- C双take前真实空载；仅端点合法但中间native载2拒绝，包含h，first delivery后持1与native共载合法；
- 在native pickup前不先释放一件会超C2的反例；H→D_first正共载航段与零距离拒绝；
- g=h联合detour、g<h不同边、h=n开放尾部、空middle、delivery跨区间分区；
- 原C先删i/j重计时、source先/后到造成的实际等待，以及W只计一次；
- 沿native送第二件比直达完整块更低J的解析合成例，全native延误与selected真实第二clock都绑定；
- 非二进制速度/逐flight加法、native EPS和selected-only EPS、两段sum误差超限的前池回放、同max不同(ai,aj)缓存；
- early原receiver布局实际进入完整候选池、cap3后J/minN/pooled代表正确且省略声明真实；
- DIRECT灵活whole-pair正例、六块仍存在、独立12maps、原strong回退及相同资源/K；
- 索引/回放/hash内部deadline，未完成共同目录和未完整验证候选都不能采用；
- 独立审计从truebase/原skeleton g/h重建，不导入定价producer或primitive取答案，核source provenance、所有200覆盖/K/C2/DAG、当前全部clock/N/L/D。

所有新早价、cache、表、回放和证据开销都计入原绝对wall；只允许采用截止前已完整验证并严格优于原输入J的结果。过期返回原checked guard或已有有效complete incumbent，不返回未检查partial，也不把未完成的不同共同目录当完整对照。

该项值得验证，是因为当前split receiver在准确计价后仍只开放连续四块，而旧较广模型的native穿插没有在这个免comp前门作完整availability-aware真价。它不是唯一剩余办法，更不保证官方实例能获益或远胜DIRECT。
