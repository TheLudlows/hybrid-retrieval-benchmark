# MIRACL 中文：关闭模型重排的原生检索对比

用户于 2026-11-13 将本轮主比较改为关闭产品内部模型重排。此目录独立于[此前带重排实验](../miracl-zh-scale-20261112/README.md)，不混合模式、耗时或逐题排名。**10万段主实验已全部完成。完整复算报告见 [REPORT.md](REPORT.md)，结构化差值和核验见 [comparison-complete.json](comparison-complete.json)。额外配置组合验证单独记录，不改变本轮各6组的结果。**

**范围更新：用户已确认不做100万段验证，仅完成本轮10万段实验。原方案备份见 [protocol-revisions/before-100k-only.json](protocol-revisions/before-100k-only.json)。**

## 条件与关闭方式

- 仅执行10万段实验，复用相同的10万段语料、官方开发集393题、固定100题调参分组与额外200题；索引无需因关闭重排而重建。
- ContextBase 原生 Recall 使用 high，服务端及 Bank 的 `enable_reranking=false`，读取原生配置核验。其原生行为为 RRF passthrough，保留非模型的截断及 Token 预算规则。
- WeKnora Lite 原生 knowledge-search 显式发送 `rerank.enabled=false`，要求响应诊断 `outcome=disabled` 且未 applied，保留原生融合与上下文处理。
- BGE-M3 查询向量化继续执行。BGE-reranker 不参与本轮查询；模型服务可继续加载该模型，但不能实际调用它。每个正式查询必须有 Embedding 调用记录，所有预热和正式重排调用必须为零。
- 主配置保留60条原生融合候选/输出预算；它不再表示模型重排候选数。初始召回池仍使用产品原生规则：ContextBase high 原始预算1000，WeKnora单知识库原生超额召回300。不能将本实验描述为相同初始候选池的算法消融。
- 相同CPU、3线程ONNX Embedding、输入上限512、相同四次非计分预热、查询顺序和并发1。`runtime-readiness.json` 为共享模型服务此前读回，不代表已完成关闭重排验证。

## 执行与验证

旧执行器已停止，正在执行的 WeKnora 入库作为独立子进程继续，避免打断可能未返回内部 ID 的创建请求。新执行器等待此入库结束后，重新审计两端全部官方 ID、内容和映射，然后串行执行：

1. 每端先用独立、不计分的最多3题记录验证关闭重排、原生服务重启恢复与跳过已完成题。
2. 两端默认原生流程各393题，全部完成后再调参。
3. 两端仍各6组、每组100题；仅按预注册选择规则冻结配置，再验证393题、派生293题以及新增200题。
4. 40/100条预算和ContextBase返回Token预算试验单独报告，不参与选参。
5. **百万阶段已按用户明确要求取消。**10万段基线、调参与额外查询验证完成后结束，不再执行百万段入库、查询或容量检查；已生成语料和指纹保留。

报告 Hit@1/5/10、nDCG@10、Recall@10/100、实际返回深度、串行p50/p95/p99、成功数和模型用量。关闭重排的Recall@100仍受实际返回深度限制。开发集和额外train查询的局限同原方案，不作盲测承诺。

完整方案：[experiment-plan.json](experiment-plan.json)；[代码与方案指纹](experiment-plan.sha256)；[模式切换记录](protocol-change.json)。已有带重排结果和模型用量原样保留在旧目录，只作单独参照。

## 进度和恢复

入口为 `miracl_zh/run_scale_no_rerank.py`，复用原 `run_scale.py`、`ingest.py`、`bench.py` 和 `audit_index.py`。查询使用新增 `--disable-rerank`；跨模式续跑会拒绝配置不一致。ContextBase 单独使用该参数不能更改服务端配置，必须同时按上文关闭原生开关并核验调用。

当前环境启动命令（私有设置、凭证和索引不进入 Git）：

```bash
/workspace/work/api-venv/bin/python miracl_zh/run_scale_no_rerank.py \
  --settings /workspace/work/miracl-scale/runtime-settings.json \
  --results results/miracl-zh-native-no-rerank-20261113
```

持久状态仍在 `/workspace/work/miracl-scale/status.json`，原生索引和检查点仍在 `runtime-v2`；新日志在 `worker-no-rerank.log`，完整索引备份在 `backups`。入库用量与耗时只引用可核验的原始入库run ID，不把复用索引记成零耗时入库。若等待期间环境再次重启使入库进程消失，执行器会停止并明确报告；先按原README核对未决创建、使用原检查点恢复入库，再重新运行本入口。持久目录不保证进程跨环境重启存活。

新方案代码已做请求开关、关闭诊断拒绝、跨模式续跑拒绝和Recall@10计算的功能检查；真实原生重启/零重排调用验证须在完整索引上运行，以后续 `no-rerank-restart-preflight/verified.json` 为准。

## 完成后保留数据

按用户明确要求，实验结束后保留 ContextBase 和 WeKnora 两端的数据库、索引、原生文件、私有配置、入库检查点及备份，供后续查询和复测。已有1万段数据也继续保留。执行器切换产品时可以停止进程，但不会删除原生数据；不执行完成后清库或清理运行目录。只有收到用户后续明确删除指令才可删除。凭证继续留在工作区，不上传Git。见 [data-retention.json](data-retention.json)。
