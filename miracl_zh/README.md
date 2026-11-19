# MIRACL 中文检索评测

用已有的 ContextBase 和 WeKnora Lite 服务比较 LLM 入库与开启重排的检索。直连脚本支持 Windows 和 Linux，需使用 Python 3.11+。脚本通过两端的原生 API 使用服务已配置的模型。仓库自带 MIRACL v1.0 中文开发集的 [10 万段语料、393 道题和 3,928 条相关性标注](data/miracl-zh-100k/README.md)。

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

在 Windows Git Bash 中可用 `python` 替代 `python3`。

本地配置文件已加入 Git 忽略规则。`api_key` 也可留空，由脚本读入 `CONTEXTBASE_API_KEY` 或 `WEKNORA_API_KEY`。如果使用位置参数直接传两个地址，同时设置这两个变量和 `CONTEXTBASE_BANK_ID`、`WEKNORA_KB_ID`。WeKnora 重排模型的选择顺序为 `--weknora-reranker-id`、配置文件的 `reranker_id`、`WEKNORA_RERANKER_ID`、服务默认模型。

ContextBase 的 Key 还需要能读取测试 Bank 的生效配置。托管版的普通成员 Key 可能能读写数据，但 `GET /config` 会隐藏配置字段。如果预检提示缺少 `retain_extraction_mode` 等字段，请在 `contextbase.api_key` 中使用该部署的管理员 API Key。若管理员 Key 仍缺字段，请核对版本，并确认 API 地址指向 ContextBase 服务。

默认导入 8 段并查询 8 道题。报告和进度保存在 `~/miracl-api-benchmark/api_smoke/`。用浏览器打开该目录下的 `REPORT.html` 可以查看图表，`REPORT.md` 是文本报告。

终端会实时显示配置检查、入库段落数、索引核验、各并发档的预热及查询进度、成功和失败数，以及报告生成状态。相同日志会追加到结果目录的 `progress.log`，`status.json` 保存最近一次状态。耗时较长的单个请求返回后，段落数或查询数才会更新。续跑时会标明复用的入库检查点和已完成的查询档。

在 Git Bash 中可以另开终端跟踪日志：

```bash
tail -f "$HOME/miracl-api-benchmark/api_smoke/progress.log"
```

正式评测目录改为 `api_full`，使用 `--output` 时查看你指定的目录。

## 正式运行

预检通过后，给正式评测创建另一对空库，把新库 ID 写入配置。添加 `--full` 后，脚本会导入 10 万段，分别以并发 1／4／8 在两端查询 393 道题。

```bash
python3 miracl_zh/run_existing_services.py --config miracl-services.local.json --full
```

- 默认数据目录为 `miracl_zh/data/miracl-zh-100k/`。使用 `python3 miracl_zh/bench.py --data miracl_zh/data/miracl-zh-100k verify` 可单独核验语料。评测其他用 `build_subset.py` 生成的 MIRACL 中文子集时，传 `--data <目录>`。
- 正式报告默认写入 `~/miracl-api-benchmark/api_full/`。`--output <目录>` 可指定结果路径。中断后保留测试库和输出目录，用相同模式、地址、库 ID 和输出目录续跑。WeKnora 创建请求中断时，按脚本提示用 `--recover-weknora` 恢复官方段落 ID 与已创建的知识 ID 的映射。
- 加 `--concurrency 1,4` 可只跑并发 1 和 4。已按 1／4／8 开始的同一轮评测，可以中断后在原命令追加该参数。其余配置一致时，脚本会复用入库检查点及已完成的并发 1、4 结果。
- 报告包含导入耗时、查询成功率、QPS、p95、nDCG@10、Recall@10、MRR@10、Hit@1 和 Precision@5／10。逐题排名和 p50、p99、返回深度等写在各并发档的 JSON 中。查询耗时包含客户端到服务的网络。
- ContextBase 同步导入返回完整的事实抽取用量时，报告会列出 LLM 输入和输出 Token。其他实际模型用量与服务端资源消耗依赖部署方监控。直连模式拿不到的数据会在报告中标为 `N/A`。

## 用较少段落跑正式评测

可以直接从仓库自带的 10 万段抽取更小的子集。例如下面生成 10,000 段并核验语料。抽样会保留 393 道官方查询、3,928 条相关性标注及其对应的 3,786 个段落。用 `--count` 调整段落数，最少需要 3,786 段才能保留全部标注。

```bash
python miracl_zh/build_subset.py --data miracl_zh/data/miracl-zh-100k --output miracl-zh-10k-local --unit passages --count 10000 --seed 20261112
python miracl_zh/bench.py --data miracl-zh-10k-local verify
```

把配置里的测试库 ID 换成一对空库，使用独立的结果目录运行。此时 `--full` 会导入所选子集的全部段落，并在每档并发下跑 393 道题。

```bash
python miracl_zh/run_existing_services.py --config miracl-services.local.json --data miracl-zh-10k-local --full --output "$HOME/miracl-api-benchmark/api_10k"
```

生成的 `subset.json` 会记录抽样种子、子集和抽样来源的指纹。分数仅适用于该子集。从归档的 10 万段再次抽样会得到一份新的 10,000 段子集，复现[早期 10,000 段报告](../results/miracl-zh-10k-20261112/README.md)需要该报告使用的语料指纹。

## 减少查询题数

`--full --query-count N` 可从 393 道官方题中抽取 1 到 393 道。下面的命令导入已生成的 10,000 段，并在两端的每档并发下跑同样的 50 道题，计分使用这 50 道题的官方标注。请使用专用空测试库和独立的输出目录。

```bash
python miracl_zh/run_existing_services.py --config miracl-services.local.json --data miracl-zh-10k-local --full --query-count 50 --output "$HOME/miracl-api-benchmark/api_10k_q50"
```

抽样种子固定为 `20261112`，选中的题目 ID 写入 `plan.json`。报告会标注实际题数和抽样范围。这个参数只缩短查询阶段，入库段落由 `--data` 决定。题数低于并发档位时，在途请求数最多等于题数。

## 查看或重建图表

评测完成后自动生成 `REPORT.html`。打开文件可切换并发 1／4／8，查看检索质量、延迟、成功 QPS、返回深度、入库耗时和已记录的模型用量。HTML 可以离线打开。

从已有结果生成图表时，把评测结果目录传给下面的命令。也可以用已有的部分结果生成报告，页面会显示各并发档已记录的产品数。

```bash
python miracl_zh/render_report.py "$HOME/miracl-api-benchmark/api_smoke"
```

也可以先用仓库里归档的 8 题预检结果查看效果。

```bash
python miracl_zh/render_report.py results/miracl-zh-llm-ingest-rerank-20261118/smoke --output miracl-preview.html
```

## 其他复现入口

- [从零启动本机服务和模型的流程](LLM_INGEST_RERANK.md)。
- [历史实测和逐题结果](../results/)。复跑旧版 10,000 段、扩大语料、关闭重排或参数调优实验时，从[整理前的固定版本](https://github.com/TheLudlows/hybrid-retrieval-benchmark/tree/fc48ea1051e75af7dd3be596d48dcfe3d6ebf024/miracl_zh)取脚本及原运行说明。例如，在当前仓库旁开一个检出目录：

  ```bash
  git worktree add --detach ../miracl-zh-history fc48ea1051e75af7dd3be596d48dcfe3d6ebf024
  ```
