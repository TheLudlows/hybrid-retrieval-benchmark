# 100k 阶段结果

所有数值来自已完成并落盘的阶段；缺失阶段仍在运行或尚未开始。开发集结果已被检查，不能声称盲测。新增200题来自官方 train，仅未用于本轮调参。

| 产品 / 配置 / 查询集 | 题数 | Hit@1 | nDCG@10 | Recall@100 | p95 秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| [contextbase/dense70-tune_dev100-cap60](contextbase/dense70-tune_dev100-cap60/metrics.json) | 100 | 46.0000% | 0.665669 | 100.0000% | 0.766 |
| [contextbase/script_expand_stop-tune_dev100-cap60](contextbase/script_expand_stop-tune_dev100-cap60/metrics.json) | 100 | 51.0000% | 0.639539 | 100.0000% | 0.818 |
| [contextbase/stock_equivalent-tune_dev100-cap60](contextbase/stock_equivalent-tune_dev100-cap60/metrics.json) | 100 | 42.0000% | 0.596384 | 100.0000% | 0.849 |
| [contextbase/stop_only-tune_dev100-cap60](contextbase/stop_only-tune_dev100-cap60/metrics.json) | 100 | 43.0000% | 0.591085 | 100.0000% | 0.819 |

每个已完成配置目录保留配置指纹、逐题排名、实际模型调用（gzip）和用量。`assessment_dev293-derived` 是完整393题中的固定子集，不另发查询；其余记录标明实际查询集。40/100候选和ContextBase 16384返回预算仅为独立预算实验，不参与60候选主配置选择。

入库进度在挂载目录 `/workspace/work/miracl-scale/status.json`，事件在 `events.jsonl`。各产品入库用量文件标明本次续跑前已完成数量、尝试墙钟和累计已记录批次墙钟；原生索引与认证配置保存在该目录 runtime/backups，不进入 Git。
