# ContextBase：9组原生混合检索参数探索

后续[知识库指标补全](../miracl-zh-metrics-suite-20261116/REPORT.md)已完成，包含两端同期复测、MRR/Precision、并发吞吐、资源成本与更新可见性；本页保留参数探索原始快照。

完整成绩、候选差值和结论见 [REPORT.md](REPORT.md)，实时状态见 [execution-status.json](execution-status.json)。只有存在完整 `metrics.json` 的阶段才算完成。

本轮已完成全部15个阶段、2679次正式请求。[推荐候选配置](recommended-config.json)为0.985/0.015、high预算300；393题nDCG@10为0.781703，额外200题为0.833261。模型重排调用为0；推荐候选未部署到原服务。

## 评测范围

- MIRACL v1.0 中文，固定10万个官方段落；种子20261112，包含既有1万段子集及开发集、额外200题的全部标注段落。
- 393道官方dev题；其中既有100题用于本轮筛选。额外200道来自此前已使用的官方train样本。全部为探索性评测，不作盲测或模型未见数据的声明。
- 共享同版本BGE-M3、ONNX INT8 CPU、串行并发1；每阶段相同4次预热，不计分。全文与向量开启，图/时间支路及模型重排关闭。
- 9组参数在请求前注册；先完成全部100题筛选，冻结质量与效率候选，才运行候选及基线的393题和200题。复核后不扩大网格。
- WeKnora本轮没有重跑；本轮ContextBase追加调优，不能替换此前等调参预算的两产品比较。

本轮使用独立恢复库，源码固定为 [`cdf6430`](https://github.com/RSIXDB/hindsight/commit/cdf6430e028ff456d490436ff99e50fcf4aba59c)。可配置RRF权重依赖 [草稿PR #108](https://github.com/RSIXDB/hindsight/pull/108)，不是原服务已部署的默认能力。原服务没有切换这个版本。开始时曾恢复已停止的数据库容器、模型服务和原API进程，经过设置核对后再跑；没有重建索引，详见 [服务恢复记录](service-recovery.json)。

## 语料与证据

gzip语料SHA-256：`d51e1fe9ec6ec92db05df87aabff132f861a72b046a7713bc945f18746665363`。

原生索引覆盖、正文一致性及官方ID映射见 [index-audit.json](index-audit.json)。模型文件指纹见 [model-fingerprints.json](model-fingerprints.json)，运行时读回见 [model-info.json](model-info.json)。本轮复用索引，入库Embedding新增为0；不把历史入库时间冒充本轮测量。

每阶段目录保存：

- `config.json`、`bank-config-readback.json`：参数和原生配置读回。
- `rankings.jsonl`：官方段落ID排名、请求耗时和完成时间；每题落盘，可续跑。
- `metrics.json`：六项质量指标、耗时分位数、实际返回深度及逐题指标。
- `native-probe.json.gz`、`probe-verification.json`：实际召回深度、全文支路、加权RRF和无模型重排的核验。
- `model-calls.jsonl.gz`、`model-usage.json`：实际模型调用、Token及预热/探针/正式查询分类。

Recall@100按实际返回列表计分。本轮候选上限60且有Token过滤，不能理解成实际返回100条。未标注段落按官方qrels规则得0分，这不等价于人工证明不相关。

## 复跑和恢复

运行本报告的历史命令前，请按[MIRACL 复现说明](../../miracl_zh/README.md#其他复现入口)检出整理前的固定版本。

先按 [正式配置验证报告](../miracl-zh-rrf-config-formal-20261113/REPORT.md) 准备独立恢复库、原生API运行环境、模型服务及私有设置。复制本目录的 `experiment-plan.json`、`fresh-topics.tsv`、`fresh-qrels.tsv` 到新的结果目录。驱动会校验自身SHA、源码提交、模型文件和语料指纹；私有路径与密钥不入库。

```bash
python miracl_zh/optimize_contextbase_native.py \
  --settings /private/runtime-settings.json \
  --work /private/rrf-validation \
  --source /path/to/contextbase-cdf6430 \
  --python /path/to/venv/bin/python \
  --results /path/to/results-copy
python miracl_zh/analyze_contextbase_optimization.py \
  --data /path/to/miracl-zh-100k \
  --results /path/to/results-copy
```

已有逐题文件必须与同一语料和配置指纹一致。原命令重跑会跳过完整阶段和已完成题目；所有实际模型尝试均保留在日志中。不要清除检查点或用其他配置覆盖结果文件。`--publish`仅在明确需要提交评测仓库时启用，本地默认不提交。

既有[少量题目重启续跑验证](../miracl-zh-rrf-config-formal-20261113/restart-proof.json)继续保留。本轮预检失败发生于0次正式请求时，不将其算作完成题目的续跑证明；原故障记录保存在recovery-history中，已恢复状态见service-recovery.json。

本轮结束时将独立Bank恢复0.97/0.03、high=1000，并停止本轮拥有的验证API。两端原数据、索引、检查点、备份及独立验证库均保留；原始服务进程恢复与实验配置变更是分开记录的。
