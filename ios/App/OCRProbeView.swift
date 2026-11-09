import PhotosUI
import SwiftUI

struct OCRProbeView: View {
    @State private var selection: [PhotosPickerItem] = []
    @State private var results: [OCRResult] = []
    @State private var status = ""
    @State private var isRunning = false

    var body: some View {
        List {
            Section {
                PhotosPicker(selection: $selection, maxSelectionCount: 20, matching: .images) {
                    Label("选择截图（最多 20 张）", systemImage: "photo.on.rectangle")
                }
                .disabled(isRunning)
                if isRunning {
                    ProgressView(status)
                } else if !status.isEmpty {
                    Text(status).font(.footnote)
                }
            } footer: {
                Text("识别语言：简体中文 + 英文，精确模式。结果与实际送入 Vision 的图片保存在 Documents/S0/ocr，用于与 Tesseract 对比（scripts/s0/ocr_compare.py）。")
            }

            Section("结果（\(results.count)）") {
                ForEach(results) { result in
                    NavigationLink {
                        OCRDetailView(result: result)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(result.fullText.isEmpty ? "（未识别到文字）" : result.fullText)
                                .lineLimit(2)
                            Text("\(result.lines.count) 行 · \(Int(result.elapsedMs)) ms · \(result.originalPixelWidth)×\(result.originalPixelHeight)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("截图 OCR")
        .onAppear { results = ProbeStorage.loadOCRResults() }
        .onChange(of: selection) { _, items in
            guard !items.isEmpty else { return }
            Task { await run(items) }
        }
    }

    private func run(_ items: [PhotosPickerItem]) async {
        isRunning = true
        var failures = 0
        for (index, item) in items.enumerated() {
            status = "识别中 \(index + 1)/\(items.count)"
            do {
                guard let data = try await item.loadTransferable(type: Data.self) else {
                    failures += 1
                    continue
                }
                let output = try await TextRecognizer.recognize(imageData: data, source: "app", keepProcessedPNG: true)
                try ProbeStorage.saveOCR(output.result, processedPNG: output.processedPNG)
                results.insert(output.result, at: 0)
            } catch {
                failures += 1
                status = "第 \(index + 1) 张失败：\(error.localizedDescription)"
            }
        }
        isRunning = false
        selection = []
        status = "完成 \(items.count - failures) 张" + (failures > 0 ? "，失败 \(failures) 张" : "")
    }
}

struct OCRDetailView: View {
    let result: OCRResult

    var body: some View {
        List {
            if let image = UIImage(contentsOfFile: ProbeStorage.imageURL(for: result).path) {
                Section {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(maxHeight: 360)
                }
            }
            Section("统计") {
                LabeledContent("行数", value: "\(result.lines.count)")
                LabeledContent("耗时", value: "\(Int(result.elapsedMs)) ms")
                LabeledContent("尺寸", value: "\(result.originalPixelWidth)×\(result.originalPixelHeight)")
                LabeledContent("Vision revision", value: "\(result.revision)")
                LabeledContent("支持 zh-Hans", value: result.supportedLanguages.contains("zh-Hans") ? "是" : "否")
                if let m = result.memory {
                    LabeledContent("内存峰值", value: String(format: "%.1f MB", m.peakMB))
                }
            }
            Section("全文") {
                Text(result.fullText).textSelection(.enabled)
            }
            Section("逐行置信度") {
                ForEach(Array(result.lines.enumerated()), id: \.offset) { _, line in
                    HStack(alignment: .top) {
                        Text(line.text).font(.footnote)
                        Spacer()
                        Text(String(format: "%.2f", line.confidence))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(line.confidence < 0.5 ? .red : .secondary)
                    }
                }
            }
        }
        .navigationTitle("OCR 详情")
    }
}
