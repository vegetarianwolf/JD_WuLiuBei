# 用户追加30秒预算诊断

首批10秒官方配对全部退化后，用户显式选择两个新种子的30秒配对，以诊断启动时间。该批不是初筛晋级后的三种子验证，不用于替换默认。主预部署场景结果为 `63→98`、`72→97` 个逾期任务，0胜2负。

4次完整运行累计116.057秒，与首批共用540秒上限并预留30秒；合计188.391秒。使用相同旧、新源码ZIP和构造，种子为2026101931/2026101932，按AB/BA顺序。首批manifest/runs/decisions/validation的不可变指纹保存在本manifest，父批记录原样保留。

从仓库根目录复核，禁止改写父批的原始validation收据：

```bash
PYTHONPATH=algorithm/src .venv/bin/python -m algorithm.experiments.audit_joint_relay_extension --output-dir algorithm/results/joint_relay_trials_20261014/extension30
```

完整报告位于 [batch1/index.html](../batch1/index.html)，原首批页面保留为index.initial.html。重放时先在独立检出恢复首批归档执行脚本与新源码，运行新的首批并通过审计，再对其绑定的追加目录执行prepare/run；固定墙钟不保证逐位重现。不要覆盖本批证据或改写其父绑定来混用其他结果。
