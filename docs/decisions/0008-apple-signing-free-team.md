# 0008 暂用免费签名，内测前购买开发者账号

- 日期：2026-11-09
- 状态：已采纳

## 背景
当前没有付费 Apple 开发者账号。

## 决策
- S0–S5 使用免费个人团队签名在本人真机调试，接受约 7 天需要重新安装。
- S0 首项验证：免费团队下主 App 与 Share Extension 能否配置 App Group 与 Keychain 共享访问组。若不可用，分享扩展可靠队列推迟到购买账号后验证，先用主 App 内记录完成 S2 其他部分。
- S6 邀请他人内测前购买开发者账号，使用 TestFlight 分发。

## 后果
- 免费签名期间无法向他人分发；多账号隔离用本人设备上的两个测试账号验证。
- 后台 URLSession 与扩展行为必须在真机验证，模拟器结果不作为出口依据。

## 待验证（S0）
- [x] 当前免费团队的 App Group / Keychain 共享已通过真机读写验证。
- [x] macOS 27.2、Xcode 27.2、iPhone 16 Pro Max（iOS 27.2）。构建产物最低版本为 iOS 17.0，已在上述设备启动。

## S0 实测

- 独立的 Gatherloom Probe 主 App 和分享扩展使用当前 Personal Team 签名。两个产物均带相同的 App Group 和 Keychain 访问组。
- 探针主 App 在 iPhone 上向共享容器和 Keychain 写入同一个测试码。用户从 Safari 分享菜单打开扩展，两处均读到相同的值。
