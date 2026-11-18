# MIRACL 中文 LLM 入库与模型重排

正式任务使用 [固定 10 万段与 393 道查询](formal-plan.json)。ContextBase 的独立索引已开始导入。首批 16 段已持久化，并核对了原生文档、向量行和实际模型用量。WeKnora 的正式导入排在 ContextBase 之后。这里的状态是发布时的快照。

- [正式任务状态快照](formal-status-at-publication.json) 与 [服务配置读回](formal-runtime.json)
- [8 段预检报告及逐题数据](smoke/REPORT.md) 与 [独立核验](smoke-verification.json)
- [脚本配置及本地复现步骤](../../miracl_zh/LLM_INGEST_RERANK.md)

两端预检各完成 8 段导入。ContextBase 同步抽取事实并写入向量，WeKnora 在原文之外成功写入 8 条生成问句的向量和全文索引。首轮 WeKnora 并发 4 有一次模型连接重置触发回退，脚本把对应请求记为失败。延长模型服务的连接保活后，以同一组预检索引重跑全部六个窗口，48 次查询均成功并实际调用了重排。两轮记录保存在各窗口的 attempt 目录，其中附上了首次失败的窗口。预检样本按已标注正例选取，只用于检验流程。

两端正式任务使用同一组 10 万段，各自建库。正式成绩将在正式索引和查询窗口完成后从落盘记录生成。
