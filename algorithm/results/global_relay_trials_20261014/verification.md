# 最终核验记录

- 完整回归：`MPLBACKEND=Agg PYTHONPATH=algorithm/src .venv/bin/python -m pytest -q -p no:cacheprovider algorithm/tests`，**984 passed in 46.24s**。在全部实测求解结束后运行。
- 实验独立审计：33 个唯一解全部合法、预算内，配对和晋级决策复核通过；原始凭据为 `validation.json`，原审计文件未为更新日期而重写。
- 三名子代理分别参与全局邻域、中继机制和文献检索；最终只读代码复核未发现阻塞项。
- 报告构建器核对输入、实测 ZIP、执行脚本、逐次解、审计绑定与历史均值。测后改动单列在 `final_source_delta.json`。
- 浏览器实际检查：1280 像素视口显示 17 张 SVG 图；主比较表 12 组配对、4 个详细场景。切换消融后变为 6 组配对、2 个中继场景，再切回正常。
- 窄屏检查：320×760 视口无页面水平溢出，SVG 标签无水平裁切；实际截图中数值和分类可读。表格通过内部横向滚动查看。
- 场景选择切换生效，浏览器无 JavaScript error 日志；临时视口已恢复。
- 排除原始 HTTP 响应存档后，`git diff --cached --check` 无错误；`papers/notes/global_relay_20261014/raw/` 中两个原始网页响应带有源端行尾空白，按取证字节原样保留。未重新运行或筛除任何算法实测样本。

完整回归曾发现两个历史测试把当前新增模块误当作旧版快照；fixture 现使用各自历史
manifest 绑定且校验哈希的 ZIP。旧审计脚本与篡改拒绝断言均未放宽。
