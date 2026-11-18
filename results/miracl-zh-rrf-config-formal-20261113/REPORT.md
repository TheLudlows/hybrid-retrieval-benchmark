# RRF 权重正式配置落地验证

已完成正式配置实现、100题默认权重对照及393题配置复跑。正式版393题的全部逐题质量指标与此前实验版一致；完整返回列表并非全部逐项一致，详见下文。

实现：[ContextBase PR #108](https://github.com/RSIXDB/hindsight/pull/108)，提交 [`cdf6430`](https://github.com/RSIXDB/hindsight/commit/cdf6430e028ff456d490436ff99e50fcf4aba59c)。PR 保持草稿，未合并或替换原服务。GitHub Extension/brand 检查因账户付款/消费额度限制未启动，主 CI 另显示 startup_failure，不能宣称远端 CI 通过。详见 github-ci-blocker.json。

## 实现与验证范围

增加四个正式配置字段 `recall_rrf_semantic_weight`、`recall_rrf_bm25_weight`、`recall_rrf_graph_weight`、`recall_rrf_temporal_weight`，支持全局环境变量 → 租户 → 知识库覆盖。原生 RRF 使用 `weight / (60 + rank)`；四路默认值均为1，拒绝0、负数、NaN、无穷和布尔值，知识库覆盖值设为 null 时恢复父级。权重同时传给原生融合、策略优先级候选截断和存储扩展请求；检索 trace 可核验生效值。Interleave 与模型重排后的加成保持原有处理。

没有引入繁简转换、停用词改写、外部排序或新模型。0.97/0.03只用于本次复核，未设为产品默认值。369项针对性回归测试、18项真实API/Worker的SDK系统测试、全仓lint与类型检查通过，提交前完成代码审查。检查脚本同时整理了已有格式、未使用变量、BOM及环境模板同步问题。

## 数据与条件

- MIRACL v1.0 中文开发集，既有100,000段子集，种子20261112；393道查询、3,928条标注均保留。固定原子集及查询顺序，不重新选参。
- 压缩语料 SHA-256：`d51e1fe9ec6ec92db05df87aabff132f861a72b046a7713bc945f18746665363`。
- 从保留备份恢复独立数据库，逐段核验100,000个官方ID、原文和向量覆盖，缺失/额外ID均为0；复制并重新绑定独立检查点。未修改原库。
- BGE-M3版本 `25b9af8e87a38eb120cfe87125383677b9cd309e`；保留BGE-reranker-v2-m3版本 `6f5ff65298512715a1e669753bc754d2bc8f367b`，本轮关闭模型重排。ONNX INT8 CPU，单查询并发1，线程3/1，截断512，模型/Tokenizer文件SHA均重新核验。详见 environment.json、model-info.json 和 model-fingerprints.json。
- 原生Recall high=1000，各路入融合前上限100，融合候选上限60，返回Token预算8192；向量与jieba BM25均启用，图/时间支路关闭。原始查询直接用于Embedding。
- 每次采集前4次预热均排除。默认对照先完成4题，重启独立API，再跳过已完成题续跑到100题；配置不变。该对照共8次预热，正式393题为4次。

## 实测结果

| 阶段 | 成功数 | Hit@1 | Hit@5 | Hit@10 | nDCG@10 | Recall@10 | Recall@100 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 默认权重1/1，固定100题 | 100 | 42.00% | 80.00% | 95.00% | 0.596384 | 82.93% | 100.00% |
| 正式配置0.97/0.03，全部393题 | 393 | 64.89% | 93.64% | 99.24% | 0.776885 | 95.41% | 99.83% |

正式393题检索p50 **680.39ms**、p95 **876.30ms**、p99 **998.73ms**。平均返回 **54.73** 条，范围 **38–60**，没有请求返回100条；Recall@100按实际最多60条计分。

本轮未重新入库或计算语料Embedding，不把备份恢复算作入库耗时。历史入库记录在此前原生无重排报告中保留，本轮不重报为新测耗时。

## 模型实际用量

| 阶段 | 查询Embedding调用 | 查询Embedding输入Token | 查询重排调用/Token | 平均模型重排候选 |
| --- | ---: | ---: | ---: | ---: |
| 默认100题 | 100 | 948 | 0 / 0 | 0 |
| 配置393题 | 393 | 3795 | 0 / 0 | 0 |
| 合计正式请求 | 493 | 4743 | 0 / 0 | 0 |

入库Embedding本轮新增调用/Token均为0。另行归档12次预热及2次trace探针，不计入质量或正式查询耗时。输入Token取自实际模型日志；逐题ID、调用数与排名记录已核对。

## 与实验版的逐题核对

- 默认100题：Top1、Top5、Top10完整列表分别有 **100、100、99** 题相同，全部返回列表 **84/100** 相同；100题的全部逐题质量指标一致。
- 正式配置393题：Top1、Top5、Top10完整列表分别有 **393、393、393** 题相同，全部返回列表 **392/393** 相同；393题的全部逐题质量指标一致，六项总体质量差值均为0。
- 这次使用逻辑备份恢复的独立数据库，恢复过程重建了ANN/BM25索引；上述较深排名差异已保存为逐题诊断，不能将其称为完全复现全部排名。正式配置的数学公式、默认行为及优先级组合另由确定性测试和完整trace公式复算验证。
- 两次探针每个融合条目的加权公式均核对通过，BM25实际返回候选；查询向量哈希与实验版完全一致。

本轮是ContextBase单端配置落地复核，不是重新进行同等调参预算的两产品公平比较。此前已见过这些查询，不能当作盲测；原版ContextBase/WeKnora对比报告保持原结论。结果仅适用于本次10万段子集、冻结配置与实测环境。

## 复跑与保留状态

运行本报告的历史命令前，请按[MIRACL 复现说明](../../miracl_zh/README.md#其他复现入口)检出整理前的固定版本。

先按既有README完成100k数据和原生入库，保留私有runtime-settings.json及备份。使用本目录contextbase.patch或上述提交准备源码，按源码仓库的本地启动指南安装依赖；Python参数指向安装该版本依赖的解释器。下面的私有工作目录必须在Git仓库外：

```bash
python miracl_zh/prepare_rrf_config_validation.py \
  --settings /private/runtime-settings.json --work /private/rrf-validation \
  --source /path/to/contextbase --backup /private/contextbase-100k-index-ready.dump
python miracl_zh/verify_rrf_configuration.py \
  --settings /private/runtime-settings.json --work /private/rrf-validation \
  --source /path/to/contextbase --python /path/to/venv/bin/python \
  --results results/rrf-config-local
```

恢复脚本只允许专用数据库名称，已有同名库须有匹配的私有所有权标记；原库和两端原检查点不改写。Docker小共享内存下应使用串行HNSW维护，脚本已设置；先前恢复尝试的共享内存故障在独立副本中解决。采集持续fsync逐题结果，同样参数重跑可续跑未完成题。API/查询故障细节写入私有工作目录日志及execution-status.json。

原ContextBase服务仍运行原版代码并健康；WeKnora数据库、两端原备份/检查点均保留。独立验证API在完成后停止，独立数据库、私有运行配置和索引也保留。凭据未提交。
