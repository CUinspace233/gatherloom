# 0001 Embedding 使用百炼 qwen3.7-text-embedding-flash

- 日期：2026-11-09
- 状态：已采纳，S0 待验证

## 背景
DeepSeek 生成 API 不等同于具备 Embedding 能力；服务器不假设有 GPU，需要托管的中文 Embedding。

## 决策
使用阿里云百炼 `qwen3.7-text-embedding-flash`。服务器位于海外/港澳（ADR 0007），默认调用国际站（新加坡）端点。该配置记为 `index_version = 1`。

## 后果
- `EmbeddingProvider` 接口独立于生成模型；配置项：provider、endpoint、model、dimension、index_version。
- pgvector 列维度按选定 dimension 固定；更换模型或维度必须新建索引版本、回填、验证后切换，不混用向量空间。
- 文档与查询文本可能需要不同的 text_type / 指令前缀，按官方文档设置。

## 待验证（S0）
- [ ] 该模型在国际站是否可用；不可用时的替代（同系列其他型号或中国站端点）及跨境延迟。2026-11-10：当前 key 属于中国站，模型在中国站可用（本机调用约 200 ms）；国际站与服务器跨境延迟待部署时测。
- [x] 选定维度：默认输出 1024 维，即 `index_version = 1` 的 `vector(1024)`。其他维度选项未比较。
- [x] 单次批量上限 25 条（26 条返回 400），Worker 按 25 条分批。单条 token 上限未测；分块上限 800 字远低于常见上限。
- [x] 计价：¥0.125/百万 token（中国站，2026-11-10），写入 `server/app/providers.py` 的价格表。
- [ ] 20 道中文/中英混合检索题的语义路 Top 5 召回。
