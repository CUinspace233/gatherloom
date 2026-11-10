# Gatherloom（知织）

> Gather ideas. Weave understanding.

围绕项目，收集想法、连接资料、沉淀有据可查的认识。

A project-driven personal knowledge workspace with iOS capture, source-grounded answers, and continuous reflection.

## 状态
Gatherloom 支持邀请码注册、项目与问题管理，以及离线保存的想法、链接和截图（含系统分享扩展与本机文字识别）。最低支持 iOS 17。

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

在登录页的「服务地址」填写 `http://<Mac 的局域网 IPv4>:8000`，再登录或注册。首次访问时按 iPhone 的提示允许本地网络访问。连接失败时登录页会显示错误。模拟器可直接使用 `http://127.0.0.1:8000`。

### 记录与分享

在「记录」写想法，或粘贴一个链接（整段只有一个 http/https 地址时按链接保存），也可以点「选择截图」。备注和归属（收集箱、最近项目或其他项目）可选。保存时先写入本机，收到 API 确认后才显示「已保存到服务器，正在整理」。截图在上传前用 Vision 在本机识别文字。

在 Safari、相册等 App 的分享菜单选 Gatherloom，可保存链接、文字或一张图片。Safari 分享会附带页面标题和正文快照。扩展先存到本机，再短暂尝试上传；失败的记录在打开 Gatherloom 后继续上传。扩展只保存到 Gatherloom 当前登录的账号，未登录时提示先打开 App 登录。

断网后可在「收集箱」或「项目 → 查看项目记录」找到未同步的内容。恢复网络后保持 App 在前台，或重开 App 继续上传。退出账号会保留该账号的待上传记录，也可以在「账号设置」中删除本机副本。资料详情支持编辑想法和备注（生成新版本）、加入或移出项目，以及删除。

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

从仓库根目录创建独立测试数据库，然后运行与 CI 相同的检查。测试数据库只需创建一次。

```sh
docker compose -f deploy/compose.yaml exec -T postgres createdb -U gatherloom gatherloom_test
cd server
uv run --locked ruff check .
uv run --locked mypy app tests
GATHERLOOM_TEST_DATABASE_URL=postgresql+psycopg://gatherloom:gatherloom-local@127.0.0.1:54329/gatherloom_test \
  uv run --locked python -m pytest
```

## 文档
- [产品与技术方案](docs/product-plan.md)
- [知识卡片与检索增强方案](docs/knowledge-cards-and-retrieval.md)
- [分阶段开发路线](docs/roadmap.md)
- [架构决策记录](docs/decisions/README.md)
- [评测集](docs/evaluation/README.md)

## 技术路线
SwiftUI（iOS 17+）· FastAPI · LangGraph · PostgreSQL + pgvector · 百炼 Embedding · DeepSeek / 千问
