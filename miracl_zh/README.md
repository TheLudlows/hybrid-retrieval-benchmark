# MIRACL 中文检索评测

采用 MIRACL v1.0 中文开发集的全部 393 道查询、3,928 条相关性标注以及完整的 4,934,368 个段落。语料和标签来自 [MIRACL 官方仓库](https://github.com/project-miracl/miracl)。[source.json](source.json) 固定了文件版本、长度和 SHA-256，[verified.json](verified.json) 是全量语料检查记录。

## 下载与核验

需要 Python 3.11 或以上。

```bash
python miracl_zh/bench.py --data .benchmark-data/miracl-zh fetch
python miracl_zh/bench.py --data .benchmark-data/miracl-zh verify
```

验证检查全部分片的 SHA-256、段落数及每个已标注段落是否在完整语料内。两款产品各自将每条原始段落作为一个文档入库，保留官方标题、正文和来源段落 ID。两边使用相同的标题与正文文本。索引阶段只使用语料，查询和标注供评测使用。

## 采集与评分

先准备两个完整、独立的评测索引，再执行采集。固定与 [TaurusDB 基线](../results/taurusdb-config-sweep/METHOD.md) 相同的共享模型和产品混合检索设置。采集前用非计分查询预热，并记录各端配置、索引覆盖、模型输入用量及运行硬件。ContextBase 的 `budget=high` 上限分别在服务端设置为 60 和 40，查询入口沿用原生 Recall。WeKnora Lite 使用原生混合检索，默认请求深度为 60，并要求非空返回实际执行重排。

从评测环境传入 `CONTEXTBASE_URL`、`CONTEXTBASE_BANK_ID`、`CONTEXTBASE_API_KEY` 或 `WEKNORA_URL`、`WEKNORA_KB_ID`、`WEKNORA_API_KEY`。若产品使用内部文档 ID，传入 `--passage-map` 指向入库时保存的 SQLite 映射。表结构为 `passage_map(remote_id TEXT PRIMARY KEY, passage_id TEXT NOT NULL UNIQUE)`。WeKnora Lite 使用该映射。ContextBase 如直接使用官方段落 ID，则直接从响应读取。

```bash
python miracl_zh/bench.py --data .benchmark-data/miracl-zh capture contextbase contextbase-60.jsonl
python miracl_zh/bench.py --data .benchmark-data/miracl-zh capture weknora weknora-60.jsonl --passage-map weknora-map.sqlite
python miracl_zh/bench.py --data .benchmark-data/miracl-zh score contextbase-60.jsonl --trec-output contextbase-60.trec
```

在 ContextBase 服务端切换 40 档后，可复用同一份索引重新采集，并指定独立的结果文件。按原生排名保留每个来源段落的首次出现位置，统计官方 nDCG@10、Recall@100，以及补充的 Hit@5 和串行 p95。评分同时输出实际返回深度。60 或 40 候选档的 Recall@100 会受各产品配置及实际返回深度限制。和公开基线比较深召回时，需披露请求深度和返回深度。

MIRACL 的相关性标注以整个段落为单位。这份评分器使用官方段落 ID 计分，2,000 Token 原文证据召回沿用独立的 TaurusDB 业务试测。模型 Token 由实际部署的模型服务记录。

数据已完成全量核验，评分公式已用全部 393 个查询和构造排名与 `pytrec_eval` 逐题核对。两款产品的全库索引和实测结果仍待准备。

## 当前环境的向量化实测

在 6 个推理线程和每批 8 条的设置下，用与上一轮相同版本的 BGE-M3 ONNX 模型处理从 10 个分片固定抽取的 640 个段落。模型推理与分词合计 37.11 秒，约 17.25 段/秒。按这一速度线性估算，单遍完整向量化约需 79.5 小时，产品写入、索引构建和查询另计。测量明细见 [embedding-probe.json](embedding-probe.json)。
