# WeKnora 与 ContextBase 文档召回基准 V1

## 数据与范围

- `corpus/` 包含 30 篇合成中文运维文档，覆盖 10 个虚构系统，每个系统有生产参数、预发参数和值班手册。
- `questions.jsonl` 包含 120 道问题。精确匹配、语义改写、易混淆、长文定位、跨证据和无答案各 20 道。100 道有答案题共标注 120 个证据需求，来自 80 条不同的原文证据。可用 `python build_corpus.py` 重建。
- WeKnora 走知识库原生搜索，ContextBase 走原生 Recall 并取来源分块。ContextBase 检索 `world` 和 `experience`。两边评估可供下游使用的来源正文，保留产品各自的入库与排序流程。
- 这是小规模合成样本。题型结构在系统之间重复，适合联调和发现明显差异。正式对比需另外验证真实文档和独立人工题集。

## 运行

准备同一区域可达的两个实例，分别创建空的专用测试知识库和 Bank。开启 ContextBase 原文存储，使用具备写入和检索权限的测试凭据。把以下变量设在运行脚本的环境中。密钥通过运行环境注入。

| 环境变量 | 含义 |
|---|---|
| `WEKNORA_URL` | WeKnora API 站点地址，保留部署路径前缀，末尾省略 `/api/v1` |
| `WEKNORA_KB_ID`、`WEKNORA_API_KEY` | 专用知识库 ID 和访问凭据 |
| `CONTEXTBASE_URL` | ContextBase 接入页列出的 API 地址，末尾省略 `/v1/default/banks/...` |
| `CONTEXTBASE_BANK_ID`、`CONTEXTBASE_API_KEY` | 专用 Bank ID 和业务 Key |

使用 Python 3.10+。采样依赖 `tiktoken` 的 `o200k_base` 词表。首次运行可能需要下载词表。相同运行环境使用同一个包版本和词表。

```bash
python -m pip install 'tiktoken==0.9.0'
python bench.py ingest weknora run1
python bench.py ingest contextbase run1
python bench.py collect weknora run1
python bench.py collect contextbase run1
```

入库会写入这 30 篇资料。运行目录需保留两个产品各自的文档映射文件。如果上传已被服务接受但客户端未收到响应，先核对知识库再重试，避免 WeKnora 重复创建。WeKnora 等每篇文档完成索引后结束入库。ContextBase 使用同步 Retain；背景整理另外确认完成或关闭，并记录配置。记录两边部署版本、Embedding、重排配置、索引设置和测试机器。采样按固定随机顺序串行发送，一次完整运行采集 120 个请求。复跑使用新的输出路径。确认运行目录的映射与当前测试库一致后才复用映射。

## 核对证据并出分

在每条采样记录的 `candidates` 里，核对 `budget_text` 和 `matched_evidence_ids`。脚本先按同一文档中的原文精确匹配填写标签。人工核对文本提取差异、证据完整性和来源对应关系后，再设 `reviewed=true`。预算外的 `text` 保留用于诊断。无答案题和空响应自动完成该步骤。使用 `--allow-unreviewed` 可看自动匹配的初稿，报告会显示未复核题数。

```bash
python bench.py score run1/weknora-capture.jsonl > run1/weknora-summary.json
python bench.py score run1/contextbase-capture.jsonl > run1/contextbase-summary.json
```

- Evidence Recall@1024 是先按题计算必需证据的覆盖率，再取题均值。完整证据命中率要求该题所有必需证据均被带回。
- 两边用同一个 tokenizer 计量排序后的来源正文。相同文档的重复正文只保留第一次出现。最后一条按剩余预算截断。1024 是评测时分配给正文的预算。
- 文档 Hit@1、Hit@5 和文档 nDCG@10 按来源文档去重。nDCG 使用二元文档相关性。来源映射缺失及无原文的候选另计数。
- 无答案题统计原生接口返回任意候选的比例。不同产品的原始分数各有含义，首轮不设通用相似度阈值。
- 延迟为客户端测得的接口往返时间。指标报告成功请求的结果，失败次数单列。两边只有在成功题集合一致时才直接比较质量指标；若不一致，优先修复失败并重跑。

## 可选的模型用量

从模型服务的 usage 或可关联到该次入库、召回的追踪记录获取 Token 和模型调用次数。入库应包含属于本次导入的异步调用。在线按题记录查询 Embedding、改写、重排等实际消耗，覆盖失败请求中的已发生调用。对异构模型，另存逐模型和逐阶段明细；费用按各自模型计价。上下文正文 Token 由评测脚本独立计算。

为每个产品各建一个 `usage.jsonl`，把模型记录汇总成以下格式。示例中的数字只用于演示。

```jsonl
{"phase":"ingest","id":"run","status":"observed","input_tokens":1000,"output_tokens":100,"calls":10}
{"phase":"retrieve","id":"alder-01","status":"observed","input_tokens":80,"output_tokens":0,"calls":2}
{"phase":"retrieve","id":"alder-02","status":"estimated","input_tokens":74,"output_tokens":0,"calls":2}
{"phase":"retrieve","id":"alder-03","status":"unobserved"}
```

```bash
python bench.py score run1/weknora-capture.jsonl --usage run1/weknora-usage.jsonl
```

整道题或整个入库阶段的用量都有实测才标 `observed`，存在估算则标 `estimated`，存在无法估计的未观测调用则标 `unobserved`。只有确认某项为零才填零。工具分开汇总实测和估算，并报告覆盖题数；缺少用量文件时显示未观测。

## 接口参考

- [WeKnora API 概览](https://github.com/Tencent/WeKnora/blob/main/website-docs/04-api/01-api-overview.md)
- [WeKnora 文档导入](https://github.com/Tencent/WeKnora/blob/main/website-docs/04-api/02-api-knowledge.md)
- [ContextBase 记忆接口](https://github.com/RSIXDB/hindsight/blob/main/docs/contextbase/user-guide/api/memories.md)
