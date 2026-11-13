# ContextBase 配置组合追加验证结果

本轮仅使用原生配置，无模型重排。它是原先每端6组公平预算实验之后追加的探索，不能当成仍然相同选参预算的产品比较。393题和额外200题此前已检查，不作盲测承诺。

| 100题配置 | Hit@1 | nDCG@10 | Recall@10 | p95秒 |
| --- | ---: | ---: | ---: | ---: |
| lean_control | 42.00% | 0.594464 | 82.43% | 0.866 |
| lean_semantic_priority | 39.00% | 0.584728 | 82.43% | 0.991 |
| lean_source_cap100 | 42.00% | 0.596384 | 82.93% | 0.808 |

仅按固定100题选择：**lean_source_cap100**。

| 393题参照 | Hit@1 | nDCG@10 | Recall@10 |
| --- | ---: | ---: | ---: |
| 此前ContextBase lean | 45.29% | 0.601630 | 80.57% |
| 本轮选定配置 | 45.29% | 0.604006 | 80.97% |
| 此前WeKnora rrf_k30 | 53.18% | 0.709752 | 93.75% |

不同时间窗口的耗时仅作描述，不把此前参照当成同期配对性能实验。完整逐题排名、配置指纹、实际模型用量和原生配置读回均在100k目录。所有正式及预热重排调用必须为零。两端索引、数据库、私有连接配置及备份保留，百万阶段不执行。

## 额外200题复核

| 配置 | Hit@1 | nDCG@10 | Recall@10 | Recall@100 |
| --- | ---: | ---: | ---: | ---: |
| 此前lean | 45.00% | 0.613737 | 81.85% | 100.00% |
| 本轮lean_source_cap100 | 45.50% | 0.615439 | 81.49% | 100.00% |

393题nDCG差值为+0.002376，探索性配对bootstrap 95%区间[-0.001138, 0.006104]，包含零；15题nDCG改善、6题下降，首位命中无变化。额外200题nDCG与首位命中略增，但Recall@10下降，收益不一致。不能据此宣称稳定或显著提升，也没有消除与此前WeKnora选定配置的质量差距。

## 可用配置与结论

语义优先组合未胜出，不建议基于本实验启用。每支路100候选只带来轻微收益和取舍；仍以关闭图/时间/模型重排的lean配置为明确基线。该组合的原生服务端环境变量如下（环境变量需重启生效；Bank覆盖值也须核对）：

```bash
CONTEXTBASE_API_ENABLE_GRAPH_RETRIEVAL=false
CONTEXTBASE_API_ENABLE_TEMPORAL_RETRIEVAL=false
CONTEXTBASE_API_ENABLE_RERANKING=false
CONTEXTBASE_API_RECALL_MAX_CANDIDATES_PER_SOURCE=100
CONTEXTBASE_API_RECALL_STRATEGY_BOOSTS=
```

本次没有调整原始high预算1000、融合候选预算60或返回Token预算8192。两端数据均保留。完整核验、实际Token与调用数见 [final-verification.json](final-verification.json)；893次正式查询均有Embedding证据，重排调用为0，20次预热和3次重启试测均独立记录。
