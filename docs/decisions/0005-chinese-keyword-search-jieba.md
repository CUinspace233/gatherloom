# 0005 中文关键词检索采用应用侧 jieba

- 日期：2026-11-09
- 状态：已采纳

## 背景
PostgreSQL 无内置中文分词；zhparser 需自建镜像编译扩展，pg_bigm/pg_trgm 排序较粗、索引较大。

## 决策
- Python 侧用 jieba（搜索引擎模式）对 chunk 文本分词，以空格连接后写入 `to_tsvector('simple', ...)` 的生成列/普通列，并建 GIN 索引。
- 查询使用同一分词器与词典生成 `tsquery`；英文按小写与原样保留。
- 支持账号无关的全局自定义词典（技术名词、项目名），词典版本写入 chunk，词典变更后后台重算 tsvector。
- 与 pgvector 语义路用 RRF（倒数排名融合）合并，所有查询先约束 owner_id（及 project_id）。

## 后果
- 不改动 PostgreSQL 官方镜像；分词逻辑可单元测试。
- 分词版本与 Embedding 索引版本分别管理。

## 待验证（S0/S4）
- [ ] 20 道检索题关键词路与混合检索的 Top 5 命中。
- [ ] 中英混合词（如 "SwiftUI 状态管理"）的召回。
