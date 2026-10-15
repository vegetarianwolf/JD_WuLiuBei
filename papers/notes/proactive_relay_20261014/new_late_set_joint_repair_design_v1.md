# 强制新增迟到全集的中继普通任务完整修复

039真实证明同源两件IN和收端两件OUT共载可以净省航程，但三个保留中继渠道都拖晚了原本按时的105、160，且native修复的最优更新没有改变关键源机。新方法检验的是调度缺口：原native destroy强制至少两个owner，且不接收原guard，不能保证所有“原按时、当前迟到”的普通任务同时进入一笔结清的修复事务。它不是首次允许删除160，也不是首次使用whole任务、donor、联合回填或非单调搜索；030/035/036已覆盖相关原语，039也确实删过160。

双方共同起点升级为039当前加权最优DIRECT：60晚/140按时，L1718.952761分钟、D580.382954公里、J .433297274。025的141按时参考保留，它与039是加权取舍；不能让R只挑战较弱的025。D分类由实际共同起点和本次付费raw重新计算，不硬编码105或160，不免费复用历史中继候选。

## 完整事务及有限搜索

对每个实际raw group-count渠道，在typed joint context内重算完整原guard与当前raw的当前DAG时钟。完整新晚集合分为受保护group任务和普通whole-DIRECT任务D；受保护损伤单独报告，不删除group。普通D全部必须同时删除，不按损伤大小截断；若超过声明上限4，明确跳过而非冒称全体覆盖。

先做D每项原owner不变的联合重排，允许D全部同owner。真实缺口q由目标最终数量减当前present final-count计算。再从普通非D/pins且对至少一D存在不同owner的任务中，按min_D(P点距离加D点距离)、owner、task确定前4个donor。这只是有限几何提案，不是含等待的完整评分或界。

单donor逐个D目标交换真实owner；双donor枚举两个D目标、两个donor及两种配对，各donor返回对应D的原owner，其他D保原owner。全部槽都来自实际删除D与donor，不能让一项先免费跨到满额飞机。每个variant有固定完整assignment，先全部移除，再完整DFS回填。U不超过3件时枚举全部顺序；更大时四种确定顺序去重。所有任务D和donor补齐、q为0且完整验证通过，才允许按完整J比较或保留。未补齐的首项与任何partial J都不用于筛枝。

PD候选先枚举真实容量合法位置，过滤连续双take及joint连续双drop内部gap；按几何、早取和早交多渠道留有限位置，补充实际原位置重建分支。同owner控制代理前沿最多12位置，donor事务每步最多3位置；各允许额外至多一个去重后的实际原owner seed-anchor重建位置，因此每步总上限分别为13/4。每条实际分支都用当前global evaluate_delta及typed `_preview`，全部补齐后用标准force_full并校验当前完整DAG分数一致。有限位置/顺序/池会漏解，绝不称作最优、完备或下界。

保存完整输入、所有开始的variant实际删除partial、全D与受保护损伤、donor池/真实assignment/顺序、真实q和覆盖计数，以及保留完整解的插入trace、全部时钟/分数/组身份和期限。独立证明保存实体，不冒称未保存探测已逐项实体重放。

## 接入与计时

不复制原paid wrapper，也不修改旧ALNS五hook。薄context仅临时替换原`polish.polish_groups`，调用现有joint typed paid链。每leaf先付费focus，子期限为min(leafdeadline,start+min(20秒,max(0,剩余leaf wall)的40%))，外层min只收紧调用已过期的极边界，绝不延长leaf，然后把focus实际完整返回解交给原native helper。原native metadata/input/history/时钟全部原样保存，追加focused_stage；原native初始化过期时仍回raw/evaluationNone并skip，不能将focus cache伪装成native证书。原outer继续相对真实raw和共同强DIRECT全核、严格采用；所有context finally恢复。

独立callback单独绑定新增focus参数，再将明确剔除已证明新增参数的core manifest视图交旧joint paid proof。scope包裹native metadata证明：先由标准库和现有typed full/partial公共证明独立重建focus，再用真实focused input审计原native记录。outer complete_state仍相对raw。全部构造、分类、筛选、验证和context恢复付同一总wall。

DIRECT维持原独立all200自由的完整预算ALNS＋periodic＋conditional repair，无R目录或pin。两者温度和流程不同，且继承历史检查点；即使本次R更好，也只能是机制证据，不能替代最强调优fresh DIRECT、多独立种子和等端到端预算的最终优势验证。

本实现不改官方200任务、8机、C2、每机最终25件、速度0.9、四站或逐机home，不引入免费空飞/统计未来订单/中继奖励。当前加权J是研究偏好，不是官方另行规定的综合分。远胜目标仍需真实结果证明，协议不预设胜出。
