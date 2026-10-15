# 同源联合双件接力接入多组与普通任务修复

038已经真实放开普通任务：三个渠道换机4/9/21项、内部best更新2/13/17次，仍无原强解采用。
一、二组关键source8与第二take未提前，C更早到站只增加等待。继续重复同一native ALNS
不能检验一个更省路程的运输骨架。本设计只改变新组的收货骨架，不改变任务、资源、
原四站、逐机home、截止期、当前J或原完整strong-DIRECT采用门槛。

## 既有能力及缺口

001已有同源连续两IN，010已有同源分散pickup/联合drop及同C两件配送，015、cluster和
生产plan邻域也已有单件/同源可达动作。025/028已有native自由，037已有多组暂差beam。
这里不声称同源双件、接力、ALNS或非单调是新原理。尚未接通的是：同源联合双件作为
正式完整group，由当前同C任务对重新选择，在多组当前DAG中累积，再接generic native
自由修复，并由独立审计证明两端真实共载。现037分支硬认三不同飞机，直接令a=b会
覆盖第一条source替换，不能当作联合构造。

## 运输结构与有限提案

新组保持i<j、a=b=S、S≠C、原H，显式source_type=joint_pair；两件最终责任仍为当前
C，不因IN占用S的最终配送配额。父组可兼容原split型与joint型；本次新增提案只产生
joint型，隔离这一结构因子。每次选择当前未中继、同最终C的whole-DIRECT任务对。

S的一条实际联合路线同时插入两IN：第一P插在原gap g，保原g..h事件且其原载荷≤1，
在原空gap h接第二P，然后赴H连续卸下两件；枚举两个pickup顺序，要求第二P到首次
卸载前有正距离双载。两个IN不能由相互独立的route替换拼成。C保原空载连续双OUT
取件、正距离双载，允许首D和次D之间穿插原native事件。所有既有group的实际计划、
角色/H/leg和selected子序列保留；source联合双drop及receiver双take不可被拆开。
每个完整状态仍由standard force_full和当前完整global DAG重算全部新旧等待与环。

有限目录沿用原参数接口和cap：所有实际候选S先用联合几何/真实parent-clock代理，
再保source cap3；按站geometry/estimated-take渠道保base64并round-robin family16，
source位置12/options3、receiver options3。代理只排名，不是完整J或下界。联合source
只占一条真实source_layout，family保存真实source_joint_profile；候选routesSHA从
真实parent加联合source/receiver布局重建。所有准备、筛选、hash和检查均付同一期限。

## 复用及独立证明

新增structure模块提供原shape/signature/propose_catalogue兼容接口。一个小wrapper
只在非嵌套的进程内上下文替换原raw/native helper/outer wrapper的三个structure引用，
finally恢复并验证；复用原构造、快照、分层seed渠道、native五钩子和strict原guard。
原core时钟保持原义，extension上下文时钟绑定worker付费phase，绝不额外增加时间。
原canonical handoff字段本来可表达同源两件的实际drop，保持该载体；独立typed证明
额外重建source的注册IN载荷积分、连续卸载及正共载，不依赖producer的分数或几何。

独立模块复用既有raw状态/亲子/guard、native完整与partial q、outer采用链证明，
只替换typed descriptor、联合source布局/目录和两端形状检查。所有post和partial都
必须继续证明source真共载，不能只在raw初始化检查。上下文不可嵌套，异常/期限后
所有引用恢复；D使用原空pin、all200自由的独立完整预算控制。

现有ledger继续用同一对arm、manifest、phase、receipt、CF和audit循环。协议可以
选择已实现的method_modules与对应metadata_auditor，两者必须匹配且源码全部冻结。
不用另一套批次框架或新一组载体别名。此设计尚未注册下一官方seed或预算。

## 可检验条件与结论边界

若S本来顺路经过两P、可合法捎货，且H接近其后续路径，C省去两P绕行，源端与配送端
都可能通过双载减少总航程；joint availability也可去掉另一来源机的max等待瓶颈。
P分散、H不顺路或S原任务受拖延时仍可能变差，必须让完整N/L/D/J决定，不能加接力
奖励或强行采用。旧解的受限反事实不是最强DIRECT：最终性能仍需fresh同资源、同
加权目标、同端到端预算和固定多种子对照。当前远胜目标未达，任何tiny或机制改善
都不能代替该证据。
