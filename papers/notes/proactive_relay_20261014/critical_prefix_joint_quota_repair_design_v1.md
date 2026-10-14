# 因果前缀与动态额度联合修复：设计v1

状态：NEW设计，尚未实现、测试或运行官方优化；本文不冻结下一次运行seed、时间分配或新beam参数。只新增本文，不修改034或任何冻结源码、结果、数据及旧文档。不承诺中继超过当前strongDIRECT。

## 查证：哪些已做，哪些尚未覆盖

旧025的`relay_pair_preserving_polish`已经只锁i/j两个真实计划的mode、legs、H、source/receiver、各route selected事件子序和最终责任数；其余198 wholeDIRECT允许换owner和事件顺序。destroy会过滤pins、至少打开两个owner槽；repair使用实际global评分，完整候选核shape。023仍锁四任务、028锁六任务，因此自由度分别是196/194。不能把“198任务可以换机”或把新种子套进普通pin ALNS称为新邻域。

旧030的`relay_damage_closure`以新late、positive deltaL、交接时共载伙伴、补偿邻近和positive delivery delay选core，并保各机真实donor。未受损、却位于到站因果前缀的任务没有独立类别。`damage_closure_repair._assignments`先产生独立插入代价assignment、正向/反向大cycle及成对swap等最多四个owner maps；后续beam中`owner=plans[current.plan][task]`，只在该预定归属内分支插入位置。它不是状态内重新分配owner的联合beam。旧生产repair已有greedy、deadline、slack、regret2/3，换一个regret名称也不是本设计新增点。

034已精确计270200个source位置及分散C交付价格，仍固定198 native的owner和相对序。其最佳RELAY仍是i124/j166、A6/B4/C1/H2，J0.43636644384604817；source IN均在本机50个native事件后，drop分别81.57768335977975、80.49985659819978。这两source共50个native的交付clock与strong输入逐项完全相同：它们自身delta clock和deltaL为0，却决定了很晚的source availability。仅扩大“受损后缀”池无法有意选中这类前缀。

本设计的单一结构改变是：**因果prefix目录与真实donor slots选闭包，beam状态内联合决定whole任务owner和插入路线**。它不是新温度、更多普通ALNS轮数或再做四组静态assignment。

## 原始种子及来源分开

从已完成真实raw构造独立保两个RELAY通道：

1. 全部完整RELAY候选中的minJ，不要求pooled获救；034为上述0.43636644384604817。
2. 实际C路线的两selected负事件间确有native事件的候选中minJ；034仅2条满足，最好为i178/j190、A4/B6/C7/H4，N59、L1818.4321387399425、D583.7937707430543、J0.43746741350718904。

若同一方案同时占两通道则去重；不存在第二类时记缺失，不用标签伪造。两者来源及rawscore分别保存，不宣称分散种子更好，不强求选很差的pooled2种子，0 pooled rescue也允许。不得用旧+.015 gate或其它未经预注册门槛将已声明通道静默删除。

DIRECT从自己的完整候选及原strong中独立选起点，不被迫匹配RELAY的A/B/C/H、selected分工或raw finalmap。其post阶段所有200任务均可参与普通wholeDIRECT修复，pins为空；原强解始终作为最终guard。

## 1. 对当前真实路线建立因果前缀

RELAY只pin两个实际计划和既有A/B/C/H，保存当前全DAG时钟。目标包括source各自IN drop `a_i/a_j`、C无等待到H时刻`c`、实际第二take与selected第二次delivery `d_second`。

source目标的直接prefix是其IN drop前的原native事件；C arrival是首次OUT前native prefix；selected第二delivery还包括双take之后、该delivery之前的native事件。按真实route位置/当前DAG祖先索引whole任务：只要某native的一次事件在因果prefix中，整个任务都可作为候选，不只取已经完成或deltaL>0的任务。source/receiver自身已存在的pin事件不删除。

DIRECT没有a_i/a_j、H到站或C arrival目标，不能制造假中继clock。它以当前实际i/j最终delivery，以及同一候选政策下真实deadline-critical普通任务的最终delivery为target，沿这些任务**当前实际owner**的路线建立因果prefix；这里只决定排名目标，i/j也不成为pins。RELAY可额外使用其内生的真实a_i/a_j/c中间目标。两臂不得因此被强迫使用同一A/B/C、H或owner分工。

对每个有资格的native t，用真实whole任务移除生成partial preview，重放当前物理时钟：

`Delta_x(t)=clock_x(raw)-clock_x(remove_whole(raw,t))`，x取a_i、a_j、c、d_second。

所有target可从同一次partial global schedule取值；c按无等待物理prefix到H计算。preview缺一项最终任务，**不是完整计划**，其分数不能与200单J比较，也不是全局J/可达性下界；Delta只用于因果候选排名和覆盖证据。保留signed变化与数值容差，不把微小浮点负值任意改成证明。

若t正是某个DIRECT/native delivery target，移除后该target不存在，其Delta分量标为undefined，不能填0、伪造到达时刻或记为“已救回”。t仍可从其他真实影响、损伤或普通DIRECT候选政策进入U，之后整单修复时owner照常自由；target身份不构成隐含pin。

不能只按`Delta(max(a_i,a_j,c))`排序：另一source可能被当前较晚source遮蔽，单独移除不改变secondtake，却是联合缩短两边的必要成员。a_i、a_j、c及第二delivery各自的改善方向都应有有限代表；同时保留已有damage、长suffix和共载伙伴信息。结构索引/preview如有数量限制或到期，记录未评对象，不宣称穷尽。

## 2. 真正释放本机和外机责任槽

从eligible普通任务全集（RELAY198；DIRECT200 eligible）选择有界闭包U：以各臂实际因果prefix代表为核心，加入实际损伤/长suffix及载货伙伴，并为未打开的其他飞机真实移除至少一个wholeDIRECT donor。官方8机需先给这些donor预留名额，再截额外扩展；cap不足必须记actual opened aircraft，不能声称全8覆盖。donor排名可复用真实删除信用，但它们不是新资源。

设飞机a原完整最终数为K_a=25、pin最终责任数为r_a、移除U后还留在该机的普通任务数为n_a，则：

`q_a=K_a-r_a-n_a = U中原由a负责的whole任务数`，`sum(q_a)=|U|`。

q来自真实删除，不是可任意借用的新quota。RELAY的C有r_C=2，其余为0；DIRECT没有pins，r全为0。198/200表示可选全集，不表示一次把全部任务重排；实际closure cap、未纳入对象及actual q向量都保存。

当前split family中的k1/k2只是DIRECT donor参照，在RELAY raw中没有补偿移动。新接口不得把它们伪称compensation，也不构造假的四任务base满足旧repool auditor。

## 3. 动态owner＋路线联合beam

状态至少为`(partial_routes, current_global_schedule, present_task_ids, remaining_q, remaining_U, pin_signature)`。使用共同的固定next-task顺序；可把预声明的一或两任务作为一层。每层在状态内对所有实际`remaining_q[a]>0`的owner提出合法wholeDIRECT插入，选择后减对应q，而非查一个预定owner map。

`current_global_schedule`是**present_task集合上的物理DAG preview**：实际覆盖恰为present，所有present计划完整，RELAY两个pin的IN/OUT全部存在；缺失的只是尚未修复的U。必须独立核present计划/依赖图、真实等待和C2，当前final任务数<=25，且`remaining_q[a]=25-current_present_final_count[a]`、`sum(remaining_q)=|remaining_U|`。不能调用要求full200的标准validator后，把它报的缺任务invalid忽略并将结果标complete。此接口明确`complete=false`，只有U全恢复的leaf才调用标准full200验证。

两任务层需在剪枝前完成这两个任务：允许同机（该机至少2槽）或不同机、两种插入顺序，以保留拼载互补/交换归属；每个分支均在**当前路线**真实重放。独立单任务插入矩阵只能辅助排序，不可相加当联合J或安全剪枝。多机责任cycle由连续动态选择自然产生，不预先限制为forward/reverse/disjoint四个模板。

只有present集合相同的partial状态才能比较、按共享beamcap保留。partial N/L/D仅是该覆盖集合的排名，不拿缺任务的分数当完整改善或对原strong作J界。每层保存真实owner分支、q消耗、当前coverage和exact global clock，以证明不是旧“固定assignment换位置”的重命名。

全部U恢复、q全0后，才是complete leaf。完整重新验证所有200任务、final K25、C2及DAG/等待，再与原strong完整J作strict比较；任何invalid、超时未验证或未恢复覆盖的状态不能采用。原raw可以比原strong差，partial也可暂差；完成前不能以旧full margin替代最终严格guard。

## 4. Pin语义、DIRECT自由与证据

RELAY只锁i/j真实mode/legs/H、实际A/B/C、selected signed子序、exact relay set为这两件；C双take仍连续、进入空载、实际正共载飞行。其余native不锁原owner、原event顺序、原prefix或旧g/h位置，允许插在合法IN/OUT周围，actual source availability、等待和selected第二clock全部随post重算。不能复用raw的两段价格/旧window来证明新post。

DIRECT pins为空、全部200 wholeDIRECT均可删除重分配及改序，没有必须保的中继分工或去H要求；禁生成新relay，finalcount仍25。两臂采用同closure/动态quota政策、候选/beamcap和总预算，因各自状态不同可选不同任务，不强迫相同owner map。只给RELAY native自由、DIRECT仍锁两原任务的旧025设置不能称本轮强对照。fresh强DIRECT可把完整同预算用于自己的direct构造/修复，不必支付其不需要的RELAY目录构造。

独立proof分两段：先用真实base→真实raw完整split审计证明种子来源；再从raw→post只核实际pins（DIRECT为空）、完整实体/资源、全task current owner/clock/score及shape。旧split raw审计要求198native不动，不能原样冒充post自由198的审计；旧四任务repool provenance也不能拿假base适配。

raw、closure及partial均显式标scope，post保存全部actual final owners、当前source drop/take/delivery时刻与J、真实changed-owner集合。独立核q等于移除原owner数、partial恰好过滤U、每层同coverage、余槽实际减少、完整leaf全0；禁止把历史raw finalmap或k1/k2参照写成post实测责任。

## 5. 预算、数值和失败返回

本设计不预注册运行seed、leaf数或时间分配。未来协议需在运行前确定两臂相同总wall、候选/closure/beamcap与各阶段截止；实际raw构造、preview、repair、全验证、快照和hash都计费。

034完整构造与验证约28秒，不能沿用030的`0.3*40s` raw阶段上限而宣称当前目录在12秒完整重建。若总预算仍40秒，raw实际开销必须占用该预算，剩余时间再分给repair；若来不及，诚实回原guard。若改用既有034保存seed做额外continuation，须另记追加预算，不能隐藏其原构造成本后宣称同预算端到端胜利。

为隔离机制，可以另设两臂都付共同RELAY raw目录的匹配诊断，但须明标为匹配机制对照，**不是最强端到端DIRECT公平基线**。未来fresh端到端比较中，RELAY支付其实际构造与修复成本，DIRECT将相同总wall独立用于direct构造/修复；不能强迫DIRECT浪费约28秒计算不使用的中继目录后，再声称对照已充分强。此澄清不改变当前034已冻结协议或结果。

所有current route/global评估使用同一冻结weighted目标与EPS；源等待、C等待、native前后传播及selected实际路径全部计算。preview的浮点差只作排名，候选完整分数不允许用独立delta相加替代。每次昂贵调用前后检查绝对deadline，只返回截止前已验证的complete incumbent；original strong始终可用。

## 必要tiny与覆盖限制

未来实现至少验证：zero-damage native prefix确实进入目录并真实缩短a_i/a_j；被max遮蔽的另一source仍有代表；C arrival与两D间prefix分别处理；whole移除不切半任务；外5机donor真释放q及小cap未全开诚实记录；state内出现非四静态模板的新owner cycle；同机两任务拼载的联合代价与独立和不同；严格samecoverage层比较和q精确消耗；RELAY实际identity/形状不变、DIRECT零pins可改原i/j责任；raw/post独立分层、错误pin/新增relay/错误clock/容量/K拒绝；到期未完成不能采用，全部候选更差则回原strong。

这是有界原序可改变/whole任务可换责的联合修复，不是一次求解198任务全部分配的全域优化。preview和beam仍会省略路线，没有保证大幅胜出，也没有证明其他机制无效。验证重点是：是否真实触及未受损的到站因果prefix，以及owner是否真正在状态内被联合选择。
