# Gatherloom iOS

S0 阶段这里是一组**验证探针**，用于回答 ADR 0003（端侧 OCR）、0004（Safari 正文预处理）、0008（免费签名）里的待验证问题。S1 起替换为正式界面。

## 目录

| 路径 | 内容 |
|---|---|
| `project.yml` | XcodeGen 工程描述（生成的 `.xcodeproj` 不入库） |
| `Config/` | 构建配置；个人签名信息放 `Local.xcconfig` |
| `App/` | 主 App：探针页面 |
| `ShareExtension/` | 分享扩展；`Resources/GatherloomPreprocess.js` 为生成文件 |
| `SharedCapture/` | 主 App 与扩展共用：App Group、Keychain、Vision OCR、内存测量 |
| `Preprocess/` | Safari 预处理脚本源码、Readability 0.6.0、Chromium 测试 |

## 你需要做的

### 1. 准备（约 30 分钟）

1. Mac 上安装 Xcode（App Store），打开一次并同意许可。
2. 安装 XcodeGen：

   ```bash
   brew install xcodegen
   ```

3. Xcode → Settings → Accounts 登录 Apple ID，出现 “(Personal Team)”。
4. iPhone：设置 → 隐私与安全性 → 开发者模式 → 打开（需重启）。用数据线连接 Mac 并信任。
5. 复制并填写本地配置：

   ```bash
   cp ios/Config/Local.xcconfig.example ios/Config/Local.xcconfig
   ```

   - `BUNDLE_ID_PREFIX`：任意全局唯一的反向域名，例如 `io.github.yourname`。
   - `DEVELOPMENT_TEAM`：Personal Team 的 Team ID。在 Xcode 的 Accounts 里看不到时，可先留空，在第 2 步工程的 Signing 页面选 Team 后，从 Build Settings 搜 `DEVELOPMENT_TEAM` 复制回来。

### 2. 生成工程并装到手机

```bash
cd ios && xcodegen generate && open Gatherloom.xcodeproj
```

顶部选择 scheme `Gatherloom` 和你的 iPhone，⌘R 运行。首次运行需要在 iPhone 设置 → 通用 → VPN 与设备管理 中信任开发者证书。

**签名报错时（这本身就是 S0 要记录的结论）**：

| 报错内容大意 | 处理 |
|---|---|
| Personal development teams do not support the App Groups capability | `Local.xcconfig` 设 `ENTITLEMENTS_VARIANT = -KeychainOnly`，重新 `xcodegen generate` |
| 同时不支持 Keychain Sharing | 设 `ENTITLEMENTS_VARIANT = -None` |
| Bundle identifier 不可用 | 换一个 `BUNDLE_ID_PREFIX` |
| 超过 App ID 数量限制 | 免费团队每 7 天最多注册 10 个 App ID；等待或复用已注册的前缀 |

无线调试：连线成功一次后，Xcode → Window → Devices and Simulators 勾选 “Connect via network”。

### 3. 逐项验证（结果填入 `docs/evaluation/s0-ios-results.md`）

| # | 验证 | 操作 | 通过标准 |
|---|---|---|---|
| A | App Group | App → App Group 共享目录 → 写入并读回 | 显示 ✓ |
| B | Keychain 共享 | App → Keychain → 写入；再从任意 App 分享到 Gatherloom | 扩展页 “Keychain 共享” 显示读取成功 |
| C | 跨进程共享 | Safari 分享任意网页 → 完成；App → 分享扩展记录 | 能看到该记录 |
| D | 截图 OCR | App → 截图 OCR，选 10 张真实中文截图 | 记录每张字数、耗时、明显错字 |
| E | 扩展内 OCR | 在相册或截图编辑器里分享一张截图到 Gatherloom | 记录耗时、内存峰值，扩展未被系统杀掉 |
| F | 正文提取 | Safari 打开公众号、知乎、普通博客各 3 篇 → 分享 | 记录提取方式、字数、正文是否完整、是否混入导航/广告 |
| G | HTTPS | App → HTTPS，输入服务器健康检查地址，Wi‑Fi 与蜂窝各一次 | HTTP 200 |
| H | 7 天重签 | 记录首次安装日期，7 天后是否需要重新运行 | 记录日期 |

公众号注意：在微信内置浏览器里无法使用 Safari 分享扩展。请复制链接到 Safari 打开后再分享，并记录这一步是否可以接受。

### 4. 把结果交回

1. App 首页 → 生成导出文件 → 通过 AirDrop 或“存储到文件”发到 Mac。
2. 用 Finder 选中 iPhone → 文件 → Gatherloom，把 `S0/ocr` 文件夹拖到 Mac。
3. 可选：为 3–5 张截图手工写校对文本 `<同名>.gt.txt`，然后运行对比：

   ```bash
   brew install tesseract tesseract-lang
   ```

   ```bash
   python3 scripts/s0/ocr_compare.py ~/Desktop/ocr > docs/evaluation/s0-ocr-compare.md
   ```

4. 把导出 JSON 和填好的结果表发给我（或提交到仓库，注意截图与正文中的隐私内容）。我据此更新 ADR 0003 / 0004 / 0008。

## 开发者备注

- 修改 `Preprocess/src/preprocess.js` 后运行 `scripts/s0/build_preprocess_js.sh` 重新生成，并运行 `node ios/Preprocess/test/run.js`（需要 Playwright）。
- CI（`.github/workflows/ios-s0.yml`）在 macOS 上做不签名的模拟器编译，并在 Chromium 中测试预处理脚本。CI 不能验证签名能力、扩展内存或 Safari 实际行为，这些只能真机验证。
- Swift 采用 Swift 5 语言模式，部署目标 iOS 17。
