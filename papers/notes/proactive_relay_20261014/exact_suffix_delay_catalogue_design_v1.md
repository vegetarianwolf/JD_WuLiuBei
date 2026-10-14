# 连续插入的真实后缀延误定价：下一轮目录设计 v1

状态：数学范围审查与独立小组件设计。第031轮源码、数据、审计和既有冻结文件不作修改；本轮不集成流水线，不运行官方优化器，不声明性能改进。

## 修正的对象和位置

031的共同目录在保留四H机会后，对完整relay见证重新算了全部native时钟，但较早的feeder3、廉价接收gap代表及前64仍按省略source/native与receiver/native延误的proxy截断。完整重算只救得回已经进入前池的family，不能证明被漏掉的family更差。

后续若实现，应在这些截断**之前**给连续插入候选加入真实条件native N/L：source价先于feeder3和源组合排名，C价先于廉价3gap及前64。最后16family和完整见证仍重新算当前全路线，并由原完整canonical guard决定是否采用。它是修正前门价格，不是扩大同一个候选池或把未用满40秒理解为应重跑。

## 适用的严格结构

所有时间从同一原home、同一速度和真实DIRECT native路线累计；无释放时间、服务时长、旧relay等待或其他跨机时刻依赖。原native事件顺序不变。插入只替换某个原路线gap对应的一条飞行边，或在尾端追加。

1. **source单IN连续块**：在一个实际载荷为0或1的gap插入`(+IN_i,-IN_i)`，即`P_i→H`，中间没有任何原native事件。它与原持有货物同时满足C2；IN负事件不是最终交付，不增加K，也不计i的最终迟到。原native中gap前已经取件、gap后才交付的那件，也属于下面的后缀。
2. **C连续双OUT块**：先从原C删除i/j两整单，按原home重新计时这个真实native skeleton。然后在空载gap插入`(+OUT_i,+OUT_j,-OUT_first,-OUT_last)`；两take连续、之后同载正距离，处理时间0。必须用这个删除后skeleton的原普通任务时钟和N/L，不能仅从原C分数扣去i/j自身迟到。

非连续IN跨越一个或多个native事件时，各段插入延误可能从不同位置生效，不能盲套单一suffix shift。已有relay等待可能吸收或传播新增飞行时间，也不能套本式。未来若允许native穿插OUT交付段，同样要另拆分时段或直接完整重放。本设计不会以公式失败否定这些更广邻域。

## 真实条件时钟

设gap前坐标为u，原下一native事件坐标为v，尾gap没有v。source块的新增距离为

\[
\Delta D_A=d(u,P_i)+d(P_i,H)+d(H,v)-d(u,v),
\]

尾gap删去最后两项。该IN自己的到站时刻另为`a_i=t_prev+[d(u,P_i)+d(P_i,H)]/V`；它不是`ΔD_A/V`，也不是最终交付时刻。source新增等待为0，故原native所有gap之后事件统一延后

\[s_A=\Delta D_A/V\ge0.\]

接收端在删除i/j后的skeleton中抵达H时刻为c，两源真实到站为a_i、a_j。固定站允许缓存，第二take时刻

\[T=\max(c,a_i,a_j),\qquad w=T-c.\]

C块新增飞行距离`ΔD_C`按真实`u→H→D_first→D_last→v`减原`u→v`计算，故原native后缀统一延后

\[s_C=\Delta D_C/V+w.\]

两项selected最终交付时刻另外加回：`T+d(H,D_first)/V`与`T+[d(H,D_first)+d(D_first,D_last)]/V`。它们的N/L必须按实际任务期限和官方EPS另算，不能混进native表；C最终责任恢复原K，source新增IN不改变最终责任。原native prefix、各source全路线、C全路线和其他5机的分数都仍是完整fleet价格的一部分，所有飞行距离也照算。

## 按交付事件定义后缀

原native任务q的交付事件位置为p_q，gap编号g是在事件g之前插入。后缀任务集合是`p_q>=g`，不是“pickup在gap之后”的任务集合。`p_q<g`的已交付prefix保持原真实N/L；尾gap后缀为空，native N/L不变，但新增距离、IN到站或selected交付仍必须计入。

对某gap后缀设原真实raw lateness为

\[x_q=t_q-\mathrm{due}_q.\]

官方判断是严格`lateness>ε`，其中`ε=10^{-9}` min；只有满足该判断的任务同时计入N和整项lateness的L。不能把`0<lateness<=ε`的小正项累入L。

将x排序并预存后缀和。给定非负统一shift s，在实数模型中

\[
k=\operatorname{upper\_bound}(x,\epsilon-s),\quad
N_s=n-k,\quad
L_s=\sum_{r=k}^{n-1}x_r+N_s s.
\]

加回未变prefix即可得到本机普通任务的新N/L。此表可反复查询，native价格不再需要逐候选完整重放；它不包含新selected任务、距离或其他飞机，不能单独当完整目标函数。

## 浮点契约与必要回放

这里的“精确”指上述单块uniform-shift数学模型及真实条件native损失，**不承诺所有IEEE浮点路径逐位一致**。

- 实现二分可直接判断`x[mid]+s>ε`，避免`ε-s`和`x+s`两种算式在临界处不等价。N精确对应所声明的`x+s`浮点算式。
- 仍可能有`(t+s)-due`与`(t-due)+s`的重排差异。例如`t=due=1`、`s=ε`时，后一式恰为ε，前一式可能略大于ε。因此必须返回`boundary_sensitive/needs_exact_route_replay`，至少检查阈值两侧邻项。route适配器还按原clock/due量级增大边界带，必要时逐项重判给定`clock+s`模型；这仍不能代替真实逐段路线累计。
- 后缀和加`N*s`可能大数抵消。实现需保存累加误差估计，在超过声明绝对容差时以`math.fsum(x+s for selected tail)`回退；溢出或非有限物理价格必须拒绝，不能返回一个看似便宜的分数。返回数值误差/回退状态，不隐去异常。
- 未来caller遇边界、误差或超出适用结构标志，应完整`route_cost`重放这个候选后再排名。所有完整候选仍保留当前DAG/canonical检查，公式或未回放值不能充当全域J下界、采用证书或不可行证明。
- 非负shift是几何插入条件；组件拒绝负shift。若上游三角距离因浮点得到极小负数，应在那里按明确距离容差重算或处理，不能无条件截断任意负值。

## 源组合压缩及公平性

旧proxy只按`(ready=max(a_i,a_j), ΔD_i+ΔD_j)`压Pareto。加入真实source native价之后，不能把该旧支配关系称为完整J支配：更早或更短的来源也可能拖延更多紧期限native任务。若未来保留压缩，应至少将source的`ΔN、ΔL、ΔD、ready`一并作为条件比较分量，或者明确承认它仍只是有限proxy删选。即使连续插入表中被支配，也不否定同family后续非连续IN或其它feeder的更好方案。

所有参数、候选上限、四H集合保留与完整family枚举次序须在后续实现前另行冻结。DIRECT仍使用同一共同目录，但不被强迫去H、满足relay时间窗或继承relay final map；原strong guard、same-owner全空gap×6块和独立12-map donor对照的自由保持。目录所花时间与边界回放计入双方相同总wall预算。改变排名不是更换权重、输入、起点、资源或考核指标。

## 本轮交付范围

NEW `algorithm/experiments/exact_suffix_delay.py`只提供纯标准库不可变索引、uniform lateness查询和真实DIRECT record的单gap适配器；NEW tiny tests覆盖空后缀、EPS、小正项、mid/tail gap、已有载1source、C真实等待、删除后skeleton、浮点边界与大数抵消。组件不导入优化器，不集成031或下一轮runner，不计算官方数据的优化方案。本设计未获官方性能验证，不保证中继胜出。

实际API为`build_lateness_index`、`shifted_lateness`、`index_direct_suffix`和`shift_direct_suffix`。`primitive_only_not_pipeline`：此轮不存在调用这些函数的正式求解流水线；可选deadline也是该独立组件自己的边界，不是已接入某个solver的预算合同。
