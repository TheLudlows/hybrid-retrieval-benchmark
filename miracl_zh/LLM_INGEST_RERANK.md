# MIRACL 中文评测：LLM 入库与模型重排

如果两端服务和模型均已部署，使用 [API 直连入口](README.md)，从本机连接两个地址即可发起预检。

## 快速开始

在 Linux x86_64 上准备 Python 3.11+、Git、Docker、uv、Go、make、C 编译器和 curl。用下面的入口运行。首次执行会准备固定版本的 ContextBase、WeKnora Lite 和独立的 PostgreSQL，以及共享的 **BGE-M3 Embedding** 和 **BGE-reranker-v2-m3**。两端的 Embedding 与重排由脚本统一配置。

```bash
git clone --branch miracl-zh-benchmark --single-branch https://github.com/TheLudlows/hybrid-retrieval-benchmark.git
cd hybrid-retrieval-benchmark

# 可选，单独检查 GitHub 中归档的 10 万段、393 道查询和官方标注
bash miracl_zh/local_llm_ingest_rerank.sh verify

export BENCH_INGEST_LLM_URL=http://127.0.0.1:18092/v1
export BENCH_INGEST_LLM_MODEL=qwen2.5-1.5b-instruct-q4_k_m
bash miracl_zh/local_llm_ingest_rerank.sh smoke
# 要跑全部 10 万段和 393 题时
bash miracl_zh/local_llm_ingest_rerank.sh full
```

LLM 两个变量填写你已经启动的 OpenAI 兼容服务。服务需要支持非流式 Chat Completions、JSON schema 和响应中的实际 Token 用量。若服务要求鉴权，在本机注入 `BENCH_INGEST_LLM_API_KEY`。要运行本次使用的本地 Qwen，可按下文启动 llama.cpp。

结果默认保存到 `${XDG_DATA_HOME:-$HOME/.local/share}/miracl-llm-benchmark/results-smoke` 和 `results-full`，可用 `BENCH_LOCAL_RUNTIME` 改目录。预检与正式跑各自创建独立索引。默认 PostgreSQL 对外仅绑定本机 15443，原生服务和模型网关使用 18889、18081、18090。如有冲突，分别通过 `BENCH_PG_PORT`、`BENCH_CONTEXTBASE_PORT`、`BENCH_WEKNORA_PORT` 和 `BENCH_MODEL_PORT` 设定其他端口。已有专用 PostgreSQL 可传 `BENCH_PG_DSN_FILE`，其内容为一行管理连接 URL。

正式跑的数据已经放在 [miracl_zh/data/miracl-zh-100k](data/miracl-zh-100k/README.md)。`smoke` 只使用其中 8 段和 8 道题，方便确认本机导入、模型计量与检索链路。仓库数据来自公开 MIRACL，你自己的文档仍需单独整理为自定义评测集。

## 评测配置

两端使用相同的 MIRACL v1.0 中文段落 `title + "\n\n" + text`。ContextBase 采用 `concise` 事实抽取，WeKnora Lite 每文本块生成 1 个检索问句。两端使用同一生成模型做导入，并以同一 BGE-M3 和 BGE-reranker-v2-m3 运行原生混合检索与重排。查询直接使用原始问题，生成模型网关会记录并拒绝查询阶段的生成请求。模型重排上限是每次 60 个候选单元，输出按官方段落 ID 去重。ContextBase 原生 Recall 的返回预算是 8192 Token，WeKnora 保留原生返回处理。

正式配置沿用此前两端各自选定的 RRF 参数。ContextBase 为向量／全文 0.985／0.015，high=300，每源融合上限 100，关闭图／时间支路。WeKnora Lite 为 k=30、0.7／0.3。开启重排和 LLM 导入是本轮实验变化，旧索引和旧结果不作为本轮实测。并发为 1／4／8，每档各跑完整 393 题，每个窗口预热 4 次。负载依次为 C1、W1、W4、C4、C8、W8。Embedding HTTP 跨请求批处理窗口设为 0，以减小上轮已经观察到的批次相关向量变化。

## 手工配置与本地 LLM

- Linux x86_64、Python 3.11 或更高版本、Git、uv、Go 1.26.0 或更高版本、CGO 编译工具。资源采样使用 Linux `/proc` 和 cgroup v2。
- PostgreSQL 使用 `paradedb/paradedb:0.24.1-pg18` 或具备 `pg_search`、`pg_trgm`、`btree_gin`、`vector` 的兼容部署。启动器需要创建新数据库和这些扩展的权限。参数 `--pg-admin-dsn-file` 指向运行机上的私有文件，内含一行 PostgreSQL 管理连接 URL。使用专用数据库实例可获得更清楚的 CPU 和内存数据。
- ContextBase 源码是 [RSIXDB/hindsight 的 cdf6430](https://github.com/RSIXDB/hindsight/commit/cdf6430e028ff456d490436ff99e50fcf4aba59c)，含 Bank 可配置 RRF 权重。WeKnora Lite 是 [Tencent/WeKnora 的 cbdcebab](https://github.com/Tencent/WeKnora/commit/cbdcebabf48c5ee2d6868b370326aa8341703f62)。WeKnora 的 SQLite Lite 后端要以 FTS5 构建。
- 导入 LLM 可接本地或专用的 OpenAI 兼容服务。服务必须支持非流式 Chat Completions、ContextBase 的 JSON schema 请求，并在响应中返回实际 `usage.prompt_tokens` 和 `usage.completion_tokens`。远程 API 凭据用本机的 `BENCH_INGEST_LLM_API_KEY` 注入。

以下命令在评测仓库根目录执行。示例通过 `RUNTIME` 指定仓库外的空目录保存产品源码、依赖和运行文件。

```bash
export RUNTIME=/absolute/path/to/miracl-runtime
mkdir -p "$RUNTIME"
git clone https://github.com/RSIXDB/hindsight.git "$RUNTIME/contextbase"
git -C "$RUNTIME/contextbase" checkout cdf6430e028ff456d490436ff99e50fcf4aba59c
uv sync --project "$RUNTIME/contextbase" --locked --package hindsight-api-slim --no-dev
git clone https://github.com/Tencent/WeKnora.git "$RUNTIME/weknora"
git -C "$RUNTIME/weknora" checkout cbdcebabf48c5ee2d6868b370326aa8341703f62
make -C "$RUNTIME/weknora" build-lite SKIP_FRONTEND=1

uv venv --python 3.12 "$RUNTIME/model-venv"
uv pip install --python "$RUNTIME/model-venv/bin/python" \
  fastapi==0.162.5 uvicorn==0.54.0 onnxruntime==1.31.1 \
  tokenizers==0.23.3 numpy==2.5.4
python miracl_zh/bench.py --data miracl_zh/data/miracl-zh-100k verify
python deployment/fetch_hybrid_models.py
```

同批 10 万段文件 `corpus/subset.jsonl.gz` 的 SHA-256 为 `d51e1fe9ec6ec92db05df87aabff132f861a72b046a7713bc945f18746665363`。前序 BGE 文件版本由 `deployment/fetch_hybrid_models.py` 固定，运行时会记录 ONNX 权重和分词器指纹。用相同内容也可以指向已有的 10 万段语料目录。

本次云端使用 [Qwen2.5-1.5B-Instruct GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/tree/91cad51170dc346986eccefdc2dd33a9da36ead9) 的 `qwen2.5-1.5b-instruct-q4_k_m.gguf`，SHA-256 为 `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`。本地可下载 [llama.cpp b7600](https://github.com/ggml-org/llama.cpp/releases/tag/b7600) Linux CPU 版，解压后在对应目录启动服务。

```bash
curl -fL 'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf' \
  -o "$RUNTIME/qwen2.5-1.5b-instruct-q4_k_m.gguf"
llama-server --host 127.0.0.1 --port 18092 \
  --alias qwen2.5-1.5b-instruct-q4_k_m \
  --model "$RUNTIME/qwen2.5-1.5b-instruct-q4_k_m.gguf" \
  --threads 4 --ctx-size 8192 --parallel 1 --n-gpu-layers 0 --no-warmup
```

将 PostgreSQL 管理 DSN 写入 `$RUNTIME/pg-dsn`，并设置权限为 600。另开终端运行以下命令。使用官方 `uv lock` 安装的 ContextBase venv 中的 Python 运行驱动，模型服务使用独立 venv。如果 WeKnora checkout 的 Lite 二进制输出路径不同，传入实际路径。

```bash
CB="$RUNTIME/contextbase"
"$CB/.venv/bin/python" miracl_zh/run_llm_ingest_rerank.py \
  --data miracl_zh/data/miracl-zh-100k --work "$RUNTIME/run-100k" \
  --results "$RUNTIME/results-100k" --pg-admin-dsn-file "$RUNTIME/pg-dsn" \
  --contextbase-source "$CB" --contextbase-python "$CB/.venv/bin/python" \
  --weknora-source "$RUNTIME/weknora" --weknora-binary "$RUNTIME/weknora/WeKnora-lite" \
  --model-python "$RUNTIME/model-venv/bin/python" --run-name zh_llm_100k \
  --llm-url http://127.0.0.1:18092/v1 --llm-model qwen2.5-1.5b-instruct-q4_k_m \
  --llm-sha256 6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e
```

在正式执行前可以加 `--smoke --batch-size 4`，同时换一套 work、results 和 run-name，使用前 8 道题的已标注相关段落做真实 API 预检。该样本用来建立证据链，覆盖率和排序分数不能外推至正式语料。默认私有端口为 18889、18081、18090，碰到端口冲突时可分别传 `--contextbase-port`、`--weknora-port`、`--model-port`。本地 CPU 生成在 10 万段上可能耗时多天，先查看小样本的实际速度和模型输出。

## 产物与口径

- `plan.json` 记录语料、模型、源代码提交和具体参数。`status.json` 持续记录阶段和进度。保留同一 work、results 和 run-name 重跑即可续接已完成的入库批次。WeKnora 创建响应中断时，重启会在专用 SQLite 库中查找同一知识库和段落标记，核对并绑定已有知识 ID。出现多个匹配时停止，保留原数据供核对。
- `import.json` 保存首批提交到最后批次完成的墙钟时间、活动批次时间和模型用量。连续跑时以墙钟时间比较，发生中断时并列报告批次耗时，明确停顿。ContextBase 同步写入。WeKnora 等待异步任务完成，并核对原文与生成问句的 SQLite 向量及全文索引行。索引审计发生在计时后，原始段落覆盖及缺失情况单列。
- 各并发窗口的 `rankings.jsonl` 保存逐题段落 ID、毫秒耗时及失败状态。整个窗口完成后生成 `metrics.json`，其中有 nDCG、Recall、MRR、Hit、Precision、未标注率、成功 QPS、p50／p95／p99、成功率和超时数。未标注率和实际返回深度供解释 Precision 和 Recall。失败请求计入分母并按空排序计分。中断的查询窗口保留独立 attempt，下次运行会另起连续窗口。
- 模型用量来自本机 BGE/tokenizer 或上游 Chat Completions 的实际 usage。生成模型调用失败且没有 usage 时会列为 `unmetered_calls`，Token 合计仅含已观测值。模型重排证据要求实际非零调用且每次候选不超过 60。模型用量按窗口归属，并发下不宣称逐题模型记录已配对。
- `resources.jsonl` 保存约 0.5 秒采样的 RSS 与 CPU 轨迹，共享 BGE 进程、两端 API 和 PG 实例分开记录。RSS 累加可能重复计算共享页。存储体积在新索引核验后记录，含 ContextBase PG 数据库与工作区索引、WeKnora SQLite 主文件与 WAL。

运行器会创建有归属标记的专用 PostgreSQL 数据库、ContextBase Bank 和独立 WeKnora SQLite / KB，结束后保留它们。工作目录、密钥、数据库和私有日志留在本地。提交或分享时仅归档脱敏的方案、状态、指标与逐题结果。
