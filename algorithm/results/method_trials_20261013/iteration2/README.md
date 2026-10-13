# 独立追加：跨路线相邻串移除

[离线仪表盘](index.html)仅比较本批与原 main `2451101` 的配对结果。
用户选择继续查文献、迭代新方法；本批不混合首批缓存与降温的胜负。

8 次完整运行，耗时 72.024 秒；连同首批累计 187.429 秒（3.12 分钟），
共享 900 秒上限及 30 秒审计保留。全部从官方 CSV 独立完整复算，合法且预算内。

| 集群种子 | 基线逾期 | 候选逾期 | 词典序判定 |
|---:|---:|---:|---|
| 2026101601 | 77 | 78 | 负 |
| 2026101602 | 77 | 77 | 胜，总逾期减少，航程增加 |

单机两配对均持平。候选一胜一负，未过全部初筛配对不退化门槛，因此没有启动
30 秒验证，`enable_multiroute_string_destroy` 保持默认关闭。

实现新增一个独立加权的破坏算子，在两条空间邻近路线中均衡移除连续取件任务串，
同时删除完整取送对；保留其余破坏、修复、接受与完整历史最优评分。
只作用于多机 DIRECT。[实现说明](implementation_notes.md)与
[文献阅读](literature/reading_notes.md)明确了作者技术报告与项目适配的边界。
本实现未包含 SISRs 的随机跳过插入，不能称为完整 SISRs。

25 任务单机和官方 200 任务 / 8 机；两种子各 10 秒初筛。同种子、同初解、同预算；
阶段内轮换 AB/BA 顺序，构造计入预算并预留 1 秒收尾。全部运行保留完整解，
未重跑择优。三个预定 30 秒验证种子只有通过初筛才运行。

父批 manifest、runs、decisions、validation、源码与解文件绑定到本批，禁止修改。
`validation.json` 的 `total_seconds` 是父子共享耗时，`batch_seconds` 为本批耗时。
只有本批结果用于本批判胜；累计时间是资源记账，不增加统计样本量。

重建报告不启动求解：

```sh
.venv/bin/python -m algorithm.experiments.render_method_trials_report \
  --output-dir algorithm/results/method_trials_20261013/iteration2
```

重新审计：`PYTHONPATH=algorithm/src .venv/bin/python -m algorithm.experiments.audit_method_iteration2`。
若第三批已绑定本审计，应在副本运行审计，避免时间戳改变冻结文件指纹。
重放须在独立 checkout 恢复 `sources/` 中候选模块与归档脚本，使用新的空结果目录，
依次执行 `run_method_iteration2 prepare` / `run`；父批证据目录同时保留。

后续 [相邻访问微调实验](../iteration3/README.md)继续使用新种子、新对照和独立评分。
这些短预算、小样本观察不证明最优性、分钟级效果或跨实例稳定性。
