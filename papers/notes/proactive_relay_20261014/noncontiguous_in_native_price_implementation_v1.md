# 非连续单IN前置真价：033实现合同v1

状态：NEW源码和tiny验证的实现合同，不是性能报告。只新增`split_feeder_catalogue_v3.py`、`test_split_feeder_catalogue_v3.py`和本文，不修改032、冻结设计、旧数学primitive、旧producer/auditor、官方数据或runner。依据`noncontiguous_in_native_price_design_v1.md`（SHA `591a52444d141ca1a11a9f813eb7ad06d41201e61d5b258e18ff2bda6d43feea`）。正式实验另由ROOT冻结，开发没有运行官方优化或试测官方运行时间。

## 不变的模型、API与共同对照

`build_catalogue`沿用v2字典接口及完整family/witness字段，默认seed2026101518，仍为family16/base64、feeder3、source3、一般source position8、table3、sigma0。两臂共用同一个无arm参数目录。原pureDIRECT强解、任务/home/原4H/8机、速度、C2、最终K25、scales和weighted J保持。

i/j原owner同为C，两个不同source A/B各增加一个完整IN，C接两个OUT且仍最终负责i/j。其余198任务保持原owner及signed-event相对序。IN不增加最终责任，source不删除补偿。donor k1/k2只用于DIRECT独立四任务12maps参照，在RELAY中原样不动。DIRECT仍独立same-owner全空gap×6合法PD块、donor12maps，不强迫去H或继承RELAY finalmap；原强解一直是采用guard。

## 在sourcecap3/feeder3之前完整覆盖的两gap域

原native route为`v[0:n]`。g/h均为**原gap**，g<=h；新source路线为：

`v[:g] + (+IN,) + v[g:h] + (-IN,) + v[h:]`。

实际pickup position为g、drop position为h+1。每次候选都要求原gap load L_g…L_h全部<=1；在某g向后枚举h时，遇到原load2即停止，之后任何更大h都跨过非法载荷区间。允许source在新pickup之前已载一个native。没有“最多跨4事件”的硬限；4只是冻结设计对当前输入的静态观察，tiny另用跨8事件验证。

g=h继续用一次完整`u→P→H→v`绕行，不能拆成两个同边detour。g<h分别计算原边g插P的deltaP和原边h插H的deltaH；h=n时deltaH只加开放尾段`u_n→H`。两者均在检查非负/有限后用于距离和时间；所有native wholeDIRECT仍保留。

所有合法位置的完整native价先计算，再以相对本机原native分数的`deltaN/deltaL/deltaD`作source及feeder排名。不同source之间不比较其绝对原N/L。位置渠道沿用真实weighted delta、earliest、最小native deltaN及selected期限方向，source3不足以保四个不同渠道时明确记录省略。没有二维ready/deltaD Pareto，也没有把position8移到新域前面。

统计分开记录`source_contiguous_positions_priced`、`source_noncontiguous_positions_priced`、`source_positions_priced_before_option_limit`、`source_positions_not_retained`、`source_two_zone_native_queries`及各级省略。5600/93800等官方计数仍须以正式保存元数据为准，tiny结果不替代正式运行。

## 稳定middle+tail价格与实际ready

g<h时，原native delivery位置r<g原样，g<=r<h增加sP=deltaP/V，r>=h增加s12=(deltaP+deltaH)/V。计分只在`(clock-due)>1e-9`时同时计N和整个lateness值。

实现不作三个大型完整suffix分数相减。对每条原native route缓存：

- middle索引：原g前N/L原样，加上delivery位置[g,h)集合的有序lateness索引；
- tail索引：delivery位置>=h集合，prefix强制为0。

分别查询middle(sP)与tail(s12)，相加得到完整source native N/L。空middle、已在g前载货但g后才交的native、h=n空tail都按delivery事件位置处理。D为原整条source D+deltaP+deltaH，IN负事件只记录dropavailability，不加final N/L/K。

每g从原gap前真实clock飞P，然后按原事件g…h-1的真实坐标**逐flight累加**，再飞H得到ready。相同g可逐h增量推进，不使用`原h前clock+sP`代数值冒充实际clock。每个保留seed还完整`route_cost`回放。因此即使source native全离deadline很远，其ready的加法路径也不会被忽略后传给C。

## 浮点守卫与回放路径

middle/tail查询各自用`x+shift>EPS`二分及有误差估计的正和；实现不把它改成`x>EPS-shift`。合并时把两段sum误差、最后加法ULP以及受影响native任务数乘时钟误差余量共同计入一个1e-10 min预算，不能两段各用完整预算后直接宣称总误差合格。

代码中时钟余量使用真实原/新路线时间量级、due及ready的最大值scale，取`(2*n+16)*ulp(scale)+8*(ulp(sP)+ulp(s12))`，同时扩充EPS邻项边界带。其用途是对正flight累计、detour差/除法及重排提供保守运行守卫；它不宣称任意IEEE规模下bitwise相等。非有限、边界敏感、sum误差超限或几何roundoff时，必须在source/feeder截断**之前**完整回放真实新route并使用actual N/L/D和availability。保存完整early布局还核对其价格与实际fleet N/L/D；最终采用仍由独立global/canonical guard决定。

delta出现负数时，只允许64×ULP相关距离尺度内的tiny negative校正0，并强制完整回放；实质负数拒绝，保存显式roundoff flag。没有把真实负delay当作合法uniform模型。索引primitive内部Deadline转换为共同`costs.Deadline`，所有遍历、索引构造、回放、表和hash均使用同一绝对deadline；到期不返回共同目录prefix。

C路径保持032：先删除i/j并从原home重计native skeleton，全空gap×两个delivery顺序计native等待/selected交付。selected自己的EPS带也在前池前完整C回放；native/selected物理回放仅按实际(ai,aj)缓存，不能复用另一相同max-ready而不同两到达时刻的浮点路径。未修改C模型或其候选上限。

## 原gap、实际位置与seed的可审证据

新增`pricing_scope='complete_two_gap_IN_and_OUT_block_model_before_base_pool'`。每family的weighted和pooled early证据均包含：

- source的兼容`gap=g`、`drop_gap=h`、`original_pickup_gap/original_drop_gap`、实际`pickup_position=g/in_drop_position=h+1`；
- `layout_type`，分别为contiguous或noncontiguous；
- `pickup_detour_distance_km/drop_detour_distance_km`、`middle_native_shift_min/tail_native_shift_min`及总`delta_distance_km/native_shift_min`；
- 实际drop、完整source native N/L及整route D、时钟余量与物理回放/roundoff bool。

连续情形middle集合为空，pickup_detour字段存**整个连续块**的detour，drop_detour=0，tail shift为总shift；不可把这两个字段误解为两条原边。非连续字段才分别对应两条真实边。独立审计从truebase重建分区，不以自报float flag替代物理分数。

family source表额外显式保存原双gap及layout type，原实际position字段照旧。seed cache和去重包括g/h两维，不会把同g不同h误并。每family两个early exact source布局在同sourcecap3内强保，权威表仍是family表；顶层为明确inspection union。一般position8只补余槽，不能重建时丢掉新early g/h。

两个完整early布局真实进入候选评分池，然后与一般完整表一起用weighted/minN/pooled三渠道保最终witness<=3；不强留early而挤掉minN。`early_price_model_replayed`、真实source保留、进入评分池、最终同route是否保留及省略理由继续分开记录。完整early/final witness保存全部fleet路线、真实clock、delivery、source可用时刻和实际全分。

## 定向tiny与验证边界

新增tiny含：方形native沿途carry零detour改变feeder_limit1排名；连续真价遗漏该机会的反例；全部g/h价格逐一与真实route对照；原已有载1；端点合法但中间load2和drop-gap load2拒绝；h=n；空middle/已迟到prefix；连续同边专用；native/selected-only EPS、两段误差超限回放；实际ready在speed0.9和超过静态4事件的路线；primitive内部到期转换；两early真实入source表及完整pool；新独立v3审计跨端与共同hash。

这些是6/8/9任务合成检验，没有读取官方CSV生成候选、运行官方优化或长测试。最终SHA与真实测试命令/结果由开发交付记录另列。本实现仅扩大原事件相对序固定、pureDIRECT source的前置计价域；不是所有relay路线或全域J界，不保证正式实例改善。
