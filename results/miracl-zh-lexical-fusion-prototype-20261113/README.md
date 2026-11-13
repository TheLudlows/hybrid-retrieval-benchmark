# 中文BM25查询处理与融合权重：独立实验版

用户要求继续定位后的优化。本轮保留原版实验结果，使用独立源码副本验证查询词处理与真正的线性加权RRF。**新增EXPERIMENT参数属于本轮实验补丁，不是原版ContextBase已有配置；本轮结果不能改写原版产品成绩。**

## 实测线索

原生jieba将“哥斯达黎加的国歌是什么？”切成“哥斯达黎加、的、国歌、是、什么、？”；正例原文为繁体“高貴的家園，你美麗的旗幟……為哥斯大黎加國歌”，原生jieba将关键部分切成“哥斯大黎、加國歌”。繁简、地区用词和词边界均可能导致关键词未直接匹配。这是具体实例，不代表所有BM25错题只有这一种原因。

## 固定六组与评估

所有组复用同一10万段原文、索引、BGE-M3及原始查询向量化，保持图/时间/模型重排关闭、原始high预算1000、每支路融合前最多100、最多60候选、返回Token8192、查询并发1及4次非计分预热。

| 配置 | BM25查询处理 | 语义/关键词RRF权重 |
| --- | --- | --- |
| stock_equivalent | 原样 | 1/1，原版等价对照 |
| stop_only | 删除固定问句停用词 | 1/1 |
| script_expand_stop | 原文＋OpenCC简体/繁体/台湾用词变体，去固定停用词 | 1/1 |
| dense70 | 原样 | 0.7/0.3 |
| dense97 | 原样 | 0.97/0.03 |
| script_expand_stop_dense70 | 繁简变体＋去停用词 | 0.7/0.3 |

这不是穷举网格；只按固定100道调参题的nDCG@10、Hit@1、注册顺序选择，冻结后跑393题、派生293题及额外200题。查询此前已有其他配置结果，全程按探索性复核报告，不声称盲测。额外预算和算法补丁独立于原先两端各6组的公平对比。

只修改BM25查询参数和查询词过滤，语料及现有全文索引不重建、不转换；BGE-M3仍接收原始查询。扩展使用固定`opencc-python-reimplemented==0.1.7`、t2s/s2t/s2twp配置。每个新配置用独立trace探针验证原生API实际RRF贡献和相同查询向量，诊断调用单独记账。关闭实验开关的对照必须逐题复现原版100题完整排名，否则停止。

## 实现、核验和恢复

补丁：[contextbase-experimental.patch](contextbase-experimental.patch)。相对固定版本源码应用，不包含凭证。方案、源码指纹、原版文件指纹和依赖版本见[experiment-plan.json](experiment-plan.json)。原版目录不改，服务临时切换到独立副本；正常结束或捕获异常时恢复原版源码和原`lean_source_cap100`配置，并验证一个原版完整排名探针。数据库、索引、检查点及备份保留，不做百万段验证。

5项功能单元检查已通过。仓库整套lint已执行，但缺少前端`@eslint/js`导致未全通过；目标Python检查还记录新增实验模块的I001导入排序提示，见[implementation-checks.json](implementation-checks.json)。这是实验补丁，未宣称可以直接合并为生产功能。没有新增HTTP接口或改变SDK请求结构。

```bash
/workspace/work/api-venv/bin/python miracl_zh/run_lexical_fusion_prototype.py \
  --settings /workspace/work/miracl-scale/runtime-settings.json \
  --results results/miracl-zh-lexical-fusion-prototype-20261113
```

私有设置、认证文件及模型权重不上传Git。状态仍在`/workspace/work/miracl-scale/status.json`，日志为`worker-lexical-prototype.log`。正式模型用量与探针/预热区分；重排调用必须为零。完成后结果为RESULTS.md和comparison.json；复原证据为restored-stock.json。环境整体停止不能保证finally执行，若发生此类中断，应先按原始runtime-settings启动原版服务，再用相同指纹恢复本轮检查点。
