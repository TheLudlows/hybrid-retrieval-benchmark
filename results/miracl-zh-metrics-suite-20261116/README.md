# 知识库指标补全：中文原生检索、性能、资源与可见性

本轮已完成：**1986/1986次正式查询成功，60次合成文档操作核验通过**。见[完整报告](REPORT.md)、[独立复算](independent-verification.json)及[数据保留核验](retention.json)。两端393题串行完整排名分别与此前归档一致393/393题。

按用户确认的指标执行。用户随后选择不使用生成模型，本轮不评测最终答案正确率，不把检索相关性冒充答案正确率。

并发下质量存在波动。事后[模型批次诊断](embedding-batch-diagnostic/independent-verification.json)确认共享ONNX INT8模型服务的输出受批次组合影响；并发吞吐应结合各轮质量一起解读，不能称为完全无损扩展。没有修改已测模型设置或用并发最好分数替换串行主成绩。

- 质量：nDCG@10、Recall@10、MRR@10、Hit@1，以及Precision@5/10、未标注率；保留Hit@5/10与Recall@100。
- 性能：同期393题串行耗时；固定100题负载的并发1/2/4、两轮重复，记录p50/p95/p99、成功QPS、成功率、错误/超时及实际返回深度。
- 成本：引用可核验历史入库时间及Embedding用量；新增查询Token/调用次数实测，采样API/共享模型/数据库RSS、CPU，记录原生数据库体积。
- 更新：独立副本中10个临时合成文档，分别测新增、替换和删除到原生检索可观察的延迟；原始语料和备份保留。

同一MIRACL v1.0中文10万段、393题，语料SHA-256为`d51e1fe9ec6ec92db05df87aabff132f861a72b046a7713bc945f18746665363`。保持BGE-M3 ONNX INT8 CPU、3线程、HTTP批处理窗口10ms、模型重排关闭；不追加选参。

ContextBase使用独立恢复库、特性源码cdf6430、权重0.985/0.015、high=300、源上限100、候选上限60、返回Token上限8192。WeKnora在新SQLite快照中使用原生knowledge-search、k30、向量/关键词0.7/0.3、候选60。原服务不切换源码或配置。ContextBase此前经过额外调优，本轮同期复测不能消除调参投入差异。

## 证据和进度

- [冻结执行方案](experiment-plan.json)、[执行状态](execution-status.json)。未完成项目没有成绩。
- [已有393题质量复算](QUALITY.md)、[完整质量比较](quality-comparison.json)，两端逐题指标在quality目录。
- [同期393题质量](current-quality-comparison.json)、[两轮并发结果](load-comparison.json)、[资源与数据库体积](resource-comparison.json)、[可见性结果](visibility-comparison.json)。
- [历史入库成本](historical-ingestion-cost.json)：不是本轮重新入库的耗时。
- 新查询、实际模型调用与资源采样在runs目录；合成文档的逐次操作记录在visibility目录。
- storage.json保存可见性测试前的原生数据库体积，最终四份审计核对两个副本和两个原库的官方ID及正文指纹。

吞吐以连续负载窗口内的成功请求数/墙钟秒计，窗口包含派发与结果落盘。请求耗时从工作线程开始调用原生API计时，包含失败请求，排除线程池排队与预热。100题负载仅代表该固定工作负载，p99只有有限样本。两轮产品顺序与并发顺序反转，闲置的对端API及PG保持相同运行状态。

模型日志使用阶段run ID归属实际调用。并发时保持全局测量上下文不变，stage-batch不是逐题关联ID；不伪造逐题模型调用对应关系。合成文档的异步写入与检索探针模型开销合计单列，不强行拆分。

资源以0.5秒间隔采样；进程树RSS之和可能重复计算共享页，PG是整个实例口径，共享模型单列，不将这些数据冒充单Bank独占内存。更新延迟为首次外部观察值，包含探针检索耗时和轮询间隔，不是内部提交时间。

## 复跑

运行本报告的历史命令前，请按[MIRACL 复现说明](../../miracl_zh/README.md#其他复现入口)检出整理前的固定版本。

复用此前独立ContextBase恢复库及私有运行设置，并保留原始两端100k索引。将本目录实验方案复制到新的结果目录，按冻结版本执行：

```bash
python miracl_zh/extend_retrieval_metrics.py \
  --data /path/to/miracl-zh-100k --results /path/to/results
python miracl_zh/run_metrics_suite.py \
  --settings /private/runtime-settings.json \
  --private-work /private/metrics-runtime \
  --results /path/to/results
python miracl_zh/analyze_metrics_suite.py \
  --data /path/to/miracl-zh-100k --results /path/to/results \
  --measurement-dir /private/model-measurements
```

若需要复核本轮事后模型诊断，在完整查询窗口结束后运行`diagnose_embedding_batches.py`，参数同分析脚本，再运行分析脚本汇总。诊断不改变模型配置，额外模型调用单列；它是事后4例受控检查，不属于原先注册的14个负载窗口。

服务恢复与本机绝对路径沿用本仓库此前的固定环境，运行器会核验脚本/模型/语料指纹；私有工作目录需要保留model-pid.json所标识的同一模型服务，API地址和密钥不入库。`--publish`仅用于明确要求提交评测分支时启用。

完整阶段可直接跳过。不完整的计时窗口保留在attempt目录，连续吞吐需要单独的新窗口，不能把中断时间拼进QPS。未决合成文档操作保留journal，先核对它自己的ID再恢复；绝不删除原始官方段落。

`metrics.json`沿用原评分器的`serial_p*_ms`字段名；在并发阶段，它表示该负载下单请求延迟分位数。汇总文件使用`request_p*_ms`命名，不把并发耗时当作串行耗时。
