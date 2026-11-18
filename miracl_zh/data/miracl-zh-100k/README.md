# MIRACL v1.0 中文开发集（100,000 段）

这是 LLM 入库与模型重排正式评测使用的同一份数据。压缩语料约 17.8 MB，保留原始段落 ID、标题和正文。语料由官方中文语料抽样，固定种子为 20261112，包含此前 10,000 段子集和额外训练题的标注段落。官方开发集的 393 道题与 3,928 条标注按原字节归档。抽样参数、父子集和来源 SHA-256 在 [subset.json](subset.json)。

在仓库根目录运行 `bash miracl_zh/local_llm_ingest_rerank.sh verify` 可以校验文件指纹、段落数和官方标注覆盖。实际评测的配置和启动方法见[本地复现说明](../../README.md)。

## 资料来源与署名

- 段落采自 MIRACL Corpus v1.0 中文发布版，固定于修订版 `d921ec7e349ce0d28daf30b2da9da5ee698bef0d`。MIRACL 的处理和此处的抽样均保留文章标题。 [官方语料卡及维基百科转储链接](https://huggingface.co/datasets/miracl/miracl-corpus/blob/d921ec7e349ce0d28daf30b2da9da5ee698bef0d/README.md)
- 题目与标注来自 MIRACL v1.0 中文开发集，固定于修订版 `5be20db9509754dadad47689368639fcec739c00`。MIRACL 发布许可为 Apache License 2.0。 [官方题目与标注](https://huggingface.co/datasets/miracl/miracl/tree/5be20db9509754dadad47689368639fcec739c00/miracl-v1.0-zh) · [许可文本](LICENSE)
- 原文作者为中文维基百科各条目的贡献者，源转储日期为 2022-03-01。可用每段的 `title` 访问对应的 `https://zh.wikipedia.org/wiki/<URL-encoded-title>` 并查看条目历史及贡献者。原文适用维基百科的 CC BY-SA 3.0 等原始条款。 [维基百科版权与署名](https://zh.wikipedia.org/wiki/Wikipedia:版权信息) · [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/)
- MIRACL 引用为 Zhang et al. (2023), *MIRACL: A Multilingual Retrieval Dataset Covering 18 Diverse Languages*。 [论文](https://aclanthology.org/2023.tacl-1.63/)
