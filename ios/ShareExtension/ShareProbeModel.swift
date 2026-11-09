import Foundation
import UIKit
import UniformTypeIdentifiers

@MainActor
final class ShareProbeModel: ObservableObject {
    enum Phase {
        case running
        case finished
    }

    @Published var phase: Phase = .running
    @Published var record: ShareCaptureRecord?
    @Published var saveMessage = ""

    var onDone: () -> Void = {}

    func process(items: [NSExtensionItem]) async {
        let started = Date()
        let sampler = PeakMemorySampler()
        let before = MemoryProbe.footprintMB()
        let available = MemoryProbe.availableMB()
        sampler.start()

        let providers = items.flatMap { $0.attachments ?? [] }
        var record = ShareCaptureRecord(
            id: UUID().uuidString,
            createdAt: started,
            kind: "unknown",
            attachmentTypes: providers.flatMap(\.registeredTypeIdentifiers),
            keychain: "",
            elapsedMs: 0,
            errors: []
        )

        // 1. Safari JS 预处理结果（ADR 0004）
        if let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.propertyList.identifier) }) {
            do {
                let item = try await provider.loadItemAsync(typeIdentifier: UTType.propertyList.identifier)
                if let dictionary = item as? NSDictionary,
                   let results = dictionary[NSExtensionJavaScriptPreprocessingResultsKey] as? [String: Any] {
                    let preprocess = PreprocessResult(dictionary: results)
                    record.kind = "safari-page"
                    record.preprocess = preprocess
                    record.url = preprocess.url
                    record.title = preprocess.articleTitle ?? preprocess.title
                } else {
                    record.errors.append("属性列表中没有 JS 预处理结果")
                }
            } catch {
                record.errors.append("读取 JS 预处理结果失败：\(error.localizedDescription)")
            }
        }

        // 2. 图片：扩展内执行 OCR（ADR 0003）
        if record.kind == "unknown",
           let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.image.identifier) }) {
            record.kind = "image"
            do {
                let data = try await provider.loadImageData()
                record.ocr = try await TextRecognizer.recognize(imageData: data, source: "share-extension").result
            } catch {
                record.errors.append("图片 OCR 失败：\(error.localizedDescription)")
            }
        }

        // 3. 仅 URL（非 Safari 的 App 分享链接）
        if record.kind == "unknown",
           let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.url.identifier) }) {
            record.kind = "url"
            do {
                let item = try await provider.loadItemAsync(typeIdentifier: UTType.url.identifier)
                record.url = (item as? URL)?.absoluteString ?? (item as? NSURL)?.absoluteString
            } catch {
                record.errors.append("读取 URL 失败：\(error.localizedDescription)")
            }
        }

        // 4. 纯文字
        if record.kind == "unknown",
           let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) }) {
            record.kind = "text"
            do {
                let item = try await provider.loadItemAsync(typeIdentifier: UTType.plainText.identifier)
                record.text = item as? String
            } catch {
                record.errors.append("读取文字失败：\(error.localizedDescription)")
            }
        }

        // Keychain 共享：读取主 App 写入的探针值（ADR 0008）
        record.keychain = KeychainStore.probeReadDescription()

        record.memory = MemorySnapshot(
            beforeMB: before,
            afterMB: MemoryProbe.footprintMB(),
            peakMB: sampler.stop(),
            availableAtStartMB: available
        )
        record.elapsedMs = Date().timeIntervalSince(started) * 1000

        // 先保存再展示：即使用户直接关闭，测量结果也已留存。
        do {
            let url = try AppGroupStore().saveShareRecord(record)
            saveMessage = "已写入 App Group：\(url.lastPathComponent)"
        } catch {
            saveMessage = "未写入 App Group：\(error.localizedDescription)。请截图本页记录结果。"
        }

        self.record = record
        phase = .finished
    }
}

extension NSItemProvider {
    func loadItemAsync(typeIdentifier: String) async throws -> NSSecureCoding? {
        try await withCheckedThrowingContinuation { continuation in
            loadItem(forTypeIdentifier: typeIdentifier, options: nil) { item, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: item)
                }
            }
        }
    }

    func loadDataAsync(typeIdentifier: String) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            _ = loadDataRepresentation(forTypeIdentifier: typeIdentifier) { data, error in
                if let data {
                    continuation.resume(returning: data)
                } else {
                    continuation.resume(throwing: error ?? OCRError.invalidImage)
                }
            }
        }
    }

    /// 截图编辑器、相册、文件等来源给出的图片形式不同：Data、文件 URL 或 UIImage。
    func loadImageData() async throws -> Data {
        if let data = try? await loadDataAsync(typeIdentifier: UTType.image.identifier) {
            return data
        }
        let item = try await loadItemAsync(typeIdentifier: UTType.image.identifier)
        if let url = item as? URL {
            return try Data(contentsOf: url)
        }
        if let image = item as? UIImage, let png = image.pngData() {
            return png
        }
        if let data = item as? Data {
            return data
        }
        throw OCRError.invalidImage
    }
}
