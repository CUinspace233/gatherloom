import Foundation
import Vision

/// On-device OCR for screenshots (ADR 0003). Failures never block saving.
enum TextRecognizer {
    static func recognize(imageAt url: URL) async throws -> CaptureDraft.OCR {
        try await Task.detached(priority: .utility) {
            let data = try Data(contentsOf: url)
            guard let image = ImagePayload.downsampled(data, maxPixels: 2048)?.cgImage else {
                throw CocoaError(.fileReadCorruptFile)
            }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = ["zh-Hans", "en-US"]
            request.usesLanguageCorrection = true
            try VNImageRequestHandler(cgImage: image).perform([request])
            let lines = (request.results ?? []).compactMap { observation -> CaptureDraft.OCRLine? in
                guard let text = observation.topCandidates(1).first?.string else { return nil }
                let box = observation.boundingBox
                // Vision uses a bottom-left origin; the API expects top-left.
                let values = [box.minX, 1 - box.maxY, box.width, box.height].map { min(max(Double($0), 0), 1) }
                return CaptureDraft.OCRLine(text: String(text.prefix(2000)), box: values)
            }
            let limited = Array(lines.prefix(2000))
            let text = String(limited.map(\.text).joined(separator: "\n").prefix(100_000))
            return CaptureDraft.OCR(text: text, lines: limited, engine: "apple-vision")
        }.value
    }
}
