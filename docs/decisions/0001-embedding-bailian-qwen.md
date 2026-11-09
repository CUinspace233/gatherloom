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
- [ ] 该模型在国际站是否可用；不可用时的替代（同系列其他型号或中国站端点）及跨境延迟。
- [ ] 支持的维度选项，选定维度（兼顾召回与存储）。
- [ ] 单次批量条数与单条 token 上限，决定分块上限与批处理大小。
- [ ] 计价（带日期写入价格配置）。
- [ ] 20 道中文/中英混合检索题的语义路 Top 5 召回。
