import CoreGraphics
import Foundation
import ImageIO
import UIKit
import Vision

/// 一行识别结果。box 为归一化坐标（左上角为原点）：[x, y, width, height]。
struct RecognizedLine: Codable {
    var text: String
    var confidence: Float
    var box: [Double]
}

struct OCRResult: Codable, Identifiable {
    var id: String
    var createdAt: Date
    var source: String
    var languages: [String]
    var supportedLanguages: [String]
    var revision: Int
    var originalPixelWidth: Int
    var originalPixelHeight: Int
    var processedPixelWidth: Int
    var processedPixelHeight: Int
    var downsampled: Bool
    var elapsedMs: Double
    var lines: [RecognizedLine]
    var fullText: String
    var memory: MemorySnapshot?
}

enum OCRError: LocalizedError {
    case invalidImage
    case thumbnailFailed

    var errorDescription: String? {
        switch self {
        case .invalidImage: return "无法读取图片数据"
        case .thumbnailFailed: return "图片解码失败"
        }
    }
}

/// 端侧 Vision 文字识别（ADR 0003）。识别失败不阻塞保存，由调用方决定如何记录。
enum TextRecognizer {
    static let languages = ["zh-Hans", "en-US"]

    /// - Parameters:
    ///   - maxPixel: 长边超过该值时先降采样，避免扩展内存超限。
    ///   - keepProcessedPNG: 是否返回实际送入 Vision 的图片（PNG），用于与 Tesseract 对比。
    static func recognize(
        imageData: Data,
        source: String,
        maxPixel: Int = 4096,
        keepProcessedPNG: Bool = false
    ) async throws -> (result: OCRResult, processedPNG: Data?) {
        try await Task.detached(priority: .userInitiated) {
            try recognizeSync(imageData: imageData, source: source, maxPixel: maxPixel, keepProcessedPNG: keepProcessedPNG)
        }.value
    }

    static func recognizeSync(
        imageData: Data,
        source: String,
        maxPixel: Int,
        keepProcessedPNG: Bool
    ) throws -> (result: OCRResult, processedPNG: Data?) {
        let started = Date()
        let sampler = PeakMemorySampler()
        let before = MemoryProbe.footprintMB()
        let available = MemoryProbe.availableMB()
        sampler.start()

        guard let imageSource = CGImageSourceCreateWithData(imageData as CFData, nil) else {
            _ = sampler.stop()
            throw OCRError.invalidImage
        }
        let properties = CGImageSourceCopyPropertiesAtIndex(imageSource, 0, nil) as? [CFString: Any]
        let width = properties?[kCGImagePropertyPixelWidth] as? Int ?? 0
        let height = properties?[kCGImagePropertyPixelHeight] as? Int ?? 0
        let longest = max(width, height)
        let downsampled = longest > maxPixel

        // 缩略图接口同时处理 EXIF 方向，并只解码需要的尺寸。
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: downsampled ? maxPixel : max(longest, 1),
        ]
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(imageSource, 0, options as CFDictionary) else {
            _ = sampler.stop()
            throw OCRError.thumbnailFailed
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.recognitionLanguages = languages
        let supported = (try? request.supportedRecognitionLanguages()) ?? []

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        do {
            try handler.perform([request])
        } catch {
            _ = sampler.stop()
            throw error
        }

        let lines: [RecognizedLine] = (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let b = observation.boundingBox
            return RecognizedLine(
                text: candidate.string,
                confidence: candidate.confidence,
                box: [b.origin.x, 1 - b.origin.y - b.height, b.width, b.height].map { Double($0) }
            )
        }

        let png = keepProcessedPNG ? UIImage(cgImage: cgImage).pngData() : nil
        let peak = sampler.stop()

        let result = OCRResult(
            id: UUID().uuidString,
            createdAt: started,
            source: source,
            languages: languages,
            supportedLanguages: supported,
            revision: request.revision,
            originalPixelWidth: width,
            originalPixelHeight: height,
            processedPixelWidth: cgImage.width,
            processedPixelHeight: cgImage.height,
            downsampled: downsampled,
            elapsedMs: Date().timeIntervalSince(started) * 1000,
            lines: lines,
            fullText: lines.map(\.text).joined(separator: "\n"),
            memory: MemorySnapshot(
                beforeMB: before,
                afterMB: MemoryProbe.footprintMB(),
                peakMB: peak,
                availableAtStartMB: available
            )
        )
        return (result, png)
    }
}
