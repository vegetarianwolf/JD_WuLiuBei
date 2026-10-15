# 当前最好：算法与结果（2026-10-15）

本目录明确标注本会话截至 batch040 的**加权总分最佳已审计正式终解**。它采用固定四站预部署后的纯直送，实际中继为0；“最好”按当前研究加权 J 定义，不表示已证明全局最优。

| 文件 | 用途 |
| --- | --- |
| [BEST_ALGORITHM.py](BEST_ALGORITHM.py) | 最佳结果采用的核心算法模块，逐字复制原冻结源码 |
| [BEST_RESULT.json](BEST_RESULT.json) | 最佳完整结果：8架无人机路线、200单责任分配和全部送达时刻 |
| [BEST_ALGORITHM_MANIFEST.json](BEST_ALGORITHM_MANIFEST.json) | 算法参数、种子、输入、完整冻结依赖和审计的路径与SHA256 |
| [REFERENCE_141_ON_TIME.json](REFERENCE_141_ON_TIME.json) | 保留的141单按时参考，供按时单量优先时比较 |

| 已审计正式终解 | 按时单量 ↑ | 逾期单量 ↓ | 总逾期/min ↓ | 航程/km ↓ | 加权 J ↓ |
| --- | ---: | ---: | ---: | ---: | ---: |
| **当前加权最好：batch040 DIRECT** | 140 | 60 | 1716.285265 | 573.996325 | **0.431089741253357** |
| 按时更多的保留参考：batch025 DIRECT | **141** | **59** | 1790.632129 | 578.534685 | 0.434215788787754 |

J = 0.5 × 逾期单量/200 + 0.25 × 总逾期分钟/4464.183540734935 + 0.25 × 航程/775.7731679286865。这是本研究的偏好权重，不是题目官方综合分。

算法为加权 ALNS、惰性修复、完整任务末端/周期互换，以及普通任务完整联合修复；采用完整可行性和强直送终解保护。核心模块名为 `multi_relay_direct_control_v1`，其实际方法名为 `Independent-Weighted-DIRECT-ALNS-And-Complete-Exchange-V1`。

`BEST_ALGORITHM.py` 是核心模块，不是独立CLI。它需配套[原始冻结执行器及所有模块](../results/proactive_relay_diagnostic_20261014/batch040/sources/)、其中的 `weighted_solver.zip`、[原运行manifest](../results/proactive_relay_diagnostic_20261014/batch040/manifest.json)和manifest绑定的历史checkpoint；复制到本目录没有改变算法，也没有修改公共默认入口。完整依赖逐文件哈希已写入 `BEST_ALGORITHM_MANIFEST.json`，重现实验应使用冻结环境并写入新结果目录。

最佳终解来自历史强解的继续搜索：batch040追加预算上限240秒，实际195.682509秒，种子2026101527；此前构造和追加搜索预算另外保留，不能表述为从零240秒获得本解。按时单量与综合分存在取舍，因此保留141单参考，不将140单宣称为每项指标都最好。

原始[HTML报告](../results/proactive_relay_diagnostic_20261014/batch040/index.html)、[独立审计](../results/proactive_relay_diagnostic_20261014/batch040/audit.json)、[完整运行记录](../results/proactive_relay_diagnostic_20261014/batch040/runs.json)及[已提取的结果与停止凭证](../results/extracted_best_pure_direct_20261015T012805Z/README.txt)均保留。本次仅命名、归档与上传；`/goal` 仍暂停，没有新求解。
