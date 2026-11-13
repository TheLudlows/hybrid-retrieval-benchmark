# ContextBase 原生配置组合追加验证

用户授权在精简配置上验证两个组合。原先[两端各6组的主实验](../miracl-zh-native-no-rerank-20261113/README.md)独立保留，本轮是ContextBase额外探索，不能包装为相同调参预算的产品对比。

## 冻结方案

复用同一100,000段语料和完整索引，保留原生Recall high、最多60条融合候选、8192返回Token预算。全部关闭图、时间和模型重排，继续使用相同BGE-M3向量化、CPU配置、顺序、并发1及每配置4次非计分预热。

| 配置 | 额外改动 |
| --- | --- |
| lean_control | 无；同期重跑精简配置作对照 |
| lean_semantic_priority | `RECALL_STRATEGY_BOOSTS=semantic:high` |
| lean_source_cap100 | `RECALL_MAX_CANDIDATES_PER_SOURCE=100` |

三组都跑相同固定100道调参题；仅按nDCG@10、Hit@1和注册顺序选取，完全相同优先对照。冻结后再跑完整393题、派生293题以及额外200题。**这些查询此前已有其他配置成绩，后续结果都是探索性复核，不能称为新的盲测。**不在后两组查询上重新选参数。

所有改动只通过产品原生配置实现，不修改产品算法。语义优先是原生支路boost，不等同纯粹设置RRF线性权重。启动后核对Bank有效开关及原生启动生成的api.env；重启预检、预热和正式采集均要求实际模型重排调用为零，正式题必须有Embedding记录。

计划和代码指纹：[experiment-plan.json](experiment-plan.json)、[SHA-256](experiment-plan.sha256)。所有指标由已有bench.py用官方段落ID重算，报告Hit、nDCG@10、Recall@10/100、深度、p50/p95/p99和实际用量。此前产品结果只作明确标注的参照；不把不同时间窗口的耗时当成同期配对实验。

## 执行和恢复

等待主实验完成后持有同一单实例锁，串行使用原索引。不重新入库，不启动百万阶段，也不删除两端任何原生数据、私有配置、检查点或备份。

```bash
/workspace/work/api-venv/bin/python miracl_zh/verify_contextbase_configs.py \
  --settings /workspace/work/miracl-scale/runtime-settings.json \
  --primary results/miracl-zh-native-no-rerank-20261113 \
  --results results/miracl-zh-contextbase-config-followup-20261113
```

本环境进度仍在 `/workspace/work/miracl-scale/status.json`，日志为 `worker-config-followup.log`。配置指纹一致时自动跳过完成的题和阶段，代码改变会停止。模型与服务须先恢复；目录持久保存不保证进程跨环境重启存活。最终结果入口为执行完成后生成的 `RESULTS.md` 和 `comparison.json`，不以方案冻结或任务启动代替完成。
