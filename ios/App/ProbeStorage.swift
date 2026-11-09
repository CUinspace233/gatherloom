import Foundation

/// 主 App 的 Documents/S0 目录（Info.plist 开启了文件共享，可在“文件”App 或 Finder 中取出）。
enum ProbeStorage {
    static var root: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("S0", isDirectory: true)
    }

    static var ocrDirectory: URL { root.appendingPathComponent("ocr", isDirectory: true) }

    static func saveOCR(_ result: OCRResult, processedPNG: Data?) throws {
        try FileManager.default.createDirectory(at: ocrDirectory, withIntermediateDirectories: true)
        let base = "\(result.createdAt.fileStamp)-\(result.id)"
        try JSONEncoder.probe.encode(result)
            .write(to: ocrDirectory.appendingPathComponent("\(base).json"), options: .atomic)
        if let processedPNG {
            try processedPNG.write(to: ocrDirectory.appendingPathComponent("\(base).png"), options: .atomic)
        }
    }

    static func loadOCRResults() -> [OCRResult] {
        let urls = (try? FileManager.default.contentsOfDirectory(at: ocrDirectory, includingPropertiesForKeys: nil)) ?? []
        return urls
            .filter { $0.pathExtension == "json" }
            .sorted { $0.lastPathComponent > $1.lastPathComponent }
            .compactMap { url in
                guard let data = try? Data(contentsOf: url) else { return nil }
                return try? JSONDecoder.probe.decode(OCRResult.self, from: data)
            }
    }

    static func imageURL(for result: OCRResult) -> URL {
        ocrDirectory.appendingPathComponent("\(result.createdAt.fileStamp)-\(result.id).png")
    }
}

struct ProbeExport: Codable {
    var device: DeviceInfo
    var ocr: [OCRResult]
    var shareRecords: [ShareCaptureRecord]
    var shareRecordsError: String?
}

enum ProbeExporter {
    static func export(device: DeviceInfo) throws -> URL {
        var shareRecords: [ShareCaptureRecord] = []
        var shareError: String?
        do {
            shareRecords = try AppGroupStore().loadShareRecords()
        } catch {
            shareError = error.localizedDescription
        }
        let export = ProbeExport(
            device: device,
            ocr: ProbeStorage.loadOCRResults(),
            shareRecords: shareRecords,
            shareRecordsError: shareError
        )
        try FileManager.default.createDirectory(at: ProbeStorage.root, withIntermediateDirectories: true)
        let url = ProbeStorage.root.appendingPathComponent("s0-ios-export-\(Date().fileStamp).json")
        try JSONEncoder.probe.encode(export).write(to: url, options: .atomic)
        return url
    }
}
