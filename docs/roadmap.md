# Gatherloom 分阶段开发路线（细化）

> 基于 [product-plan.md](product-plan.md) v1.2 与 [decisions/](decisions/)。首版 P0 范围不缩减。
> 每阶段：目标 → 任务（单项约 ≤ 4h）→ 出口检查 → 风险。工时为单人有效工时初始估算，S0 后换算为日历。
> 出口检查中标注 `[T1]`–`[T9]` 的条目对应 product-plan §11.2 的 9 项必须自动化验证。

## 总览

| 阶段 | 工时 | 一句话出口 |
|---|---:|---|
| S0 技术验证与骨架 | 12–20h | 关键假设逐项有实测结论，仓库骨架可运行 |
| S1 账号与项目 | 24–36h | 两个账号独立创建项目，越权测试通过 |
| S2 可靠收集 | 28–40h | 断网/退出/切账号不丢、不串上传 |
| S3 整理与索引 | 24–36h | Worker 崩溃可恢复，资料加入项目后可搜索 |
| S4 搜索与问答 | 24–36h | 检索目标与无答案题达标，项目范围不越界 |
| S5 项目认识与回顾 | 24–36h | 从材料到正式决定、周回顾完整贯通 |
| S6 内测与部署 | 20–32h | 完整验收通过，TestFlight 邀请少量用户 |

---

## S0 技术验证与骨架

**目标**：把方案中"待实测"的关键假设变成结论，并建立可运行的空骨架。

### iOS 与签名（ADR 0003、0004、0008）
- [ ] 记录 Mac、Xcode、测试 iPhone 的 iOS 版本，确认最低 iOS 17。
- [ ] 免费团队签名下创建主 App + Share Extension，验证 App Group 共享目录读写。
- [ ] 验证 Keychain 共享访问组：主 App 写入、扩展读取。
- [ ] Vision OCR 原型：10 张真实中文截图，记录识别质量、耗时；与 Tesseract 对比。
- [ ] Safari JS 预处理原型：公众号、知乎、普通博客各 3 篇，记录正文完整度、耗时、扩展内存。
- [ ] 真机通过 HTTPS 请求服务器 `/healthz`。

### 服务端与模型（ADR 0001、0002、0005、0007）
- [ ] 服务器清单：系统、CPU、内存、磁盘、Docker/Compose、域名、证书、备份目的地。
- [ ] 测量服务器到百炼国际站、DeepSeek 的延迟。
- [ ] 百炼：确认 `qwen3.7-text-embedding-flash` 国际站可用、维度、批量上限、计价。
- [ ] 准备评测样本：两项目 × 15–20 条材料（见 [evaluation/](evaluation/README.md)），20 道检索题、5 道无答案题。
- [ ] jieba + `simple` tsvector 原型；语义路 / 关键词路 / RRF 三种方式的 Top 5 命中对比。
- [ ] 中文分块策略：按段落 + 标题，比较 2–3 组块大小与重叠，锁定初始值。
- [ ] 生成模型选型：light / heavy 档各 10 条样本，比较 JSON 合法率、字段正确率、质量、延迟、成本，确定路由配置。

### 仓库骨架
- [ ] 目录：`ios/`、`server/`、`deploy/`、`scripts/`（结构见 product-plan §13）。
- [ ] `server/`：uv + `pyproject.toml` + 锁文件；FastAPI 空应用、`/healthz`；ruff、pytest、mypy 配置。
- [ ] 锁定版本：FastAPI、SQLAlchemy、Alembic、LangGraph、checkpointer、pgvector、jieba。
- [ ] `deploy/compose.yaml`：api、worker、scheduler（同镜像不同命令）、postgres（pgvector 镜像）。
- [ ] `ios/`：Xcode 工程、App / ShareExtension / SharedCapture target、SPM 依赖锁定。
- [ ] CI：服务端 lint + test。

**出口检查**
- 真机可请求 HTTPS；一组中文材料能提取、OCR 与检索；配置清单（模型 ID、维度、预算、服务器）填写完整。
- 每个 ADR 的"待验证（S0）"项均有结论；不通过的项已更新 ADR 或调整方案。

**风险**：免费团队不支持 App Group → 按 ADR 0008 推迟扩展可靠队列验证；百炼国际站无该模型 → 更新 ADR 0001。

---

## S1 账号与项目模型

**目标**：所有权与隔离先于任何资料 API。

### 服务端
- [ ] Alembic 初始迁移：User（时区默认 Asia/Shanghai）、Session、InviteCode、Project、ProjectQuestion。
- [ ] 所有用户资源表带 `owner_id`；关联表用 `(owner_id, id)` 复合外键模式（后续表沿用）。
- [ ] 密码哈希（argon2）、访问令牌（短时）+ 刷新令牌（可撤销、轮换）。
- [ ] `POST /v1/auth/register|login|refresh|logout`、`GET /v1/me`；登录限流。
- [ ] `current_owner` 依赖：owner 只从会话取得；Repository 层所有查询强制带 owner 条件。
- [ ] 项目 API：`GET/POST /v1/projects`、`GET/PATCH /v1/projects/{id}`（目标、active/archived）。
- [ ] 问题 API：`GET/POST /v1/projects/{id}/questions`、`PATCH /v1/questions/{id}`。
- [ ] 跨账号访问统一 404；游标分页工具。
- [ ] OpenAPI 导出 + 契约测试骨架。
- [ ] 运维命令：生成邀请码、重置密码。
- [ ] 跨账号测试夹具：两个账号、每账号两个项目。

### iOS
- [ ] API 客户端（async/await、令牌刷新、错误映射）。
- [ ] Keychain 按账号保存凭证（共享访问组）。
- [ ] 登录 / 邀请注册页；四 Tab 导航（记录 / 收集箱 / 项目 / 搜索）+ 头像进入设置。
- [ ] 项目列表、创建项目、项目详情占位（目标、待解问题增改与解决）。

**出口检查**
- 两个账号各自创建项目与问题，互不可见。
- [T2] 账号 A 对 B 的项目、问题读取或修改全部返回 404（后续阶段对每类新资源扩展该测试）。

**风险**：令牌刷新与 Keychain 共享在扩展中的行为差异 → S2 前用 S0 原型复测。

---

## S2 可靠收集

**目标**：本地已确认保存的内容不丢失、不重复、不串账号。

### 服务端
- [ ] 迁移：Entry、EntryRevision（原始文本/文件引用、备注、内容指纹）、Attachment、ProjectEntry、Job（先建表，S3 实现 Worker）。
- [ ] `POST /v1/entries`：文字 / URL / 单图（multipart 落盘、10 MiB 限制、校验和）；附带 `client_ocr`、`client_extract` 可选字段。
- [ ] `client_submission_id` 幂等：同账号同键返回同记录；Entry 与 Job 同一事务登记。
- [ ] 附件先写私有临时文件、校验后关联；定期清理孤立临时文件。
- [ ] 相同内容不同 submission_id → 返回"可能重复"提示，不合并。
- [ ] `GET /v1/entries?inbox=true`、`GET/PATCH/DELETE /v1/entries/{id}`、`GET /v1/entries/{id}/revisions`。
- [ ] `PUT/DELETE /v1/projects/{id}/entries/{entry_id}`，校验同账号。
- [ ] `GET /v1/attachments/{id}` 认证读取。

### iOS：SharedCapture（App 与扩展共用）
- [ ] Outbox：App Group 下按账号分目录；每个提交一个目录（内容文件 + 原子写入的 manifest）。
- [ ] 提交状态机：本地已保存 → 上传中 → 服务器已保存 → 可检索 → 整理完成 / 部分失败。
- [ ] Vision OCR 封装（降采样、行 + 坐标、失败不阻塞）。
- [ ] 上传器：从文件上传、指数退避；主 App 进入前台时排空队列。
- [ ] 账号切换 / 退出：暂停旧账号任务，提示待上传数量，可显式删除本机副本。

### iOS：界面与扩展
- [ ] 记录页：输入框、粘贴链接、选择截图、个人备注、归属选择（收集箱 / 最近三个项目）、保存后清空并提供"查看记录"。
- [ ] 收集箱列表与分配项目；资料详情（原始内容、备注、OCR/正文、状态）。
- [ ] Share Extension：预览、备注、归属（默认收集箱）、保存；多附件时提示选择一项。
- [ ] Safari JS 预处理文件接入（ADR 0004），扩展内 OCR（ADR 0003）。
- [ ] 扩展提交后短时尝试上传，失败留给主 App。

**出口检查**
- [T1] 同账号重复提交同幂等键只创建一条；不同账号同键相互独立。
- [T8] 账号退出与切换不会将旧账号待上传内容提交到新账号。
- [T9]（部分）文件大小限制有效。
- 真机：Safari 分享、截图分享、离线新增后恢复上传；20 次断网 / 强退 / 恢复场景无已确认内容丢失。

**风险**：扩展内存与超时 → OCR 与正文提取失败时降级为主 App 补做。

---

## S3 自动整理与索引

**目标**：可靠的后台执行，资料加入项目后可搜索。

- [ ] Job 认领：`SELECT … FOR UPDATE SKIP LOCKED` + 租约 + 心跳；退避重试（上限 3 次）；不可重试错误分类。
- [ ] 进程：`worker`、`scheduler` 启动命令；全局并发 2、单账号并发 1（可配置）。
- [ ] LangGraph + PostgreSQL checkpointer 接入；thread_id 含 owner_id 与 run_id（ADR 0006）。
- [ ] Run / Usage 表：provider、model、prompt_version、用量、费用估算、错误。
- [ ] `LLMProvider`（DeepSeek、千问）与任务路由配置（ADR 0002）；Pydantic 校验 + 一次修复。
- [ ] `EmbeddingProvider`（百炼）与 index_version（ADR 0001）。
- [ ] 正文兜底：服务端抓取（SSRF 防护：协议、私有/回环/链路本地/元数据地址、DNS 与每次重定向校验）+ 正文提取，100,000 字符上限。
- [ ] OCR 兜底：Tesseract 适配器（仅在无 `client_ocr` 时）。
- [ ] EntryIngestionGraph：提取兜底 → 标题/摘要（light）→ 分块 → jieba tsvector + Embedding → 状态更新；节点幂等写入。
- [ ] Artifact / Chunk 表（来源版本、加工类型、位置、分词与索引版本）。
- [ ] `GET /v1/entries/{id}/processing`、`POST /v1/entries/{id}/retry`（复用有效任务）。
- [ ] iOS：资料详情显示处理状态、失败步骤与重试。
- [ ] 结构化日志：run_id、阶段、延迟；不记录正文与密钥。

**出口检查**
- [T4]（入库部分）加工中重启 Worker 可恢复，发布结果不重复。
- [T9] URL 抓取拒绝内部地址与恶意重定向。
- 失败可重试；资料加入项目后可被检索命中。

**风险**：checkpointer 与 jobs 状态不一致 → 对外状态只从领域任务投影，崩溃恢复测试覆盖节点间中断。

---

## S4 搜索与引用问答

**目标**：找得到、引得准、不越界、不编造。

- [ ] 检索服务：owner（+ project）过滤 → 关键词路 + 语义路（精确向量检索）→ RRF 融合。
- [ ] `GET /v1/search`：项目、类型、时间筛选；游标分页。
- [ ] iOS 搜索页：筛选、结果片段高亮、打开资料及所属项目。
- [ ] Conversation / Message / Citation 迁移；`GET/POST /v1/projects/{id}/conversations`、`POST/GET /v1/conversations/{id}/messages`、`GET /v1/runs/{id}`（202 + run_id）。
- [ ] ProjectAnswerGraph：校验范围 → 检索 → 证据整理（区分外部资料 / 个人记录 / 正式决定）→ 回答（heavy）→ 引用校验（ID 存在、归属、项目、版本）。
- [ ] 信息不足判定与表达；不联网补充。
- [ ] iOS 问答页：会话、编号引用、引用卡片（标题、类型、时间、片段、跳转原文定位）。
- [ ] 评测脚本：检索 Top 5、无答案题、引用存在性；结果写入 `docs/evaluation/`。

**出口检查**
- 20 道检索题至少 16 道 Top 5 命中；5 道无答案题不编造决定或来源。
- [T3] 项目问答不使用当前项目范围外资料，全局搜索仍能找到该账号内容。
- [T2]（扩展）检索结果、对话、引用不泄露其他账号片段。

**风险**：召回不足 → 先调分块与词典，再评估多轮检索（至多两轮）。

---

## S5 项目认识与回顾

**目标**：从材料到正式决定和周回顾完整贯通。

- [ ] ProjectEvent 表与水位；仅原始内容、归属、问题、正式决定变化产生事件。
- [ ] 总结：2 分钟事件合并、单项目串行、按水位发布 SummaryRevision（内容、来源、覆盖说明、变化说明）；旧水位不覆盖新版本。
- [ ] `GET /v1/projects/{id}/summaries`；iOS 项目概览（目标、AI 总结与覆盖时间、待解问题、待确认决定数、最新回顾、最近动态）。
- [ ] 关联建议：同项目"相关资料""与问题相关"，每条 ≤3，含理由；`GET /v1/projects/{id}/relations`、`PATCH /v1/relations/{id}`；dismiss 后同版本不重复。
- [ ] 决定草稿：生成条件、每项目每日 ≤3、主题 + 来源指纹去重；`GET /v1/projects/{id}/decisions`、`PATCH /v1/decisions/{id}`（状态迁移与版本校验）。
- [ ] iOS 待确认列表（新草稿 / 依据已变化）、编辑确认 / 拒绝 / 稍后、正式决定历史与修订。
- [ ] 周回顾：scheduler 按用户时区周一 09:00 扫描上一自然周；有变化才生成；周期唯一键。
- [ ] 手动回顾：上次结束至今（首次自项目创建）；`GET/POST /v1/projects/{id}/reviews`、`PATCH /v1/reviews/{id}/read`；未读标记。

**出口检查**
- [T4] 旧任务不覆盖新总结。
- [T6] AI 草稿不自动成为正式决定；重复确认不生成两条决定。
- [T7] 同一周调度重试只生成一份自动回顾；无变化不生成空回顾。
- 人工检查 5 次迭代：不把草稿称为已确认决定，变化有依据。

**风险**：总结漂移 → 输入固定包含目标、正式决定与原始证据，版本可回看。

---

## S6 内测与部署收尾

**目标**：可交给少量独立用户使用。

- [ ] 生产 Compose：HTTPS 反向代理为唯一入口，数据库与文件卷不暴露；资源与并发按 S0 清单设置。
- [ ] 配额与限流：每账号每日提交、问答、模型预算；上传大小与登录频率。
- [ ] 删除生命周期：立即排除检索 → 取消未完成加工 → 清理附件、向量、产物、引用片段 → `purge_checkpoints`（ADR 0006）；引用失效标记；正式决定标记"依据已变化"。
- [ ] 备份：每日数据库 + 附件 + 必要密钥，保留 14 天；恢复演练（恢复后重放删除清单）。
- [ ] 购买 Apple 开发者账号，配置正式签名与 TestFlight（ADR 0008）。
- [ ] 在中国大陆网络下真机测试上传与问答延迟（ADR 0007）。
- [ ] 注册页数据流向告知（DeepSeek、阿里云）。
- [ ] 使用说明与运维手册（邀请码、密码重置、恢复）。

**出口检查**
- [T5] 移出项目和删除后不再检索命中；受影响引用与派生物正确失效。
- 数据库与附件可从备份恢复，并恢复删除状态。
- product-plan §11.3 真机与人工验收表全部记录结果。
- 邀请少量独立账号通过 TestFlight 使用。

## 首个纵向切片（跨 S1–S4）

在扩展 URL、截图和分享扩展前，先打通：

```text
账号 A 登录 → 创建项目和一个问题 → iPhone 写一条想法，断网保存 → 恢复上传
 → Worker 生成摘要并索引 → 项目内提问，引用该想法 → 账号 B 无法访问这条资料
```
