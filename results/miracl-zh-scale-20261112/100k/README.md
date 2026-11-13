# 100k 阶段结果

所有数值来自已完成并落盘的阶段；缺失阶段仍在运行或尚未开始。开发集结果已被检查，不能声称盲测。新增200题来自官方 train，仅未用于本轮调参。

| 产品 / 配置 / 查询集 | 题数 | Hit@1 | nDCG@10 | Recall@100 | p95 秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| [contextbase/default-assessment_dev293-derived](contextbase/default-assessment_dev293-derived/metrics.json) | 293 | 65.5290% | 0.797539 | 99.6303% | 11.320 |
| [contextbase/default-dev393-cap60](contextbase/default-dev393-cap60/metrics.json) | 393 | 68.7023% | 0.809471 | 99.7243% | 11.276 |

每个已完成配置目录保留配置指纹、逐题排名、实际模型调用（gzip）和用量。`assessment_dev293-derived` 是完整393题中的固定子集，不另发查询；其余记录标明实际查询集。40/100候选和ContextBase 16384返回预算仅为独立预算实验，不参与60候选主配置选择。

入库进度在挂载目录 `/workspace/work/miracl-scale/status.json`，事件在 `events.jsonl`。各产品入库用量文件标明本次续跑前已完成数量、尝试墙钟和累计已记录批次墙钟；原生索引与认证配置保存在该目录 runtime/backups，不进入 Git。
