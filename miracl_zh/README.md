# MIRACL 中文检索评测

用已有的 ContextBase 和 WeKnora Lite 服务比较 LLM 入库与开启重排的检索。运行端需要 Linux 和 Python 3.11+，脚本通过两端的原生 API 使用服务已配置的模型。仓库自带 MIRACL v1.0 中文开发集的 [10 万段语料、393 道题和 3,928 条相关性标注](data/miracl-zh-100k/README.md)。

## 准备测试库

- 在两端各创建一个专用空库，配置同版本的生成、Embedding 和重排模型。
- ContextBase Bank 开启事实抽取、全文检索和模型重排，关闭图和时间检索，重排候选上限设为 60。
- WeKnora 文档知识库开启入库问题生成，关闭知识库画像，并配置默认重排模型。脚本提交导入请求时会关闭文档摘要。

[正式评测配置](../results/miracl-zh-llm-ingest-rerank-20261118/formal-runtime.json)记录了原实测所用的库设置。通过 API 可读取的设置及重排证据由脚本核验，实际模型版本由部署方确认。

## 配置并预检

在仓库根目录复制配置模板，再填写两端的 `base_url`、`api_key` 以及 ContextBase 的 `bank_id` 和 WeKnora 的 `kb_id`。填写 API 根地址，省略 `/v1` 或 `/api/v1`。密钥需要能读写测试库。WeKnora 的 `reranker_id` 可留空，使用服务默认模型。

```bash
cp miracl_zh/services.example.json miracl-services.local.json
chmod 600 miracl-services.local.json
# 填好配置后执行
python3 miracl_zh/run_existing_services.py --config miracl-services.local.json
```

本地配置文件已加入 Git 忽略规则。`api_key` 也可留空，由脚本读入 `CONTEXTBASE_API_KEY` 或 `WEKNORA_API_KEY`。如果使用位置参数直接传两个地址，同时设置这两个变量和 `CONTEXTBASE_BANK_ID`、`WEKNORA_KB_ID`。WeKnora 重排模型的选择顺序为 `--weknora-reranker-id`、配置文件的 `reranker_id`、`WEKNORA_RERANKER_ID`、服务默认模型。

默认导入 8 段并查询 8 道题。结果和进度分别写入 `~/miracl-api-benchmark/api_smoke/REPORT.md` 和 `status.json`。

## 正式运行

预检通过后，给正式评测创建另一对空库，把新库 ID 写入配置。添加 `--full` 后，脚本会导入 10 万段，分别以并发 1／4／8 在两端查询 393 道题。

```bash
python3 miracl_zh/run_existing_services.py --config miracl-services.local.json --full
```

- 默认数据目录为 `miracl_zh/data/miracl-zh-100k/`。使用 `python3 miracl_zh/bench.py --data miracl_zh/data/miracl-zh-100k verify` 可单独核验语料。评测其他用 `build_subset.py` 生成的 MIRACL 中文子集时，传 `--data <目录>`。
- 正式报告在 `~/miracl-api-benchmark/api_full/REPORT.md`。`--output <目录>` 可指定结果路径。中断后保留测试库和输出目录，用相同模式、地址、库 ID 和输出目录续跑。WeKnora 创建请求中断时，按脚本提示用 `--recover-weknora` 恢复官方段落 ID 与已创建的知识 ID 的映射。
- 报告包含导入耗时、查询成功率、QPS、p95、nDCG@10、Recall@10、MRR@10、Hit@1 和 Precision@5／10。逐题排名和 p50、p99、返回深度等写在各并发档的 JSON 中。查询耗时包含客户端到服务的网络。
- ContextBase 同步导入返回完整的事实抽取用量时，报告会列出 LLM 输入和输出 Token。其他实际模型用量与服务端资源消耗依赖部署方监控。直连模式拿不到的数据会在报告中标为 `N/A`。

## 其他复现入口

- [从零启动本机服务和模型的流程](LLM_INGEST_RERANK.md)。
- [历史实测和逐题结果](../results/)。复跑旧版 10,000 段、扩大语料、关闭重排或参数调优实验时，从[整理前的固定版本](https://github.com/TheLudlows/hybrid-retrieval-benchmark/tree/fc48ea1051e75af7dd3be596d48dcfe3d6ebf024/miracl_zh)取脚本及原运行说明。例如，在当前仓库旁开一个检出目录：

  ```bash
  git worktree add --detach ../miracl-zh-history fc48ea1051e75af7dd3be596d48dcfe3d6ebf024
  ```
