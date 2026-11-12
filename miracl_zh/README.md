# MIRACL 中文检索评测

采用 MIRACL v1.0 中文开发集的全部 393 道查询、3,928 条相关性标注以及完整的 4,934,368 个段落。语料和标签来自 [MIRACL 官方仓库](https://github.com/project-miracl/miracl)。[source.json](source.json) 固定了文件版本、长度和 SHA-256，[verified.json](verified.json) 是全量语料检查记录。

## 下载与核验

需要 Python 3.11 或以上。

```bash
python miracl_zh/bench.py --data .benchmark-data/miracl-zh fetch
python miracl_zh/bench.py --data .benchmark-data/miracl-zh verify
```

验证检查全部分片的 SHA-256、段落数及每个已标注段落是否在完整语料内。两款产品各自将每条原始段落作为一个文档入库，保留官方标题、正文和来源段落 ID。两边使用相同的标题与正文文本。索引阶段只使用语料，查询和标注供评测使用。

## 启动测试实例与入库

准备一套 ContextBase 和一套 WeKnora Lite，为本次评测各建一个空的 Bank 或知识库，并为入库提供写权限。按 [TaurusDB 基线](../results/taurusdb-config-sweep/METHOD.md) 设置原生混合检索和共享的 BGE-M3、BGE-reranker-v2-m3。ContextBase 使用 chunks 写入模式，WeKnora Lite 关闭问题生成和画像。需要本地模型文件时，可用 [上一轮模型下载脚本](../deployment/fetch_hybrid_models.py)。

从当前终端配置连接和评测范围。API Key 从部署配置中注入。

| 产品 | 所需环境变量 |
| --- | --- |
| ContextBase | `CONTEXTBASE_URL`、`CONTEXTBASE_BANK_ID`、`CONTEXTBASE_API_KEY` |
| WeKnora Lite | `WEKNORA_URL`、`WEKNORA_KB_ID`、`WEKNORA_API_KEY` |

使用下面的命令入库。两边的正文都使用官方标题、空行和官方段落正文。ContextBase 保存官方段落 ID，WeKnora Lite 的内部 ID 映射写入各自的 SQLite 检查点。

```bash
DATA=.benchmark-data/miracl-zh
mkdir -p "$DATA/results"
python miracl_zh/ingest.py --data "$DATA" contextbase "$DATA/contextbase.sqlite"
python miracl_zh/ingest.py --data "$DATA" weknora "$DATA/weknora.sqlite"
```

可先在同一测试范围内给入库命令加 `--limit 100` 验证写入和索引，再去掉它续跑。检查点绑定产品、API 地址、Bank 或知识库和语料版本，建议持续保留。WeKnora Lite 以原生 API 逐条创建，成批轮询解析状态。两条完整入库命令均应输出 `"full_corpus": true` 和 `"indexed_passages": 4934368`。

如果 WeKnora 创建过程中断且没有拿到返回 ID，脚本会报告对应的 MIRACL 段落和 `miracl-…` channel。先在同一个测试知识库查找这条记录。已存在时，在原入库命令末尾加 `--recover <段落ID> <知识ID>`，检查点会核对知识库和 channel 后记录映射。确认该记录未创建时，可用 `--retry-missing <段落ID>` 清除这次未决记录，再执行原命令。遇到解析失败，修复或重新解析已存在的知识后续跑。

## 采集与评分

采集前用非计分查询预热，并记录各端配置、模型输入用量及运行硬件。ContextBase 的 `budget=high` 候选上限在服务端设置为 60 和 40。WeKnora Lite 使用原生混合检索，默认请求深度为 60，并要求非空返回实际执行重排。重排模型 ID 默认为 `benchmark-bge-reranker`，实例里的 ID 不同时用 `--reranker` 指定。

```bash
python miracl_zh/bench.py --data "$DATA" capture contextbase "$DATA/results/contextbase-60.jsonl" --passage-map "$DATA/contextbase.sqlite"
python miracl_zh/bench.py --data "$DATA" capture weknora "$DATA/results/weknora-60.jsonl" --passage-map "$DATA/weknora.sqlite"
python miracl_zh/bench.py --data "$DATA" score "$DATA/results/contextbase-60.jsonl" --trec-output "$DATA/results/contextbase-60.trec"
python miracl_zh/bench.py --data "$DATA" score "$DATA/results/weknora-60.jsonl"
```

采集使用入库检查点时，会检查对应评测范围和全量导入状态。已用其他工具完成全量入库时，WeKnora Lite 可使用带 `passage_map(remote_id, passage_id)` 的 SQLite 映射，ContextBase 如直接使用官方段落 ID，可省略映射参数。

在 ContextBase 服务端通过 `CONTEXTBASE_API_RERANKER_MAX_CANDIDATES_HIGH` 设置 60 或 40，并重启服务使配置生效。40 档可复用同一份索引。

```bash
python miracl_zh/bench.py --data "$DATA" capture contextbase "$DATA/results/contextbase-40.jsonl" --passage-map "$DATA/contextbase.sqlite"
python miracl_zh/bench.py --data "$DATA" score "$DATA/results/contextbase-40.jsonl"
```

按原生排名保留每个来源段落的首次出现位置，统计官方 nDCG@10、Recall@100，以及补充的 Hit@5 和串行 p95。评分同时输出实际返回深度。60 或 40 候选档的 Recall@100 会受各产品配置及实际返回深度限制。和公开基线比较深召回时，需披露请求深度和返回深度。

MIRACL 的相关性标注以整个段落为单位。这份评分器使用官方段落 ID 计分，2,000 Token 原文证据召回沿用独立的 TaurusDB 业务试测。模型 Token 由实际部署的模型服务记录。

数据已完成全量核验，评分公式已用全部 393 个查询和构造排名与 `pytrec_eval` 逐题核对。两款产品的全库索引和实测结果仍待准备。

## 当前环境的向量化实测

在 6 个推理线程和每批 8 条的设置下，用与上一轮相同版本的 BGE-M3 ONNX 模型处理从 10 个分片固定抽取的 640 个段落。模型推理与分词合计 37.11 秒，约 17.25 段/秒。按这一速度线性估算，单遍完整向量化约需 79.5 小时，产品写入、索引构建和查询另计。测量明细见 [embedding-probe.json](embedding-probe.json)。
