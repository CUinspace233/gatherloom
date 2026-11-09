import SwiftUI

/// 主 App 与扩展共用的记录展示。
struct ShareRecordSummary: View {
    let record: ShareCaptureRecord
    var saveMessage: String = ""

    var body: some View {
        List {
            if !saveMessage.isEmpty {
                Section { Text(saveMessage).font(.footnote) }
            }
            Section("概要") {
                row("类型", record.kind)
                row("总耗时", String(format: "%.0f ms", record.elapsedMs))
                if let url = record.url { row("URL", url) }
                if let title = record.title { row("标题", title) }
                row("附件类型", record.attachmentTypes.joined(separator: ", "))
            }
            if let p = record.preprocess {
                Section("Safari 预处理") {
                    row("提取方式", p.extractor ?? "无")
                    row("正文长度", "\(p.textLength ?? 0)" + ((p.truncated ?? false) ? "（已截断，原 \(p.originalTextLength ?? 0)）" : ""))
                    row("JS 耗时", String(format: "%.0f ms", p.elapsedMs ?? 0))
                    row("HTML 长度", "\(p.htmlLength ?? 0)")
                    if let byline = p.byline { row("作者", byline) }
                    if let error = p.error { row("错误", error) }
                    Text((p.textContent ?? "").prefix(3000))
                        .font(.footnote)
                        .textSelection(.enabled)
                }
            }
            if let ocr = record.ocr {
                Section("OCR") {
                    row("行数", "\(ocr.lines.count)")
                    row("耗时", String(format: "%.0f ms", ocr.elapsedMs))
                    row("尺寸", "\(ocr.originalPixelWidth)×\(ocr.originalPixelHeight)" + (ocr.downsampled ? " → \(ocr.processedPixelWidth)×\(ocr.processedPixelHeight)" : ""))
                    Text(ocr.fullText.prefix(3000))
                        .font(.footnote)
                        .textSelection(.enabled)
                }
            }
            if let text = record.text {
                Section("文字") { Text(text).textSelection(.enabled) }
            }
            Section("Keychain 共享") { Text(record.keychain).font(.footnote) }
            if let m = record.memory {
                Section("内存（MB）") {
                    row("开始", String(format: "%.1f", m.beforeMB))
                    row("峰值", String(format: "%.1f", m.peakMB))
                    row("结束", String(format: "%.1f", m.afterMB))
                    row("开始时剩余可用", String(format: "%.1f", m.availableAtStartMB))
                }
            }
            if !record.errors.isEmpty {
                Section("错误") {
                    ForEach(record.errors, id: \.self) { Text($0).foregroundStyle(.red) }
                }
            }
        }
    }

    private func row(_ title: String, _ value: String) -> some View {
        LabeledContent(title) {
            Text(value).multilineTextAlignment(.trailing).textSelection(.enabled)
        }
    }
}
