# 0003 截图 OCR 以 iOS 端侧 Vision 为主

- 日期：2026-11-09
- 状态：已采纳

## 背景
原方案在服务器使用 CPU Tesseract。Tesseract 对中文截图（小字号、UI 混排）效果一般，并占用服务器 CPU。

## 决策
- 主 App 与分享扩展在保存图片时，用 Vision `VNRecognizeTextRequest`（`recognitionLevel = .accurate`，`recognitionLanguages = ["zh-Hans", "en-US"]`，开启语言校正）识别文字。
- 识别结果按行保存文本与归一化坐标，作为 `client_ocr` 加工版本随图片上传；识别失败不阻塞保存。
- 服务端：若提交未携带 OCR 文字，由 Tesseract 适配器兜底；用户修正 OCR 生成新加工版本。

## 后果
- 图片引用可定位到 OCR 行与图片区域。
- 分享扩展内存受限：大图先降采样再识别，原图保持不变上传；识别需在扩展超时前完成，否则交由主 App 前台补做。
- 服务端镜像仍保留 Tesseract（chi_sim、eng），但并发与资源占用大幅降低。

## 待验证（S0）
- [ ] 10 张真实中文截图的识别质量（与 Tesseract 对比）。
- [ ] 分享扩展中识别耗时与内存峰值。
