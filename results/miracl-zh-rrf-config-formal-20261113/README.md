# 正式 RRF 配置验证

参见 [完整报告](REPORT.md)、[逐题复算](independent-rescore.json)、[排名差异](ranking-diagnostics.json)、[实际模型用量](model-usage-summary.json) 和 [源码 PR](https://github.com/RSIXDB/hindsight/pull/108)。

已保存[与WeKnora的对照表](COMPARISON-WEKNORA.md)及[下一轮优化方向](OPTIMIZATION-DIRECTIONS.md)，并附[最新结果离线诊断](residual-diagnosis.json)。

已完成[当前权重配置的393题阶段诊断](../miracl-zh-weighted-stage-diagnosis-20261113/REPORT.md)：融合损失9个语义首位正确题、救回2题；109个最终首位错题的两路原始首位均未命中。

追加[0.95、0.99及纯向量消融实测](../miracl-zh-native-weight-ablation-20261113/REPORT.md)已归档；前述对比表仍保留其原快照与比较边界。
