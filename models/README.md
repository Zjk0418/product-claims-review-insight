# BGE 中文向量模型

本仓库包含 `bge-small-zh-v1.5` 的 ONNX 模型与分词器，用于本地语义编码、问题聚类和相近主张识别。模型不是 LLM；评论和宣传的结构化分析仍需要使用者自行配置模型 API。

来源：原模型 [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)，ONNX 转换版 [Xenova/bge-small-zh-v1.5](https://huggingface.co/Xenova/bge-small-zh-v1.5)。原模型卡标注 MIT；`bge-small-zh-v1.5/LICENSE.FlagEmbedding` 保留官方 FlagEmbedding 项目的 MIT 许可文本。

仓库中的非量化 `model.onnx` 大小为 94,851,877 字节，SHA-256：

```text
69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a
```

如果模型文件缺失，可从上述 Xenova 模型仓库获取 `onnx/model.onnx`、根目录下的 `tokenizer.json`、`config.json` 与 `tokenizer_config.json`，放到本目录的 `bge-small-zh-v1.5/` 下。替换量化模型可能影响相似度与聚类，不应直接视为等价。
