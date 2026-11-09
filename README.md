# Gatherloom（知织）

> Gather ideas. Weave understanding.

围绕项目，收集想法、连接资料、沉淀有据可查的认识。

A project-driven personal knowledge workspace with iOS capture, source-grounded answers, and continuous reflection.

## 状态
Gatherloom 支持邀请码注册、项目与问题管理，以及离线文字记录。最低支持 iOS 17。

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

在 `Gatherloom` 的 Debug 构建中打开「记录 → 测试连接」，填写 `http://<Mac 的局域网 IPv4>:8000` 后点「测试连接」。首次访问时按 iPhone 的提示允许本地网络访问。成功时显示 `HTTP 200 · status=ok`。

### 分享扩展探针

在 Xcode 用 `GatherloomSharingProbe` scheme 安装 Gatherloom Probe。打开后点「写入新测试码」，再在 Safari 的分享菜单里选 Gatherloom Probe。核对扩展的 App Group 和 Keychain 两行是否都读到主 App 写入的测试码。

### 离线文字记录

登录后在「记录」写下想法，可加备注并选择收集箱或项目。保存时先写入本机，收到 API 确认后才显示「已保存到服务器」。断网后可在「收集箱」或「项目 → 查看项目记录」找到未同步的内容。恢复网络后保持 App 在前台，或重开 App 继续上传。退出账号会保留该账号的待上传记录。

## 服务端开发

使用 Docker Desktop、Python 3.12 和 uv，从仓库根目录启动本地数据库和 API。

```sh
docker compose -f deploy/compose.yaml up -d --wait postgres
cd server
uv sync --locked
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app
```

在另一个终端运行 `curl -fsS http://127.0.0.1:8000/healthz`。`GET /healthz` 返回 HTTP 200 和 `{"status":"ok"}`，用于确认 API 进程可达。

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
