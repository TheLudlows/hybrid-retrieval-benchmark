# 用已部署的服务跑 MIRACL 中文对比

入口接收 ContextBase 和 WeKnora Lite 的原生 API 基础地址。脚本通过 HTTP 导入和查找，复用两个服务已经配置好的生成、Embedding 和重排模型。运行端仅需要 Linux 与 Python 3.11+。评测语料、问题和标注已含在仓库。

## 服务侧准备

- 在两端各准备一个专用空测试库。ContextBase Bank 开启生成式事实抽取、全文检索和模型重排，关闭图与时间检索，重排上限设为 60。WeKnora 文档知识库开启问题生成，并配好默认重排模型。WeKnora 的导入请求关闭文档摘要，知识库画像在测试库中关闭。想对齐此前的权重和候选配置，可参考 [正式配置](../results/miracl-zh-llm-ingest-rerank-20261118/formal-runtime.json)。
- 两端配置相同的 LLM、Embedding 和重排模型。脚本会读取可见的库设置并验证原生重排证据。模型权重与实际版本由服务方核对。
- 使用可读写测试库的 ContextBase Bearer API key 和 WeKnora API key。首次运行时，脚本会检查两端测试库是否为空。

复制配置模板，把两端的 API 根地址、API Key 和测试库 ID 填入本地文件。地址不带 `/v1` 或 `/api/v1` 后缀。WeKnora 的 `reranker_id` 留空时使用现有服务的默认重排模型。

```bash
cp miracl_zh/services.example.json miracl-services.local.json
chmod 600 miracl-services.local.json
# 编辑 miracl-services.local.json 后执行
python3 miracl_zh/run_existing_services.py --config miracl-services.local.json
```

`miracl-services*.local.json` 已加入 Git 忽略规则。配置里的 `api_key` 也可以留空，由脚本读取对应的 `CONTEXTBASE_API_KEY` 或 `WEKNORA_API_KEY`。

已有环境变量和命令也可以继续使用。设置 `CONTEXTBASE_API_KEY`、`CONTEXTBASE_BANK_ID`、`WEKNORA_API_KEY` 和 `WEKNORA_KB_ID` 后，直接传两个地址。

```bash
python3 miracl_zh/run_existing_services.py \
  http://localhost:18888 http://localhost:18080
```

也可以用 `--weknora-reranker-id` 指定现有模型 ID。选项优先于配置文件中的非空 `reranker_id`，其次使用 `WEKNORA_RERANKER_ID`，最后由服务选择默认模型。测试期间专用库应由该任务独占。

默认使用 8 段和 8 道题预检，结果保存在 `~/miracl-api-benchmark/api_smoke`。正式跑时，用**另一对空库**更新测试库 ID，然后增加 `--full`。正式模式使用归档的 100,000 段、393 道题及官方标注，两端分别跑并发 1／4／8。指定 `--output <独立目录>` 可改路径。同一模式、地址、库和输出目录可续跑已保存的批次。如果 WeKnora 的创建响应中断，按错误提示用官方段落 ID 和已创建的知识 ID 恢复映射。

## 指标与输出

- 记录从首次导入到最后一批同步写入或异步完成的耗时，以及活动批次耗时。完成后通过两端原生列表核对所有文档 ID，并抽查 WeKnora 的生成问句。
- `REPORT.md` 列出导入耗时、请求成功率、QPS、p95、nDCG@10、Recall@10、MRR@10、Hit@1 和 Precision@5／10。逐题结果与 p50/p99、超时、返回深度、未标注率保存在各并发档的 JSON 文件中。查询时延包含客户端到服务的网络。
- ContextBase 同步导入如果返回所有批次的原生事实抽取用量，报告 LLM 输入和输出 Token。其他实际模型用量和服务端 CPU、内存、索引体积依赖现有模型或主机监控，业务 API 缺少证据时指标为 `N/A`。ContextBase 的重排通过额外 trace 抽查，WeKnora 通过每条非空搜索响应的重排元数据验证。
