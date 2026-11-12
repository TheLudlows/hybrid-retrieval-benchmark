# MIRACL 中文 10,000 段：ContextBase 与 WeKnora Lite 完整实测

2026-11-12 完成。两端各完成 **393/393 道正式查询**，正式查询失败数均为 0。全部质量、延迟和模型用量由本目录的逐题排名与模型调用记录复算，未沿用 TaurusDB 或合成语料的分数。差值统一为 **WeKnora Lite − ContextBase**。

本轮 ContextBase 的 Hit@1 高 3.82 个百分点、nDCG@10 高 0.021517，测得 p95 低 0.367 秒。两端 Hit@10 均为 100%。这些是本次子集和环境下的一次串行实测结果，不代表全量 MIRACL 或其他部署的表现。

## 语料、初始检查与索引审计

采用 MIRACL v1.0 中文开发集。先下载并核验官方全部 4,934,368 个段落，再复用 `build_subset.py --unit passages --count 10000 --seed 20261112` 生成子集。保留全部 **393 道查询、3,928 条标注、3,786 个已标注段落**，其中正相关标注 994 条。子集补入 6,214 个未标注段落，入库单位为原始段落，不是文章。

- 语料 gzip SHA-256：`3554a10e8b02519462efcdcfb632f9d7de1ee2388c3f9c9cd8c963a8efa93061`，大小 2,079,547 字节。
- 段落 ID 与标题、正文组成的排序内容指纹：`09d3d24eb941bc59555806669f767e08107b7f7cb898ff1fa01b9a64c4bcbf79`。
- 官方查询 SHA-256：`5b284a9aabf08bb2d1c88ed7ea276025c9d23846457c5350dc8d391b5e0d0a13`。
- 官方标注 SHA-256：`5546474d3dc8139014e6571e9f4041848bb0a314ca6d2d4bac708174d71c59eb`。

初始挂载环境未发现此前的 MIRACL 索引、入库检查点或 385 题逐题记录；两端产品服务也未运行。现有 ContextBase cloud-smoke 状态使用测试提供方，未复用。用户随后确认没有其他旧目录或服务，因此本轮分别补建两端，未将“此前 385/393”当成本轮已完成状态。

| 审计项 | ContextBase | WeKnora Lite |
| --- | ---: | ---: |
| 原生索引对应的官方段落数 | 10,000 | 10,000 |
| 缺失 / 多余官方 ID | 0 / 0 | 0 / 0 |
| 正文逐条校验 | 通过 | 通过 |
| 内部 ID 回映射 | 官方 ID 直接保存 | 10,000 个 UUID 与官方 ID 一一对应 |
| 语料与内容指纹 | 与上列一致 | 与上列一致 |
| WeKnora FTS / 原生向量记录数 | — | 10,146 / 10,146 |

两边均提交“官方标题 + 空行 + 官方正文”。WeKnora 原生手工文档接口会清理首尾空白，审计按该清理规则核对正文，且核对每条 `miracl-<文章ID>-<段落序号>` channel、知识库范围和完成状态。其 10,146 条检索记录来自原生分块，计分仍以 10,000 个官方段落 ID 为单位。

证据：[语料指纹](corpus-fingerprint.json)、[全量源核验](full-source-verified.json)、[子集核验](verified.json)、[ContextBase 索引审计](contextbase-index-audit.json)、[WeKnora 索引审计](weknora-index-audit.json)、两端[映射](contextbase-index-audit.mapping.csv) / [映射](weknora-index-audit.mapping.csv)。

## 固定配置与实际生效证据

| 项目 | 本次配置 |
| --- | --- |
| 机器 | 同一 Linux x86_64 环境，AMD EPYC 9V74；可见 5 个 CPU，cgroup 配额 4 核，内存上限 16 GiB |
| ContextBase | 源码 `c1cc16269361f2ed0ff4d1cfcb20345137b3b820`，采用仓库已有的[配置读取修正](../../contextbase-recall-config.patch)；原生 Recall `budget=high`、chunks 入库，分块配置 512，自动 consolidation 关闭 |
| ContextBase 数据库 | PostgreSQL 18.4、pgvector 0.8.2、pg_search 0.24.1，jieba；原生语义、BM25、图和时间检索启用 |
| ContextBase 请求 | `types=[world,experience]`，`max_tokens=8192`，`include.chunks.max_tokens=16384`；全局和 high 重排候选上限均为 60 |
| WeKnora Lite | 0.9.0，源码 `cbdcebabf48c5ee2d6868b370326aa8341703f62`，原生 `knowledge-search`；构建标识 `lite`、`sqlite_fts5`，SQLite 向量与 FTS5 |
| WeKnora 请求 | 单一知识库，`match_count=60`、`rerank.top_k=60`；向量与关键词召回均启用，向量、关键词和重排阈值均为 0；分块 512、重叠 0，问题生成和画像关闭 |
| Embedding | `onnx-community/bge-m3-ONNX@25b9af8e87a38eb120cfe87125383677b9cd309e`，1024 维 |
| 重排 | `onnx-community/bge-reranker-v2-m3-ONNX@6f5ff65298512715a1e669753bc754d2bc8f367b` |
| 模型运行 | 同一模型服务中的 ONNX INT8 CPU 权重；ONNX Runtime 1.31.1，正式查询 intra-op 3 线程、inter-op 1，推理 batch 8、最大输入长度 512；并发 Embedding HTTP 请求可在 10 ms 窗口内合批 |
| 查询条件 | 两端串行，官方 `topics.tsv` 原顺序，并发 1；各执行相同 4 条非计分预热；保留原生召回、融合、重排、合并和返回流程 |

WeKnora 通用重排器的默认保护上限是 200，本轮没有修改该原生常量。本轮单知识库请求将重排前检索深度提升并限制为 60，重排前关闭上下文扩展，且不启用查询扩展，因此实际输入池不超过 60。采集器检查原生 `candidate_count <= 60`，模型调用记录进一步确认：**两端每一道正式查询均实际送入 60 个候选**，各 393 次重排、23,580 对输入。

ContextBase 原生 trace 在满规模索引上给出语义 1,000 个、BM25 1,000 个、图 28 个召回候选；后续按 high 配置重排 60 个。WeKnora 关闭向量和重排的全文单路探针返回 60 条结果，`match_type` 全为 1；正式 393 题的 `rerank.applied` 均为 true。两边实际调用同一份 BGE 重排权重，未用 RRF 回退冒充模型重排。

预热文本依次为“检索预热：第1次中文文档检索”至“检索预热：第4次中文文档检索”。正式查询窗口（北京时间）：

- ContextBase：2026-11-12T17:39:08+08:00 至 2026-11-12T18:37:53+08:00。
- WeKnora Lite：2026-11-12T19:15:38+08:00 至 2026-11-12T20:17:07+08:00。

配置、模型文件 SHA-256、环境和包版本见 [config.json](config.json)、[配置指纹](config.sha256)、[环境配额](environment-observed.json)、[ContextBase 运行配置](contextbase-runtime-readback.json)、[WeKnora KB 配置](weknora-kb-config.json)、[WeKnora 检索控制](weknora-retrieval-config-readback.json)、[模型配置](weknora-model-config-readback.json)、[Lite 系统信息](weknora-system-info.json)、[构建信息](weknora-build-info.txt)、[模型包版本](model-packages.txt)和[ContextBase 包版本](contextbase-packages.txt)。实际探针：[ContextBase](contextbase-hybrid-probe.json)、[WeKnora 全文单路](weknora-hybrid-probe.json)。

## 检索质量

| 指标 | ContextBase | WeKnora Lite | 差值 W−C |
| --- | ---: | ---: | ---: |
| Hit@1 | 70.2290% | 66.4122% | -3.8168 pp |
| Hit@5 | 95.9288% | 95.6743% | -0.2545 pp |
| Hit@10 | 100.0000% | 100.0000% | +0.0000 pp |
| nDCG@10 | 0.828079 | 0.806561 | -0.021517 |
| Recall@100 | 99.8516% | 99.5438% | -0.3078 pp |

Hit@k 表示前 k 个官方段落中至少命中一个正例的查询比例；nDCG@10 使用官方二元相关性和标准对数折扣；Recall@100 先按题计算正例召回比例，再对全部 393 题取平均。按原生顺序保留每个官方段落首次出现的位置，不以内部 chunk ID 或知识 UUID 计分。

| 实际返回深度：去重后的官方段落数 | ContextBase | WeKnora Lite |
| --- | ---: | ---: |
| 平均 | 52.8957 | 59.2621 |
| 最小 / 最大 | 35 / 60 | 55 / 60 |
| 返回至少 100 段的题数 | 0 | 0 |

**本轮 Recall@100 是 60 候选及实际原生返回深度下的召回，未另开 100 候选实验。** ContextBase 的 Token 预算及原生过滤、两端的分块合并和段落去重会影响深度；不能把本表解释成双方均返回了 100 段。

## 检索性能与入库记录

从发出原生检索请求到完整 JSON 响应解码计时，包含模型重排；官方 ID 映射和逐题落盘位于该计时之后。预热、探针和重启试测不计入正式延迟。分位数为 nearest-rank，对 393 题分别取第 197、374、390 个耗时。

| 指标 | ContextBase | WeKnora Lite | 差值 W−C |
| --- | ---: | ---: | ---: |
| p50 | 8.824 s | 9.258 s | +0.434 s |
| p95 | 11.345 s | 11.713 s | +0.367 s |
| p99 | 12.458 s | 13.173 s | +0.714 s |
| 成功查询 | 393 / 393 | 393 / 393 | 0 |
| 正式查询失败 | 0 | 0 | 0 |

入库先执行两端各 16 段的恢复验证，再复用这 16 段，续入剩余 9,984 段。未引用无法核验的旧 10,000 段或 385 题历史耗时。

| 可核验入库计时分段 | ContextBase | WeKnora Lite |
| --- | ---: | ---: |
| 首 16 段 | 9.842 s，原生服务端 retain 记录 | 6.238 s，入库驱动墙钟 |
| 剩余 9,984 段续跑 | 2248.417 s，驱动墙钟 | 2201.972 s，驱动墙钟 |
| 上列记录分段之和 | 2258.259 s | 2208.210 s |

ContextBase 另有覆盖全部 10,000 段的原生 retain 处理记录，服务端墙钟合计 **2124.074 s**。首 16 段未保存驱动端计时，因此仅引用可核验的服务端历史，不将其伪装成驱动端墙钟。上列合计是计时分段之和，不是从零到完成的连续墙钟。

入库前期为适配 4 核配额调整过模型服务：ContextBase 前 512 段使用 6 个推理线程，后续使用 3 个线程，并有合批调试暂停；驱动墙钟包含这些暂停。WeKnora 入库使用调整后的服务。因此**入库数字用于记录本轮实际成本，不作同配置纯吞吐优劣结论**。正式查询全程固定为相同的 3 线程配置，期间未调整服务。记录：[入库计时汇总](ingest-timing-summary.json)、[ContextBase 原生逐批记录](contextbase-native-ingest-timing.json)、两端[驱动计时](contextbase.timing.jsonl) / [驱动计时](weknora.timing.jsonl)、[线程探针](thread-probe.json)。

## 实际模型输入用量

由部署模型的 tokenizer 对截断后的真实输入计数，使用 attention mask 排除 padding，包含特殊 Token；重排包含每一个 query–passage 组合，重复出现的 query 也重复计入。下列为原生入库和正式查询的实际输入，不是按字符估算。调用数指模型服务推理 API 请求数；同一请求中的 ONNX batch 不另算一个 API 调用。

| 阶段 | ContextBase 输入 Token | WeKnora 输入 Token | 差值 W−C | API 调用数 C / W |
| --- | ---: | ---: | ---: | ---: |
| 入库 Embedding | 1,056,632 | 1,109,760 | +53,128 | 10,000 / 10,000 |
| 393 题查询 Embedding | 3,795 | 3,795 | +0 | 393 / 393 |
| 393 题查询重排 | 3,283,377 | 3,538,138 | +254,761 | 393 / 393 |
| 重排输入对数 | 23,580 | 23,580 | 0 | 393 / 393 |
| 平均重排候选 / 正式查询 | 60.00 | 60.00 | 0 | — |

两端查询 Embedding 输入完全相同。ContextBase 重排输入少 254,761 Token，约为 WeKnora 的 7.20%；这反映本轮原生候选内容的实际差异，并非减少候选数。模型服务为向量和打分模型，无生成文本输出 Token。

入库用量包括用于最终索引且未重复导入的首 16 段；正式查询用量排除预热、诊断、线程探针和重启试测。原始记录保留所有阶段，ContextBase 排除 14 个非正式推理调用，WeKnora 排除 12 个。早期一条 ContextBase 重启试测沿用 `query` 标记；汇总以正式捕获时间窗和阶段共同过滤，已剔除该条，不能只对全部 `phase=query` 求和。

证据：[ContextBase 原始模型调用](contextbase-model-calls.jsonl)、[WeKnora 原始模型调用](weknora-model-calls.jsonl)、两端[逐题用量](contextbase-per-query-usage.csv) / [逐题用量](weknora-per-query-usage.csv)。精确值和时间窗在 [comparison.json](comparison.json)。

## 恢复验证、归档与复算

两端均先完成 16 段入库和第 1 题试采集，再重启原生服务并沿用原持久化状态；ContextBase 同时重启 PostgreSQL。重启后保留 16 个官方 ID，重复入库跳过已完成段落，续采集只执行第 2 题，第 1 题不再执行。正式数据使用独立文件。完整运行后再对双方执行 `--resume`，均报告 `Already complete: 393/393; no requests sent`，模型调用日志未增加。

每题保存原生排序、官方 ID、耗时及完成时间后执行 `fsync`；入库每批保存 SQLite 检查点与耗时，模型调用也逐条落盘。ContextBase 完成后先归档再停止其 API/PostgreSQL，随后运行 WeKnora 的入库和查询。预检阶段曾在 API 尚未 ready 时遇到连接拒绝，当时尚无排名或模型推理；等待健康检查通过后，沿用同一检查点和空输出文件 `--resume`，随后完成上述重启续题验证。正式查询未发生需要恢复的断点。

持久目录为 `/workspace/work/miracl-state`，共享模型记录为 `/workspace/work/miracl-measurements`。保留两端的 SQLite 入库检查点、16 段试测、完整逐题结果、进度快照、ContextBase 原生完整 PG dump、WeKnora 完整 SQLite 备份，以及各自匹配的私有运行配置。备份文件大小和 SHA-256 见 [persistent-backups.json](persistent-backups.json)。数据库及含认证状态的运行配置保留在挂载工作区，不进入 Git；Git 中保存报告、脱敏配置、指纹、完整排名、映射与模型用量。

恢复时在独立实例中还原对应原生数据库与匹配的私有配置，保持原 Bank/知识库 ID、API 地址和模型版本，沿用原 `*.sqlite` 入库检查点；重启模型服务后先核验语料指纹和索引审计，再给原采集命令增加 `--resume`。当前工作区启动脚本为 `/workspace/work/start-contextbase.py`、`/workspace/work/start-weknora.py`，ContextBase 先执行 `docker start miracl-pg`。不要用新建空 Bank/知识库替代恢复原索引。完整结果的续跑会直接跳过全部题。

以下命令复算本目录，数据目录须由同一官方源和种子生成并通过 `verify`：

```bash
python miracl_zh/build_subset.py --data /path/to/verified-full \
  --output /path/to/miracl-zh-10k --unit passages --count 10000 --seed 20261112
python miracl_zh/bench.py --data /path/to/miracl-zh-10k verify
python miracl_zh/bench.py --data /path/to/miracl-zh-10k score \
  results/miracl-zh-10k-20261112/contextbase-60.jsonl
python miracl_zh/bench.py --data /path/to/miracl-zh-10k score \
  results/miracl-zh-10k-20261112/weknora-60.jsonl
python miracl_zh/summarize_comparison.py --data /path/to/miracl-zh-10k \
  --results results/miracl-zh-10k-20261112
```

复测采集时，将模型服务配置为 `BENCH_ONNX_THREADS=3`、`BENCH_EMBEDDING_BATCH_WINDOW_MS=10`，按配置文件启动原生产品。两端均使用同一 `--config-sha256`，传入各自的 `--passage-map`、共享模型 `--measurement-dir`；每次默认执行相同四条预热。恢复检查和指标验证见 [validation.json](validation.json)。独立复算与原归档分数最大差异为 2.22e-16，处于浮点末位；验证容差为 1e-12。

## 主要结论与结果文件

1. 本轮 ContextBase 的 Hit@1 为 70.23%，WeKnora 为 66.41%，净多命中 15 题；逐题有 33 题仅 ContextBase 在首位命中、18 题仅 WeKnora 命中。Hit@5 净多命中 1 题，两端 Hit@10 均覆盖全部 393 题。
2. ContextBase nDCG@10 高 0.021517；逐题 129 题较高、80 题较低、184 题相同。Recall@100 高约 0.31 个百分点，但两端均受 60 候选和实际返回深度限制。
3. 本轮 ContextBase 的 p50/p95/p99 均较低，重排输入也较少；这是一次固定顺序串行运行的观察，不宣称跨环境或重复运行的统计显著优势。
4. 结果只适用于本次固定种子的 10,000 段候选池、ONNX INT8 模型、原生流程与实测 CPU 环境。不能直接外推到完整 4,934,368 段、其他精度、硬件或 100 候选设置。

完整原始排名：[ContextBase JSONL](contextbase-60.jsonl)、[WeKnora JSONL](weknora-60.jsonl)；TREC：[ContextBase](contextbase.trec)、[WeKnora](weknora.trec)。逐题指标和差异：[CSV](per-query-comparison.csv)、[逐题胜负计数](per-query-outcomes.json)、[ContextBase 分数](contextbase-score.json)、[WeKnora 分数](weknora-score.json)、[独立复算汇总](comparison.json)。

交付文件完整性清单：[artifacts-sha256.json](artifacts-sha256.json)（清单不包含自身）。
