# ContextBase 原生检索阶段诊断

**已完成30题×2配置的60次原生trace诊断。结论和实例见 [REPORT.md](REPORT.md)，核验见 [final-verification.json](final-verification.json)。**

用户要求先定位现有错题的阶段断点。本轮只诊断，不新增权重扫描，不修改已归档成绩，不重新入库。

从固定100道调参题中，选择精简配置首位未命中的30题。选择顺序为：先覆盖全部5道前十无正例题，再按相对WeKnora选定配置的nDCG差值从小到大、最后以固定种子qid哈希补满。选题规则和题目在任何本轮trace查询前固定，见 [selected-queries.json](selected-queries.json) 和 [experiment-plan.json](experiment-plan.json)。这是有意选择的难例集合，不代表全部查询的统计分布。

串行采集现有两配置各30次原生Recall trace：

- `lean_source_cap100`：当前配置，关闭图、时间和模型重排，每支路进入融合前最多100条。
- `lean_control`：精简基线，同样关闭图、时间和模型重排，每支路不额外截到100。

其余仍是high原始召回预算1000、最多60条融合候选、8192返回Token预算、相同查询顺序。复用原生模型和索引，诊断耗时及模型输入均使用独立diagnostic标签，不混入正式评测。诊断结束后恢复原来的lean_source_cap100配置，并将模型记录上下文切换到idle，避免后续交互计入历史跑分。

追踪每个官方正例在原始向量、BM25、RRF融合、原生候选/评分和最终返回中的位次；区分原始池未命中、支路截断、融合前60截断与最终返回过滤。原生trace字段`reranked`在此只表示未调用模型的passthrough候选阶段，不能据字段名认定模型重排执行。

支路质量指标仅由trace列表映射官方段落ID、去重后计算，是诊断投影，不是新执行的单路基准测试。原生阶段数据和真实模型用量一起保留；重排模型调用必须为零。缺少官方正例标注不等同证明某段语义不相关。

全部完成后见 `REPORT.md`、`analysis.json`、`positive-stage-ranks.csv`，各配置summary记录逐正例位次、首位段落与最佳正例的RRF贡献，压缩trace可配合node-to-official.csv复算。代码已用覆盖各类断点的构造数据验证，并和现有官方ID评分器核对投影指标；真实原生证据以实际保存的trace为准。

```bash
/workspace/work/api-venv/bin/python miracl_zh/diagnose_fusion_stages.py \
  --settings /workspace/work/miracl-scale/runtime-settings.json \
  --results results/miracl-zh-fusion-diagnosis-20261113
```

进度在 `/workspace/work/miracl-scale/status.json`，日志为 `worker-fusion-diagnostic.log`。逐题trace原子写入，存在的trace会跳过；单实例锁防止与其他评测同时运行。代码及选题指纹变化会停止。两端数据库、索引、私有配置、检查点和备份均保留，不启动百万阶段。
