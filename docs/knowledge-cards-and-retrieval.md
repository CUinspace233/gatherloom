# 知识卡片与检索增强方案

> 状态：设计建议（待评审）  
> 日期：2026-11-09  
> 关联：[product-plan.md](product-plan.md) §4.3、§4.6、§7、§8；[roadmap.md](roadmap.md) S3–S5  
> 范围：本文只细化“知识卡片层”和“检索层”，不改变 P0 范围、既有 ADR 与多账号隔离规则。

## 1. 结论摘要

当前技术底座（FastAPI + PostgreSQL/pgvector + LangGraph + 百炼 Embedding）不需要推翻。真正值得补的是两层：

1. **知识卡片层**：把零散资料整理成“一个观点一张卡片”的原子知识单元，通过后台 consolidation 增量合并，而不是定时重写一篇大摘要。
2. **检索层**：在现有混合检索之后增加 contextual retrieval、reranker 和卡片优先/父子块检索。

两项改动的优先级：

| 优先级 | 改动 | 原因 |
|---|---|---|
| P0 | 知识卡片表 + 增量 consolidation | 直接决定“整理成有逻辑的知识卡片”这一核心体验 |
| P1 | Reranker | 在混合检索之后收益最大、改动最小 |
| P1 | Contextual retrieval（chunk 上下文头） | 对链接、截图、零散想法特别有效 |
| P2 | 卡片优先/父子块检索 | 提升回答上下文质量，依赖卡片层先落地 |

**边界**：不引入图数据库、GraphRAG、Qdrant 或成品知识库平台。卡片层是现有 `Entry` / `Chunk` / `Citation` 之上的派生层，不替代原始资料和引用链。

---

## 2. 知识卡片层

### 2.1 设计原则

1. **原子**：一张卡片只表达一个主题、方法、概念或决定；能独立读懂，不依赖前后文。
2. **有结构**：核心论点、关键点、主张、证据、开放问题分开存，不做成一整段摘要。
3. **有来源**：每条主张必须能回到 `EntryRevision` / `Chunk`；没有来源的内容不进入已确认卡片。
4. **可演化**：卡片有版本、有效期和替代关系；新资料可以合并、修订或标记冲突，不覆盖历史。
5. **人在环上**：AI 生成 `draft`，用户可确认、编辑、合并；破坏性合并默认需要用户确认。
6. **项目内聚**：首版卡片按项目生成，不跨项目合并；同一资料被多个项目复用时，各项目卡片独立。

### 2.2 数据模型

所有表继续携带 `owner_id`，使用与现有模型一致的复合外键，避免跨账号关联。

```text
knowledge_cards
  id, owner_id, project_id
  title, thesis, summary
  card_type          -- topic / concept / method / decision / question / source_digest
  status             -- draft / confirmed / stale / merged / archived
  conflict_flag      -- 是否存在未解决的矛盾
  valid_from, valid_to
  version
  input_watermark    -- 生成本版本时处理到的项目事件水位
  embedding_version
  created_by         -- ai / user
  created_at, updated_at

knowledge_card_items
  owner_id, card_id, item_no
  kind               -- key_point / claim / evidence / open_question / next_step
  text
  confidence         -- 0..1，初始值，需校准
  source_entry_id, source_revision, chunk_id
  source_quote       -- 可选，引用原文片段

knowledge_card_links
  owner_id, from_card_id, to_card_id
  relation_type      -- supports / contradicts / refines / supersedes / related / derived_from
  reason
  confidence
  status             -- suggested / accepted / dismissed
  created_at

knowledge_card_revisions
  owner_id, card_id, version
  content_snapshot   -- JSONB：标题、论点、条目、链接、来源清单
  input_watermark
  change_note
  created_at
```

说明：

- `knowledge_card_items` 是卡片的主要内容层；`knowledge_card_revisions` 保存不可变快照，用于历史回看和误合并恢复。
- `knowledge_card_links` 是显式关系，不做成“自动生成的图”。首版只用于卡片之间的支持、矛盾、细化和替代。
- 卡片本身不存储完整原文；原文继续保存在 `EntryRevision`，引用继续走 `Citation`。

### 2.3 卡片类型

| 类型 | 用途 | 例子 |
|---|---|---|
| `topic` | 一个主题的当前认识 | “pgvector 混合检索的取舍” |
| `concept` | 概念/术语解释 | “RRF（倒数排名融合）” |
| `method` | 可复用方法/步骤 | “中文长文分块策略” |
| `decision` | 已确认或待确认决定 | “首版不引入图数据库” |
| `question` | 尚未解决的问题 | “多模态 embedding 是否值得引入” |
| `source_digest` | 单篇资料的要点卡 | 某篇文章的结构化摘要 |

首版建议以 `topic` 和 `source_digest` 为主，`decision` 与现有 `Decision` 表通过链接关联，不重复存业务状态。

### 2.4 Consolidation 流程

触发事件只来自原始变化，不接受 AI 派生物自触发：

- 新 `EntryRevision` 就绪；
- `ProjectEntry` 加入或移出项目；
- `Decision` 确认或修订；
- 用户手动请求整理。

流程：

```text
1. 取水位之后的变更资料
2. 规范化与去重：URL 规范化、内容指纹、近重复检测
3. 抽取：LLM 结构化输出候选卡片、主张、实体和来源 ID
4. 匹配：与项目内已有卡片做向量 + 实体 + 标题匹配
5. 决策：新建 / 合并 / 拆分 / 替代 / 标记冲突
6. 事务写入：卡片、条目、链接、版本快照、水位
7. 索引：更新卡片向量与条目索引；原始 chunk 索引不变
8. 标记未读与待确认，不直接发布为“已确认”
```

关键规则：

- **两分钟事件合并**沿用项目总结的既有策略；单项目串行执行。
- **幂等**：同一 `input_watermark` 重复执行不产生新卡片版本；建议使用 `(owner_id, project_id, input_watermark)` 作为运行指纹。
- **合并阈值**是初始值，需在 S0/S5 调参：向量相似度高且实体重叠明显时进入候选；灰度区间交给 LLM 判断；低于阈值新建卡片。
- **合并保留历史**：被合并卡片标记 `merged`，通过 `supersedes` / `related` 链接指向目标卡片；不物理删除。
- **冲突不覆盖**：矛盾内容建立 `contradicts` 链接，设置 `conflict_flag`，进入待确认列表。
- **无来源不写入**：卡片条目必须带 `source_entry_id` / `chunk_id`；抽取结果缺少有效来源时丢弃或降级为草稿。
- **删除传播**：来源被删除或失效时，相关条目和卡片按现有删除生命周期标记失效；卡片需要重新整理或进入 `stale`。

### 2.5 卡片与现有概念的关系

| 现有概念 | 关系 |
|---|---|
| `Entry` / `EntryRevision` | 原始事实来源；卡片不替代 |
| `Chunk` | 检索证据；卡片条目引用 chunk |
| `Citation` | 回答/总结/决定的引用链；卡片条目复用同一套引用契约 |
| `SummaryRevision` | 项目级当前认识；卡片是更细的原子层，二者可互相引用 |
| `Decision` | 正式决定仍由用户确认；卡片只做知识表达，不改变决定状态 |
| `ProjectEvent` | consolidation 的输入水位来源 |

---

## 3. 检索层

### 3.1 两级索引与查询路由

```text
查询
 → 卡片混合检索（Top 10）
 → 原始 chunk 混合检索（Top 50）
 → 合并候选
 → reranker 重排（Top 50 → Top 10）
 → 上下文组装（卡片 + 父章节 + 原始片段）
 → 生成回答与引用
```

路由规则：

- 卡片命中强（分数高且与第二名有明显差距）→ 卡片优先，展开其条目引用的证据 chunk。
- 卡片命中弱或问题很具体 → 以 chunk 检索为主。
- 卡片和 chunk 都命中时，去重后合并，优先保留原始 chunk 作为引用对象。
- 引用永远指向原始 `EntryRevision` / `Chunk` 片段，不引用卡片正文。

### 3.2 Contextual Retrieval

对每个 chunk 生成 1–2 句上下文头，说明它来自哪篇资料、在讨论什么、位于哪个章节：

```text
[来自《...》，讨论 pgvector 混合检索，位于“索引选择”一节]
原始 chunk 文本……
```

落库与索引：

- `context_header` 与原始 `chunk_text` 分开存储。
- 向量化文本 = `context_header + chunk_text`。
- 关键词索引文本 = 同一拼接文本；引用展示仍只用 `chunk_text`。
- 上下文头由轻量模型批量生成，按 `revision` 缓存；重试不重复计费。
- 公开评测中，contextual embeddings 可将 Top-20 检索失败率相对降低约 35%，叠加 contextual BM25 约 49%，再叠加 reranker 约 67%。这是公开基准，需在自有评测集复测。

### 3.3 Reranker

- 混合检索先取 Top 50，再用 cross-encoder 重排到 Top 10。
- 首版优先托管 API，避免服务器 GPU 依赖：Qwen3-Reranker、Cohere Rerank 3.5、Jina Reranker v3、Voyage rerank-3 均可作为候选。
- 设定超时与降级：reranker 超时或失败时回退到 RRF 结果，不阻塞回答。
- 记录 reranker 分数、耗时和供应商，纳入 `Run` / `Usage`。
- 中文/中英混合场景优先在自有评测集比较 Qwen3-Reranker 与至少一个第三方 reranker。

### 3.4 父子块与上下文组装

- 检索使用小 chunk，回答使用父章节或卡片提供的更大上下文。
- 组装顺序：最相关证据在前；高价值内容可重复放在上下文末尾，降低“中间遗忘”。
- 控制总 token：卡片摘要 + 3–5 个证据片段优先，不把整篇文章塞入上下文。
- 区分外部证据、个人记录和 AI 推断，沿用现有引用标注规则。

### 3.5 查询改写与有限多轮

- 仅对多跳、指代不清或明显需要比较的问题做查询改写/分解。
- 最多两轮检索，不运行无限 Agent 循环。
- 保存改写前后查询、检索轨迹和引用，便于评测与排错。
- 简单事实问题不触发改写，避免额外延迟和成本。

### 3.6 评测与验收

在现有评测集基础上增加：

| 题型 | 数量 | 目的 |
|---|---:|---|
| 单跳检索 | 20 | 回归现有 Top 5 目标（≥16/20） |
| 多跳/依据链 | 10 | 验证卡片和关系是否真正改善跨资料问题 |
| 无答案 | 5 | 不编造决定或来源 |
| 卡片合并/冲突 | 10 组 | 验证抽取与合并质量 |

指标分开记录：

- 检索：Top 5 命中、nDCG@10。
- 卡片：错误合并率、漏合并率、冲突识别率（人工标注）。
- 回答：引用存在性、引用支持度、信息不足表达。
- 工程：p50/p95 延迟、reranker 超时率、单条资料整理成本。

初始目标（待 S0/S5 实测校准）：

- 单跳 Top 5 ≥ 16/20，保持现有基线。
- 加入卡片与 reranker 后，多跳题 Top 5 相比基线有可测量提升。
- 卡片错误合并率 < 5%（20 组人工标注中不超过 1 组错误合并）。
- 引用对象全部存在且属于当前账号/项目范围。

---

## 4. 与现有方案的关系

- 本文是 [product-plan.md](product-plan.md) 的增量设计，不替代 §7 逻辑模型和 §8 检索方案。
- 卡片层复用现有 `owner_id`、复合外键、游标分页、幂等和删除生命周期规则。
- `Job` / `Run` 继续承担 consolidation 的执行与重试；不新增独立调度系统。
- Embedding 继续遵循 [ADR 0001](decisions/0001-embedding-bailian-qwen.md) 的 `index_version` 规则；新增卡片索引时单独记录版本。
- 中文关键词继续遵循 [ADR 0005](decisions/0005-chinese-keyword-search-jieba.md)；本文不要求切换到 BM25 扩展。

### 明确不做

- 不引入 Neo4j / GraphRAG / Graphiti 作为首版依赖。
- 不引入 Qdrant / LanceDB 等独立向量库；当前规模继续用 pgvector。
- 不引入 RAGFlow / Onyx / Dify 等成品平台替换自研领域模型。
- 不做无限制的 Agentic RAG 或全库自动研究。
- 不把卡片正文当作引用来源；引用必须回到原始版本。

---

## 5. 落地顺序

| 阶段 | 工作 |
|---|---|
| S0 | 扩充评测集（多跳、卡片合并/冲突）；对比 2–3 个 embedding；试做 contextual header 与 reranker 小样本 |
| S3 | 结构感知分块；`context_header` 落库；近重复检测；建卡片相关表（先不启用 UI） |
| S4 | 接入 reranker；实现卡片优先/父子块检索；保存检索轨迹 |
| S5 | 实现 consolidation 任务；卡片草稿/确认/编辑/合并；冲突卡片与待确认列表；与周回顾联动 |
| P1 | PDF 解析（Docling / PaddleOCR-VL）；多模态 embedding 实验 |

依赖关系：

1. 卡片表与去重先于 consolidation。
2. contextual header 先于卡片索引，避免卡片和 chunk 使用不一致的文本表示。
3. reranker 可独立于卡片先上线，作为纯检索增强。
4. 卡片优先检索依赖卡片质量，建议在 S5 卡片人工验收通过后启用。

---

## 6. 风险与待定问题

### 风险

| 风险 | 应对 |
|---|---|
| 卡片爆炸或重复 | 合并阈值、项目内卡片上限、待确认队列、定期 consolidation |
| 错误合并 | 破坏性合并需用户确认；保留 `knowledge_card_revisions` 可恢复 |
| 抽取幻觉 | Pydantic 校验；每条主张必须有来源 ID；无来源丢弃 |
| 成本上升 | 抽取和上下文头用轻量模型；只处理变更资料；批量与缓存 |
| 卡片过期 | `valid_to`、`supersedes` 链接、来源删除后标记 `stale` |
| 删除传播 | 复用现有删除生命周期；卡片条目与引用一起失效 |
| 多账号隔离 | 所有表带 `owner_id`；复合外键；跨账号统一 404 |
| reranker 延迟 | 超时降级到 RRF；记录超时率 |
| 评测失真 | 指标分开记录，不合并成单一“准确率” |

### 待定问题

- 卡片粒度：以主题卡为主，还是允许“一个主张一张卡”？首版建议主题卡 + 多条 claim。
- 卡片是否自动确认：建议默认 `draft`，用户确认后才进入 `confirmed`。
- consolidation 频率：事件驱动 + 每日一次；是否需要每周全量校验待实测。
- reranker 供应商：按中文评测结果和延迟决定，不在本文锁定。
- 跨项目卡片复用：首版不做；出现明确需求后再评估。
- 卡片条目是否需要独立版本：首版用卡片级 JSON 快照，后续按需要细化。

---

## 7. 参考

- [Anthropic: Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
- [Qwen3 Embedding 官方博客](https://qwenlm.github.io/blog/qwen3-embedding/)
- [Qwen3 Embedding 论文](https://arxiv.org/pdf/2506.05176v1.pdf)
- [Qwen3-VL-Embedding / Reranker](https://www.modelscope.cn/models/qwen/Qwen3-VL-Embedding-2B)
- [RAPTOR 论文](https://proceedings.iclr.cc/paper_files/paper/2024/file/8a2acd174940dbca361a6398a4f9df91-Paper-Conference.pdf)
- [Sleep-Time Compute 模式](https://agentpatternscatalog.github.io/patterns/patterns/sleep-time-compute.html)
- [Best Rerankers for RAG 2026](https://futureagi.com/blog/best-rerankers-for-rag-2026/)
- [ParadeDB pg_search](https://www.pgxn.org/dist/pg_search/0.22.1/pg_search/README.html)

以上外部数据来自公开资料，具体效果需在自有中文评测集复测后写入 [evaluation/](evaluation/README.md)。
