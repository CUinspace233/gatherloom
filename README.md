# Gatherloom（知织）

> Gather ideas. Weave understanding.

围绕项目，收集想法、连接资料、沉淀有据可查的认识。

A project-driven personal knowledge workspace with iOS capture, source-grounded answers, and continuous reflection.

## 状态
Gatherloom 支持邀请码注册、项目与问题管理，以及离线保存的想法、链接和截图（含系统分享扩展与本机文字识别）。保存到服务器后，后台 Worker 自动分块、建立关键词与语义索引，并生成 AI 知识卡片。最低支持 iOS 17。

## iOS 开发

从仓库根目录编译 iOS 设备版。该构建只校验编译，跳过签名，无需 iPhone 或模拟器运行时。

```sh
xcodebuild -project ios/Gatherloom.xcodeproj -scheme Gatherloom \
  -destination 'generic/platform=iOS' -derivedDataPath ios/DerivedData \
  CODE_SIGNING_ALLOWED=NO build
```

1. 打开 `ios/Gatherloom.xcodeproj`，在 Xcode 的 **Settings → Apple Accounts** 登录 Apple 账号。
2. 用数据线连接 iPhone，解锁并按提示信任 Mac。配对后在 iPhone 的 **设置 → 隐私与安全性 → 开发者模式** 启用开发者模式。
3. 在 `Gatherloom` target 的 **Signing & Capabilities** 中启用自动签名并选择 Personal Team。在运行目标中选择 iPhone，按 `⌘R` 安装和启动。若 Xcode 提示 Bundle Identifier 已被占用，改为自己的唯一值。

工程定义位于 `ios/project.yml`。修改 target 或增删源文件后，从仓库根目录运行 `xcodegen generate --spec ios/project.yml`，并提交重新生成的工程。可在本机的 `ios/Config/LocalSigning.xcconfig` 中设置 `DEVELOPMENT_TEAM = 你的 Team ID`，让各 target 共用签名团队。Git 忽略该文件。

### 真机本地联调

让 Mac 和 iPhone 连接同一个 Wi-Fi。在 `server/` 目录启动服务，将两台设备的局域网 IPv4 填入下方命令。服务只接受所列地址的请求。

```sh
GATHERLOOM_DEV_CLIENTS='<Mac IPv4>,<iPhone IPv4>' \
  uv run --locked uvicorn app.main:app --host '<Mac IPv4>' --port 8000 --reload --no-proxy-headers
```

App 默认连接正式服务器 `https://gatherloom.cuinspace.com`；Release 版只用这个地址，不显示「服务地址」。连接本地服务时，在 Debug 版登录页的「服务地址」填写 `http://<Mac 的局域网 IPv4>:8000`，再登录或注册（「使用正式服务器」可切回）。首次访问时按 iPhone 的提示允许本地网络访问。连接失败时登录页会显示错误。模拟器可直接使用 `http://127.0.0.1:8000`。

### 记录与分享

在「记录」写想法，或粘贴一个链接（整段只有一个 http/https 地址时按链接保存），也可以点「选择截图」。备注和归属（收集箱、最近项目或其他项目）可选。保存时先写入本机，收到 API 确认后才显示「已保存到服务器，正在整理」。截图在上传前用 Vision 在本机识别文字。

在 Safari、相册等 App 的分享菜单选 Gatherloom，可保存链接、文字或一张图片。Safari 分享会附带页面标题和正文快照。扩展先存到本机，再短暂尝试上传；失败的记录在打开 Gatherloom 后继续上传。扩展只保存到 Gatherloom 当前登录的账号，未登录时提示先打开 App 登录。

断网后可在「收集箱」或「项目 → 全部资料」找到未同步的内容。恢复网络后保持 App 在前台，或重开 App 继续上传。退出账号会保留该账号的待上传记录，也可以在「账号设置」中删除本机副本。资料详情支持编辑想法和备注（生成新版本）、加入或移出项目，以及删除。

## 服务端开发

使用 Docker Desktop、Python 3.12 和 uv，从仓库根目录启动本地数据库和 API。

```sh
docker compose -f deploy/compose.yaml up -d --wait postgres
cd server
uv sync --locked
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app
```

模型服务的 API key 和模型选择放在本机的 `server/.env`（已被 Git 忽略）：复制 `server/.env.example` 后填写，启动时改用 `uv run --locked --env-file .env uvicorn app.main:app`。不要把真实 key 提交到仓库。

在另一个终端运行 `curl -fsS http://127.0.0.1:8000/healthz`。`GET /healthz` 返回 HTTP 200 和 `{"status":"ok"}`，用于确认 API 进程可达。

截图等附件保存在 `GATHERLOOM_STORAGE_DIR`（默认 `server/var/storage`），单个文件最大 10 MiB。定期运行 `uv run --locked python -m app.admin cleanup-attachments`，删除 24 小时内未关联到记录的上传。

在 `server/` 目录运行 `uv run --locked python -m app.admin invite` 生成一个有效期 24 小时的单次邀请码。真机注册使用局域网启动方式，在登录页填入 Mac 的服务地址并选择「使用邀请码注册」。每个账号各用一个邀请码。账号恢复使用 `uv run --locked python -m app.admin reset-password <用户名>`，命令会提示输入密码并撤销该账号的所有会话。

API 契约来自 FastAPI，可在本地 `/docs` 查看，或在 `server/` 运行 `uv run --locked python -m app.export_openapi` 导出 JSON。访问令牌有效期最长 15 分钟，刷新时轮换两个令牌，单个会话最长 30 天。登录和注册分别按客户端地址限流，每分钟最多 10 次。项目和问题列表用 `next_cursor` 翻页。

### 后台整理（Worker）

资料保存后由 Worker 整理：分块（每块索引时拼上标题与备注）→ jieba 关键词索引 → 百炼 Embedding（完成后「已可检索」）→ 千问生成知识卡片（标题、摘要、要点、关键词）→ 卡片入索引（「整理完成」）。在另一个终端启动：

```sh
cd server
uv run --locked --env-file .env python -m app.worker
```

Worker 需要 `server/.env` 中的百炼与 DeepSeek key，缺少时拒绝启动。`uv run --locked --env-file .env python -m app.admin check-models` 会各调用一次 Embedding、light 与 heavy 模型，打印模型、用量与费用估算，用于检查配置。默认两个线程并发，同一账号同时只处理一条资料。任务按租约认领，Worker 崩溃或被杀后，租约过期（60 秒）即由其他 Worker 从检查点继续。可重试的错误（网络、限流、服务端错误）最多尝试 3 次。认证失败、余额不足、模型不可用或输出不合格不会自动重试。

资料详情显示处理状态：

| 状态 | 含义 |
|---|---|
| 已保存到服务器，正在整理 | 等待或正在建立索引 |
| 已可检索 | 原文索引完成，知识卡片仍在生成 |
| 整理完成 | 索引与知识卡片都已完成 |
| 已保存，部分整理失败 | 原文可检索，知识卡片失败，可重试 |
| 已保存，整理失败 | 索引失败，尚不可检索，可重试 |

`GET /v1/entries/{id}/processing` 返回状态与失败步骤，`POST /v1/entries/{id}/retry` 只重做未完成的步骤。每次模型调用记录在 `runs` 表（模型、用量、带价格日期的费用估算）。知识卡片只用于展示和召回，不作为问答引用的证据。

### 搜索与问答

`GET /v1/search` 按关键词（jieba）和语义（百炼 Embedding）检索当前账号的资料，可按项目、类型、时间筛选。API 进程需要 `server/.env` 里的百炼 key 来向量化查询，所以要用 `--env-file .env` 启动；没有 key 时搜索降级为只按关键词，返回 `degraded: true`。

项目问答：`POST /v1/conversations/{id}/messages` 返回 202 和 `run_id`，Worker 只用本项目的资料回答（heavy 模型），回答里的 `[n]` 指向原文片段，客户端轮询 `GET /v1/runs/{id}`。资料不足时直接说明，不联网补充。知识卡片只作导航，不能被引用。

### 项目认识：总结、关联建议、决定与回顾

资料可检索、加入或移出项目、删除，以及待解问题和正式决定发生变化时，服务端记录一条项目事件，并安排一次维护（2 分钟内的变化合并成一次）。维护由 Worker 执行：更新项目的 AI 总结（每次一个新版本，可回看历史），为新资料提出「相关资料」「与待解问题相关」的建议，并在资料里出现明确的选项与倾向时提出决定草稿（每项目每天最多 3 个）。草稿只有在你确认后才成为正式决定；正式决定的修订保留历史。AI 生成的内容不会再触发维护。

周回顾需要单独启动 scheduler，它每 5 分钟检查一次，在每个账号时区的周一 09:00 之后，为上一周有变化的项目生成回顾：

```sh
cd server
uv run --locked --env-file .env python -m app.scheduler
```

项目页的「生成阶段回顾」可以随时生成从上次回顾结束到现在的回顾。

删除的资料由 scheduler 在几分钟后彻底清理（附件、卡片、引用片段、问答依据与检查点），需要立即清理时运行 `python -m app.admin purge-deleted`。

评测集与命令见 [docs/evaluation](docs/evaluation/README.md)：`python -m evaluation.load`、`evaluation.retrieval`、`evaluation.answers`、`evaluation.understanding`。

### 检查与测试

从仓库根目录创建独立测试数据库，然后运行与 CI 相同的检查。测试数据库只需创建一次。

```sh
docker compose -f deploy/compose.yaml exec -T postgres createdb -U gatherloom gatherloom_test
cd server
uv run --locked ruff check .
uv run --locked mypy app tests evaluation
GATHERLOOM_TEST_DATABASE_URL=postgresql+psycopg://gatherloom:gatherloom-local@127.0.0.1:54329/gatherloom_test \
  uv run --locked python -m pytest
```

## 部署
生产环境使用 `deploy/compose.prod.yaml`：Caddy 自动 HTTPS 是唯一公网入口，PostgreSQL 在无外网的内部网络。部署、邀请、额度、备份与恢复见 [使用说明与运维手册](docs/operations.md)。

## 文档
- [使用说明与运维手册](docs/operations.md)
- [产品与技术方案](docs/product-plan.md)
- [知识卡片与检索增强方案](docs/knowledge-cards-and-retrieval.md)
- [分阶段开发路线](docs/roadmap.md)
- [架构决策记录](docs/decisions/README.md)
- [评测集](docs/evaluation/README.md)

## 技术路线
SwiftUI（iOS 17+）· FastAPI · LangGraph · PostgreSQL + pgvector · 百炼 Embedding · DeepSeek / 千问
