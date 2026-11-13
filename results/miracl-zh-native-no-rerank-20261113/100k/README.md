# 100k 阶段结果

所有数值来自已完成并落盘的阶段；缺失阶段仍在运行或尚未开始。开发集结果已被检查，不能声称盲测。新增200题来自官方 train，仅未用于本轮调参。

| 产品 / 配置 / 查询集 | 题数 | Hit@1 | nDCG@10 | Recall@100 | p95 秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| [contextbase/default-assessment_dev293-derived](contextbase/default-assessment_dev293-derived/metrics.json) | 293 | 16.7235% | 0.409248 | 98.7770% | 0.922 |
| [contextbase/default-dev393-cap60](contextbase/default-dev393-cap60/metrics.json) | 393 | 17.5573% | 0.407673 | 99.0034% | 0.912 |
| [contextbase/default-tune_dev100-cap60](contextbase/default-tune_dev100-cap60/metrics.json) | 100 | 20.0000% | 0.403058 | 99.6667% | 0.859 |
| [contextbase/lean-assessment_dev293-derived](contextbase/lean-assessment_dev293-derived/metrics.json) | 293 | 46.4164% | 0.604075 | 99.6018% | 0.896 |
| [contextbase/lean-dev393-cap60](contextbase/lean-dev393-cap60/metrics.json) | 393 | 45.2926% | 0.601630 | 99.7031% | 0.916 |
| [contextbase/lean-fresh_train200-cap60](contextbase/lean-fresh_train200-cap60/metrics.json) | 200 | 45.0000% | 0.613737 | 100.0000% | 0.892 |
| [contextbase/lean-tune_dev100-cap100](contextbase/lean-tune_dev100-cap100/metrics.json) | 100 | 42.0000% | 0.594464 | 100.0000% | 0.907 |
| [contextbase/lean-tune_dev100-cap40](contextbase/lean-tune_dev100-cap40/metrics.json) | 100 | 42.0000% | 0.594464 | 99.4167% | 0.890 |
| [contextbase/lean-tune_dev100-cap60](contextbase/lean-tune_dev100-cap60/metrics.json) | 100 | 42.0000% | 0.594464 | 100.0000% | 0.875 |
| [contextbase/lean-tune_dev100-cap60-tokens16384](contextbase/lean-tune_dev100-cap60-tokens16384/metrics.json) | 100 | 42.0000% | 0.594464 | 100.0000% | 0.904 |
| [contextbase/lexical_priority-tune_dev100-cap60](contextbase/lexical_priority-tune_dev100-cap60/metrics.json) | 100 | 20.0000% | 0.402938 | 94.6333% | 0.921 |
| [contextbase/recall_pool300-tune_dev100-cap60](contextbase/recall_pool300-tune_dev100-cap60/metrics.json) | 100 | 20.0000% | 0.410417 | 99.3333% | 0.817 |
| [contextbase/semantic_priority-tune_dev100-cap60](contextbase/semantic_priority-tune_dev100-cap60/metrics.json) | 100 | 19.0000% | 0.397116 | 96.2667% | 0.952 |
| [contextbase/source_cap100-tune_dev100-cap60](contextbase/source_cap100-tune_dev100-cap60/metrics.json) | 100 | 22.0000% | 0.435919 | 100.0000% | 0.829 |
| [weknora/default-assessment_dev293-derived](weknora/default-assessment_dev293-derived/metrics.json) | 293 | 49.8294% | 0.674218 | 99.6359% | 0.894 |
| [weknora/default-dev393-cap60](weknora/default-dev393-cap60/metrics.json) | 393 | 50.1272% | 0.683642 | 99.5014% | 0.887 |

每个已完成配置目录保留配置指纹、逐题排名、实际模型调用（gzip）和用量。`assessment_dev293-derived` 是完整393题中的固定子集，不另发查询；其余记录标明实际查询集。40/100候选和ContextBase 16384返回预算仅为独立预算实验，不参与60候选主配置选择。

入库进度在挂载目录 `/workspace/work/miracl-scale/status.json`，事件在 `events.jsonl`。各产品入库用量文件标明本次续跑前已完成数量、尝试墙钟和累计已记录批次墙钟；原生索引与认证配置保存在该目录 runtime/backups，不进入 Git。
