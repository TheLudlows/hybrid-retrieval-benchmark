# MIRACL 中文检索评测

当前主实验按用户要求改为[关闭产品内部模型重排](../results/miracl-zh-native-no-rerank-20261113/README.md)。采集新增 `--disable-rerank`，WeKnora请求显式关闭，ContextBase还需原生Bank/服务端 `enable_reranking=false`。该模式必须核验实际重排调用为零，且不能续跑进带重排的文件。评分新增 Recall@10。

当前0.97/0.03配置已完成[全部393题原生阶段诊断](../results/miracl-zh-weighted-stage-diagnosis-20261113/REPORT.md)，含损益题目、模型调用及可复算轨迹；诊断不调整参数或改写正式成绩。

已完成[四组原生权重与纯向量消融对照](../results/miracl-zh-native-weight-ablation-20261113/REPORT.md)，各393题，包含同期基线、逐题损益、实际模型用量及耗时。

固定种子 20261112、10,000 个段落、两端各 393 题的[完整实测报告与复算数据](../results/miracl-zh-10k-20261112/README.md)已归档。以下同时保留官方全量语料和其他规模子集的执行方法。

采用 MIRACL v1.0 中文开发集的全部 393 道查询、3,928 条相关性标注以及完整的 4,934,368 个段落。语料和标签来自 [MIRACL 官方仓库](https://github.com/project-miracl/miracl)。[source.json](source.json) 固定了文件版本、长度和 SHA-256，[verified.json](verified.json) 是全量语料检查记录。

## 下载与核验

需要 Python 3.11 或以上。

```bash
python miracl_zh/bench.py --data .benchmark-data/miracl-zh fetch
python miracl_zh/bench.py --data .benchmark-data/miracl-zh verify
```

验证检查全部分片的 SHA-256、段落数及每个已标注段落是否在完整语料内。两款产品各自将每条原始段落作为一个文档入库，保留官方标题、正文和来源段落 ID。两边使用相同的标题与正文文本。索引阶段只使用语料，查询和标注供评测使用。

## 使用 10 万篇文章或 10 万个段落

在下载官方语料后生成一个固定子集。文章模式先保留官方标注覆盖的全部文章，再用固定种子补足篇数，保留所选文章的全部段落。段落模式先保留所有已标注段落，再从未标注段落里抽样补足。两种模式都保留官方 393 道查询、3,928 条标注及其对应的 3,786 个段落。

```bash
FULL=.benchmark-data/miracl-zh
DATA=.benchmark-data/miracl-zh-100k-articles
python miracl_zh/build_subset.py --data "$FULL" --output "$DATA" --unit articles --count 100000
python miracl_zh/bench.py --data "$DATA" verify
```

如果要控制实际入库量，改用 `--unit passages` 并选择新的输出目录。两款产品使用同一个 `DATA`，分别创建新的 Bank 和知识库。后面的入库、采集和评分都传入该子集目录，采集时两边都传各自的检查点。子集入库完成后输出 `"dataset_complete": true` 和 `"full_corpus": false`，评分结果自带子集标识。子集改变了检索候选池，分数适用于相同子集上的对比。

本次 10,000 段设置为 `--unit passages --count 10000 --seed 20261112`。生成后执行同一个 `verify`，保留 `subset.json` 中的 gzip SHA-256；不能仅凭段落数量复用其他索引。

扩容需要保留原候选池时，使用 `--include-subset /path/to/parent`；不能假定相同 seed 下不同 count 的独立抽样天然包含。可另传 `--extra-passages /path/to/ids.txt` 保留其他查询集的全部标注段落。这两个选项仅支持段落模式，生成器验证所有必选 ID 均存在。新的嵌套语料、查询分组及预注册实验见[扩容实验](../results/miracl-zh-scale-20261112/README.md)。原有不带这两个选项的抽样规则不变。

默认种子 20261112 的实际生成规模如下。

| 抽样单位 | 入库段落数 | gzip 文件大小 |
| --- | ---: | ---: |
| 100,000 篇文章 | 440,281 | 70.1 MB |
| 100,000 个段落 | 100,000 | 17.7 MB |

## 启动测试实例与入库

准备一套 ContextBase 和一套 WeKnora Lite，为本次评测各建一个空的 Bank 或知识库，并为入库提供写权限。按 [TaurusDB 基线](../results/taurusdb-config-sweep/METHOD.md) 设置原生混合检索和共享的 BGE-M3、BGE-reranker-v2-m3。ContextBase 使用 chunks 写入模式，WeKnora Lite 关闭问题生成和画像。需要本地模型文件时，可用 [上一轮模型下载脚本](../deployment/fetch_hybrid_models.py)。

从当前终端配置连接和评测范围。API Key 从部署配置中注入。

| 产品 | 所需环境变量 |
| --- | --- |
| ContextBase | `CONTEXTBASE_URL`、`CONTEXTBASE_BANK_ID`、`CONTEXTBASE_API_KEY` |
| WeKnora Lite | `WEKNORA_URL`、`WEKNORA_KB_ID`、`WEKNORA_API_KEY` |

使用下面的命令入库。两边的正文都使用官方标题、空行和官方段落正文。ContextBase 保存官方段落 ID，WeKnora Lite 的内部 ID 映射写入各自的 SQLite 检查点。

```bash
DATA=${DATA:-.benchmark-data/miracl-zh}
mkdir -p "$DATA/results"
python miracl_zh/ingest.py --data "$DATA" contextbase "$DATA/contextbase.sqlite"
python miracl_zh/ingest.py --data "$DATA" weknora "$DATA/weknora.sqlite"
```

可先在同一测试范围内给入库命令加 `--limit 100` 验证写入和索引，再去掉它续跑。检查点绑定产品、API 地址、Bank 或知识库和语料版本，建议持续保留。WeKnora Lite 以原生 API 逐条创建，成批轮询解析状态。选择官方全量语料时，两条入库命令均应输出 `"full_corpus": true` 和 `"indexed_passages": 4934368`。

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

采集现在默认先执行相同的四次非计分中文预热，逐题写入后执行 `fsync`。续跑在原命令增加 `--resume`；同时传入 `--config-sha256 <冻结配置文件的SHA-256>`，脚本会核对索引绑定、语料、预算、候选上限、重排模型和配置指纹，并跳过已保存的题。首次与续跑须使用相同指纹。`--measurement-dir` 指向共享模型服务的记录目录，区分预热、正式查询和重启试测。重启试测可用独立输出文件加 `--smoke-index --max-queries 1 --warmup 0`，重启服务后用 `--resume --max-queries 2` 验证跳过已完成题；该模式最多四题，不能用于正式评分。

`audit_index.py` 可在正式采集前核对入库检查点、全部官方 ID、原生正文和向量覆盖，导出内部 ID 映射。ContextBase 从环境变量 `AUDIT_POSTGRES_URL` 读取数据库连接；WeKnora 从只读的 `--weknora-db` 读取 SQLite。示例：

```bash
python miracl_zh/audit_index.py --data "$DATA" --product contextbase \
  --checkpoint "$DATA/contextbase.sqlite" --output "$DATA/results/contextbase-index-audit.json"
python miracl_zh/audit_index.py --data "$DATA" --product weknora \
  --checkpoint "$DATA/weknora.sqlite" --weknora-db /path/to/weknora.db \
  --output "$DATA/results/weknora-index-audit.json"
```

在 ContextBase 服务端通过 `CONTEXTBASE_API_RERANKER_MAX_CANDIDATES_HIGH` 设置 60 或 40，并重启服务使配置生效。40 档可复用同一份索引。

```bash
python miracl_zh/bench.py --data "$DATA" capture contextbase "$DATA/results/contextbase-40.jsonl" --passage-map "$DATA/contextbase.sqlite"
python miracl_zh/bench.py --data "$DATA" score "$DATA/results/contextbase-40.jsonl"
```

按原生排名保留每个来源段落的首次出现位置，统计官方 nDCG@10、Recall@100，以及补充的 Hit@5 和串行 p95。评分同时输出实际返回深度。60 或 40 候选档的 Recall@100 会受各产品配置及实际返回深度限制。和公开基线比较深召回时，需披露请求深度和返回深度。

评分器也输出 Hit@1、Hit@10 和串行 p50、p99；耗时分位数使用 nearest-rank。正式评分仍要求全部 393 题，不完整文件会报错。入库脚本在检查点旁追加 `.timing.jsonl`，记录每批耗时、此前已索引数量和累计索引数量。

MIRACL 的相关性标注以整个段落为单位。这份评分器使用官方段落 ID 计分，2,000 Token 原文证据召回沿用独立的 TaurusDB 业务试测。模型 Token 由实际部署的模型服务记录。

数据已完成全量核验，评分公式已用全部 393 个查询和构造排名与 `pytrec_eval` 逐题核对。两款产品的全库索引和实测结果仍待准备。

## 当前环境的向量化实测

在 6 个推理线程和每批 8 条的设置下，用与上一轮相同版本的 BGE-M3 ONNX 模型处理从 10 个分片固定抽取的 640 个段落。模型推理与分词合计 37.11 秒，约 17.25 段/秒。按这一速度线性估算，单遍完整向量化约需 79.5 小时，产品写入、索引构建和查询另计。测量明细见 [embedding-probe.json](embedding-probe.json)。
