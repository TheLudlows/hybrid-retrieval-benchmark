# WeKnora Lite 与 ContextBase 混合检索评测

这套评测比较中文文档原文召回。两边使用同一语料、查询、正文预算、Embedding 和重排模型，保留各自的原生混合检索及正文返回逻辑。

| 内容 | 位置 | 状态 |
| --- | --- | --- |
| 测试方法与指标 | [docs/methodology.md](docs/methodology.md) | 含召回、延迟、模型用量与更新测试口径 |
| 合成基线 | [doc_recall_v1/](doc_recall_v1/) | 30 篇文档、120 道标注题 |
| 完整混合检索结果 | [docs/baseline-results.md](docs/baseline-results.md)、[results/](results/) | 双边 120 题实测、逐题返回、模型用量与检索 trace |
| TaurusDB 试测集 | [taurusdb_retrieval_pilot/](taurusdb_retrieval_pilot/) | 40 个官方页面来源、40 道标注题、5 组更新样本。[首轮实测](results/taurusdb-pilot/summary.json)已完成 |
| ContextBase 重排配置对比 | [实测结果与复算数据](results/taurusdb-config-sweep/) | 同一套 TaurusDB 语料复测 ContextBase 与 WeKnora Lite |
| 采集与复算代码 | [deployment/](deployment/)、[doc_recall_v1/bench.py](doc_recall_v1/bench.py) | 复算脚本直接读取已归档结果。原采集入口依赖外部产品和模型运行时 |

## 复算已归档的基线

使用 Python 3.10 或更新版本。安装 tiktoken 后，首次初始化词表可能需要联网。

```sh
python -m pip install tiktoken==0.9.0
python deployment/summarize_full_hybrid.py results/weknora results/contextbase
```

结果目录保留 gzip 压缩的逐题返回和 ContextBase 探针。原测试配置、产品提交、模型版本和结果范围记录在[基线报告](docs/baseline-results.md)。ContextBase 的测试副本使用[配置读取修正](contextbase-recall-config.patch)。

TaurusDB 试测使用官方页面清单和证据定位标注。可在能访问官网的环境中[构建原文语料](taurusdb_retrieval_pilot/README.md)，将两边入库使用的同一份文档哈希清单随结果保存。
