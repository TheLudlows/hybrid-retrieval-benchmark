# TaurusDB 文档召回配置对比方法

同一台 CPU 测试机串行运行各组。使用 40 篇 TaurusDB 官方文档、40 道题，包含 34 道可回答题和 6 道无答案题。10 道可回答题要求跨文档找齐证据。文档 HTML 和入库 Markdown 的逐篇 SHA-256 均与 [TaurusDB 首轮语料清单](../taurusdb-pilot/corpus-manifest.json) 一致。题目和更新样本沿用 [taurusdb_retrieval_pilot](../../taurusdb_retrieval_pilot/)。

| 项目 | 实测条件 |
| --- | --- |
| ContextBase | 源码提交 `c1cc16269361f2ed0ff4d1cfcb20345137b3b820`，沿用首轮的[配置读取补丁](../../contextbase-recall-config.patch)。原生 Recall，`budget=high`，原文 chunks，PostgreSQL 18.4、pgvector 0.8.6、pg_search 0.24.1 和 jieba。开启语义、BM25、图与时间检索。图与时间路的实际候选取决于文档和查询。 |
| WeKnora Lite | 源码提交 `cbdcebabf48c5ee2d6868b370326aa8341703f62`。原生 `/knowledge-search`，SQLite FTS5 加向量，候选请求上限 60，开启模型重排。 |
| 模型 | 本地 CPU ONNX。Embedding 为 BGE-M3，固定模型仓库版本 `25b9af8e87a38eb120cfe87125383677b9cd309e`。重排为 BGE-reranker-v2-m3，固定版本 `6f5ff65298512715a1e669753bc754d2bc8f367b`。每个模型 6 个 ONNX intra-op 线程。 |
| 调参 | 使用每组独立的索引，从同一份文档入库。ContextBase 全局候选上限为首轮的 60，配置档通过 `CONTEXTBASE_API_RERANKER_MAX_CANDIDATES_HIGH` 设置本次请求的重排上限。 |
| 请求 | 每组预热相同的 4 道题，随后按固定顺序串行执行全部 40 道题。预热调用不计入正式查询。全程使用只有回环接口的网络命名空间。 |

## 评分口径

- **2,000 Token 证据召回** 按产品返回顺序去重，使用同一 `o200k_base` tokenizer 裁剪到 2,000 Token。按来源文档 ID 和归一化原文匹配证据，先对每题计算命中证据比例，再对可回答题取平均。
- **证据 Hit@5** 统计原始前五条结果含任一标注证据的可回答题。**跨文档完整命中** 要求 2,000 Token 内出现该题所有标注证据。
- **串行 p95** 统计完整检索请求直到拿到正文的延迟。40 个请求按耗时排序取第 38 个。
- **模型 Token** 按调用记录分别计入入库和查询，查询合计 embedding 与重排输入。模型 tokenizer 与 2,000 Token 的正文评估 tokenizer 各按对应口径工作。向量和重排分数没有文本生成输出 Token。
- **更新可检索时间** 对 5 组固定样本，在已验证可召回旧版后提交替换，查询轮询间隔 2 秒。分别记录首次召回新版、首次不再召回旧版，以及最终确认仅召回新版的状态。计时含检索和重排耗时。
- **无答案返回候选** 统计无答案题仍返回任一候选的题数。这项评测没有为这些候选标注错误支撑。

## 执行和复算

先按 [混合检索基线](../../docs/baseline-results.md) 准备锁定版本的产品、模型和运行环境，按 [TaurusDB 试测集说明](../../taurusdb_retrieval_pilot/README.md) 构建语料。离线运行前预缓存评分用的 `o200k_base` 词表。评测入口需要 Linux 用户和网络命名空间支持。

```bash
deployment/model-venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
export BENCH_SWEEP_OUTPUT="$PWD/deployment/results/taurusdb-config-sweep-run"
deployment/run_config_sweep.sh contextbase-60 contextbase-40 contextbase-30 contextbase-50 weknora
deployment/model-venv/bin/python deployment/score_config_sweep.py \
  "$BENCH_SWEEP_OUTPUT" taurusdb-pilot-corpus \
  contextbase-60 contextbase-40 contextbase-30 contextbase-50 weknora
```

如需复测更新，使用 `deployment/run_config_sweep.sh --updates contextbase-60 weknora contextbase-40`，复用该轮已入库的索引。归档中的逐题返回和模型调用均可由同一个评分器复算，参数改用 `results/taurusdb-config-sweep` 和 `taurusdb_retrieval_pilot`，最后列出需要复算的档位。
