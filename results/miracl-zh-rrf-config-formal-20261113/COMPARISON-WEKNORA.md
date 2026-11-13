# ContextBase 新配置与 WeKnora Lite 对照

保存日期：2026-11-13（Asia/Shanghai）。数据来自已归档实测结果；本次整理没有新增检索或模型调用。

**比较边界：ContextBase 经过额外调优并使用新增配置代码，WeKnora 来自此前测试窗口。此表属于追加优化参照，不是同等调参预算的新一轮公平对比。** 原始两产品公平实验见[原生无重排报告](../miracl-zh-native-no-rerank-20261113/REPORT.md)。

共同条件：MIRACL v1.0 中文开发集，10万段相同语料、393题；BGE-M3，关闭模型重排，并发1，候选上限60，预热排除，按官方段落ID计分。语料指纹及模型版本见[完整验证报告](REPORT.md)。

| 检索质量 | ContextBase 新配置 | WeKnora Lite 已选配置 | ContextBase 差值 |
| --- | ---: | ---: | ---: |
| Hit@1 | 64.89% | 53.18% | +11.70 个百分点 |
| Hit@5 | 93.64% | 88.80% | +4.83 个百分点 |
| Hit@10 | 99.24% | 98.47% | +0.76 个百分点 |
| nDCG@10 | 0.776885 | 0.709752 | +0.067133 |
| Recall@10 | 95.41% | 93.75% | +1.65 个百分点 |
| Recall@100 | 99.83% | 99.50% | +0.33 个百分点 |

| 性能与模型用量 | ContextBase 新配置 | WeKnora Lite 已选配置 |
| --- | ---: | ---: |
| 成功查询数 | 393/393 | 393/393 |
| 检索 p50 | 680.39 ms | 808.66 ms |
| 检索 p95 | 876.30 ms | 873.69 ms |
| 检索 p99 | 998.73 ms | 898.41 ms |
| 平均返回条数 | 54.73 | 59.49 |
| 实际返回范围 | 38–60 | 55–60 |
| 查询 Embedding 调用数 | 393 | 393 |
| 查询 Embedding 输入 Token | 3,795 | 3,795 |
| 查询重排调用数/Token | 0/0 | 0/0 |

| 主要配置 | ContextBase 新配置 | WeKnora Lite 已选配置 |
| --- | --- | --- |
| 向量/全文权重 | 0.97/0.03 | 0.7/0.3 |
| RRF 常数 k | 60 | 30 |
| 原生检索入口 | Recall high | knowledge-search |
| 图/时间支路 | 关闭 | 无对应支路 |

本次数据中，ContextBase新配置的首位命中和排序质量更高；p50更低，p95接近，p99则是WeKnora更低。不同测试窗口的耗时不构成严格的同期性能胜负结论。两端最多返回60条，Recall@100按实际返回深度计分。

本轮没有重新入库，不新增或推算入库耗时。两端可核验的历史入库记录仍在原报告中。

ContextBase新代码仍在[草稿PR #108](https://github.com/RSIXDB/hindsight/pull/108)，未合并或替换原服务；远端CI尚未通过。两端数据、索引、检查点和备份保留。

来源：[ContextBase逐题评分](configured-dense97-dev393/metrics.json)、[WeKnora逐题评分](../miracl-zh-native-no-rerank-20261113/100k/weknora/rrf_k30-dev393-cap60/metrics.json)、[机器可读对照及文件指纹](comparison-weknora.json)。
