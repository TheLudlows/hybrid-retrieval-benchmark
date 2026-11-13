# ContextBase 向量、BM25与融合阶段诊断

选自固定100道调参题的30道错题：先覆盖前十无正例的题，再按相对WeKnora的nDCG缺口补满。这是有意选择的难例，不代表全部393题的平均表现。不改原成绩、不重新选参。

两配置仅相差每支路进入融合前是否截到100。原生向量/BM25仍先各按high预算检索，原生trace保存其支路列表、RRF列表、60候选及最终返回。诊断耗时不计入正式性能。

| 配置/阶段 | Hit@1 | Hit@10 | Recall@10 | nDCG@10 |
| --- | ---: | ---: | ---: | ---: |
| lean_source_cap100/semantic | 70.00% | 100.00% | 95.83% | 0.807894 |
| lean_source_cap100/bm25 | 0.00% | 26.67% | 20.56% | 0.083990 |
| lean_source_cap100/fusion | 0.00% | 83.33% | 64.00% | 0.321454 |
| lean_source_cap100/post_scoring_candidates | 0.00% | 83.33% | 64.00% | 0.321454 |
| lean_source_cap100/final | 0.00% | 83.33% | 64.00% | 0.321454 |
| lean_control/semantic | 70.00% | 100.00% | 95.83% | 0.807894 |
| lean_control/bm25 | 0.00% | 26.67% | 20.56% | 0.083990 |
| lean_control/fusion | 0.00% | 83.33% | 61.50% | 0.312055 |
| lean_control/post_scoring_candidates | 0.00% | 83.33% | 61.50% | 0.312055 |
| lean_control/final | 0.00% | 83.33% | 61.50% | 0.312055 |

raw-arm指标仅从trace列表映射官方段落ID、去重后计算，不是修改产品开关后的单路正式跑分。fusion是候选截断前的原生RRF列表。post_scoring_candidates是未调用模型的60候选及原生综合评分后的列表；最终Token过滤和官方段落去重仍会改变深度。

## 断点统计

### lean_source_cap100

- 至少一条原始支路正例第1、融合后非第1：21题。
- 至少一条原始支路正例前10、融合后不在前10：5题。
- 与已归档最终完整排名一致：30/30题。
- 逐正例分类：`{"returned_below_top10": 30, "returned_2_to_10": 38}`。

### lean_control

- 至少一条原始支路正例第1、融合后非第1：21题。
- 至少一条原始支路正例前10、融合后不在前10：5题。
- 与已归档最终完整排名一致：30/30题。
- 逐正例分类：`{"returned_below_top10": 31, "returned_2_to_10": 37}`。

## 融合降序实例

| 查询 | 正例段落ID | 语义位次 | BM25位次 | 融合位次 | 最终位次 |
| --- | --- | ---: | ---: | ---: | ---: |
| 哥斯达黎加的国歌是什么？ | 1576876#0 | 1 | None | 36 | 36 |
| 世界上有哪些共产主义国家？ | 80338#1 | 1 | 16 | 5 | 5 |
| 陈水扁属于哪一党派？ | 11466#0 | 1 | None | 16 | 16 |
| 蒙古发现哪些化石？ | 507736#2 | 1 | 17 | 8 | 8 |
| 日本有哪些知名作家？ | 218216#13 | 1 | 40 | 7 | 7 |

完整证据见 [analysis.json](analysis.json)、[逐正例位次CSV](positive-stage-ranks.csv) 及各配置summary文件；压缩trace配合 [node-to-official.csv](node-to-official.csv) 可复算。RRF贡献在每题first_returned_evidence及best_positive_fusion_evidence中，使用原生1/(60+rank)计算。未标注为正例不等同证明语义不相关。

所有模型记录按diagnostic独立run ID保存，模型重排调用必须为0。最终恢复诊断前的lean_source_cap100配置，保留两端全部原生数据库、索引、配置、检查点及备份。仅依据此次选择难例定位问题，不据此宣称哪种权重或分词方案能在全体查询上提升；没有执行第二项权重扫描。
