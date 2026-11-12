# MIRACL 扩容、差异归因与预注册调参

本轮承接 [已完成的 10,000 段、双边各 393 题实测](../miracl-zh-10k-20261112/README.md)。本目录记录已完成的差异分析、冻结的实验方案和后续阶段结果。**10 万/100 万结果须以对应目录实际完成的逐题文件为准；语料已生成不代表索引或查询已完成。**

## 已完成：原结果差异归因

原 10,000 段候选池下，ContextBase 有 117 题首位未命中但前十含正例，WeKnora 为 132 题；两端均无前十完全漏掉正例的题。ContextBase 仅有 2 题未取全官方正例，共缺 2 条；WeKnora 为 6 题，共缺 6 条。因此当前主要可观察差异是排序，Hit@10 已饱和，扩容用于增加干扰段落并验证差异是否保持，而非预设必须扩大优势。

逐题配对 nDCG 差值 C−W 为 0.021517。固定种子、10,000 次 query bootstrap 的 95% percentile 区间约 [0.00846, 0.03462]。Hit@1 的不一致题为 C 独中 33、W 独中 18，未校正多重比较的双侧精确 McNemar p=0.04887。这些都是已经检查过的开发集上的探索性统计；query bootstrap 假设题目独立，不能当成新盲测证明。

已对 11 道分歧/漏正例查询采集 ContextBase 原生 trace，得到两个不同断点：

| 查询 | 官方正例 | 原生证据 | 可检验方向 |
| --- | --- | --- | --- |
| 西班牙足球获得过几次欧洲杯冠军？ | `4811584#17` | 语义支路第10、BM25第501，未送入60候选重排 | 召回池/融合/支路优先级 |
| 立陶宛在哪一大洲？ | `5476290#5` | 语义第26、BM25第2，已送入模型但最终未返回 | 最终返回预算/过滤，不能算作纯召回失败 |

所检查 trace 的新近度和时间分数均为 0.5 中性值，因此不把关闭新近度衰减当成预期增益来盲扫。仅凭最终排名无法定位未返回正例在哪一阶段丢失，故保留原始压缩 trace 和逐正例阶段证据。

数据：[完整分析](analysis/ranking-analysis.json)、[逐题案例 CSV](analysis/ranking-cases.csv)、[原生阶段证据](analysis/native-traces/summary.json)。trace 耗时不进入正式性能对比。

## 冻结的语料与查询

- 使用同一官方完整语料版本、相同 seed=20261112，建立 **原1万 ⊂ 10万 ⊂ 100万** 的段落集合；扩容保留父集全部 ID 和正文。
- 两个扩大语料均保留原开发集全部 393 题、3,928 条标注和 3,786 个已标注段落。
- 固定新增 200 道官方 MIRACL train 查询，保留其全部已标注段落以便额外复核。它们未参与本轮调参，**不是官方盲测集，也不保证预训练模型未见过**。
- 原开发集按 `SHA256(20261112:qID)` 排序，前 100 题用于调参，余下 293 题用于探索性评估。393 题此前均已看过结果，293 题不得包装成全新盲测。
- 新增查询按同样哈希顺序选取，并对开发集及已选新增查询做 NFKC、casefold、空白折叠后的精确去重。初选曾有 3 道与 dev 文本重复，已在任何扩大规模正式跑分前替换；不做语义去重的过度承诺。
- 原初版嵌套语料和各16段启动预检状态保留在工作区；正式使用独立 v2 数据/数据库/运行 ID，不把初版预检调用混入正式用量。

查询分组：[query-splits.json](query-splits.json)。新增题及原始来源指纹：[topics](fresh-topics.tsv)、[qrels](fresh-qrels.tsv)，来源文件 URL、版本和 SHA-256 在 query-splits 中。构建规则新增 `--include-subset` 和 `--extra-passages`，继续复用原 build_subset/verify/ingest/capture/score 实现。

语料指纹和层级校验：[10万](100k/corpus-fingerprint.json)、[100万](1m/corpus-fingerprint.json)、[父集 ID/正文校验](nested-corpus-validation.json)。额外复核题的段落保留名单：[fresh-required-ids.txt](fresh-required-ids.txt)。两个产品使用相同的最终文件。

## 预注册的执行顺序与公平预算

1. 先完成 10 万段 ContextBase 原配置入库、审计和 393 题查询，再完成 WeKnora 原配置同样流程；两个默认基线都完成后才开始调参。
2. 两端各注册 **6 组配置、每组相同的100道调参题**。主比较始终重排上限60，保留向量+全文混合召回和实际 BGE 模型重排。
3. 每端仅按调参100题 nDCG@10 选配置，其次 Hit@1，再其次预注册顺序；完全相同优先默认配置。选择冻结后再跑完整393题和新增200题，不根据后两者重新选择。
4. 从完整393题排名派生固定293题指标，标明来源、不额外发查询；它是探索性评估。
5. 单独对选定配置执行 40/100 候选预算实验（仍为同一100题）；另对 ContextBase 测 `max_tokens=16384`、重排仍60的返回预算实验。这些均不参与主配置选择，实际返回深度和模型输入量分别记录。
6. 100 万索引只有在10万实测存储量通过容量检查后才进入；不在百万数据上重新选参数，沿用已冻结配置。先做两端默认393题，再做冻结配置和新增题验证。

| ContextBase 六组 | 改动（其他保持默认） |
| --- | --- |
| default | 原配置 |
| lean | 图与时间支路关闭，语义+BM25保留 |
| semantic_priority | 原生 `RECALL_STRATEGY_BOOSTS=semantic:high` |
| lexical_priority | 原生 `RECALL_STRATEGY_BOOSTS=bm25:high` |
| source_cap100 | 每召回支路进入融合前最多100候选 |
| recall_pool300 | high 原始召回预算300，模型候选仍60 |

原生 strategy boost 同时作用于召回融合和重排后的优先级，不把它描述成“仅修改 RRF 权重”的纯消融。

| WeKnora 六组 | 原生 RRF 设置 |
| --- | --- |
| default | k=60，vector/keyword=0.7/0.3 |
| balanced | k=60，0.5/0.5 |
| lexical_priority | k=60，0.3/0.7 |
| rrf_k30 | k=30，0.7/0.3 |
| rrf_k100 | k=100，0.7/0.3 |
| balanced_k30 | k=30，0.5/0.5 |

完整方案、模型权重文件 SHA-256、代码指纹和冻结规则：[experiment-plan.json](experiment-plan.json)、[方案指纹](experiment-plan.sha256)。两端调参次数相同，默认与调优结果都保留，不只展示对 ContextBase 有利的案例。

## 运行、进度和恢复

沿用同一台机器和同一份 CPU ONNX BGE-M3 / BGE-reranker-v2-m3。本轮从入库预检起统一3个 intra-op线程、1个inter-op线程、batch8、输入上限512、Embedding HTTP合批窗口10ms。正式查询并发1，原官方顺序，各配置相同4次非计分预热。模型、索引和原生程序不做算法源码修改，仅使用公开配置/原生环境参数。

每个规模使用独立 ContextBase 数据库和独立 WeKnora SQLite/配置目录，避免旧知识库的向量混入新规模。保留原1万索引与归档。入库仍由原 ingest.py 完成，检查点逐批提交、排名逐题fsync，模型调用按新增 `run_id` 精确归属阶段；不修改旧归档或混算旧用量。

持久进度：

- `/workspace/work/miracl-scale/status.json`：当前产品、阶段、索引进度或查询集。
- `/workspace/work/miracl-scale/events.jsonl`、`worker.log`：阶段事件与执行日志。
- `/workspace/work/miracl-scale/runtime-v2/`：独立原生状态、私有连接配置、入库检查点。
- `/workspace/work/miracl-scale/backups/`：完整索引备份。
- 原始共享模型日志位于 `/workspace/work/miracl-measurements/`，以独立实验前缀/run_id区分。

代码或冻结计划变化时任务停止，防止不同实现混入同一结果。单实例文件锁防止重复启动。中断后用同一设置、计划、原生状态与检查点恢复：

```bash
cd /workspace/hybrid-retrieval-benchmark
/workspace/work/api-venv/bin/python miracl_zh/run_scale.py \
  --settings /workspace/work/miracl-scale/runtime-settings.json \
  --results results/miracl-zh-scale-20261112
```

已有相同配置的完整阶段会跳过；未完逐题文件以 `--resume` 继续。WeKnora 若存在未决创建，不自动重复创建，按原 ingest.py 的 channel 核对及 `--recover` / `--retry-missing` 处理后恢复。私有运行设置不进入 Git。

每完成一个产品的默认基线或调优验证即归档并尝试推送本分支；入库每增加约1万段发布一次进度摘要。只暂存本实验输出，不强推，不混入其他已暂存用户修改。尚未完成的阶段不会生成完成分数。

百万阶段按10万实测原生索引尺寸估计新增索引与备份空间，并额外保留8GiB；若现有剩余空间不足，记录 `blocked_capacity` 和具体字节数，保留已完成数据，不通过删除原索引来强行继续。**目前仅完成百万语料准备，不能声称百万检索验证已完成。**

结果入口：[10万阶段](100k/README.md)、[100万阶段](1m/README.md)。启动预检与模型服务读回：[runtime-readiness.json](runtime-readiness.json)。扩容结果未完成前，不从原1万分数推算新规模质量，也不保证调优必然提升。
