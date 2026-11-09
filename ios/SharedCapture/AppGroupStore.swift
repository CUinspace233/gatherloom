import Foundation

/// App Group 共享目录的读写。S0 只用于验证共享容器，以及让主 App 读取分享扩展的探针记录。
struct AppGroupStore {
    enum StoreError: LocalizedError {
        case disabled
        case containerUnavailable(String)

        var errorDescription: String? {
            switch self {
            case .disabled:
                return "当前构建未启用 App Group（ENTITLEMENTS_VARIANT 不为空）"
            case .containerUnavailable(let id):
                return "无法获取共享容器：\(id)。检查签名能力是否包含该 App Group。"
            }
        }
    }

    let containerURL: URL

    init() throws {
        guard SharedConfig.appGroupEnabled else { throw StoreError.disabled }
        let id = SharedConfig.appGroupID
        guard let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: id) else {
            throw StoreError.containerUnavailable(id)
        }
        containerURL = url
    }

    var probeDirectory: URL { containerURL.appendingPathComponent("S0Probes", isDirectory: true) }
    var shareRecordDirectory: URL { probeDirectory.appendingPathComponent("share", isDirectory: true) }

    /// 原子写入：先写临时文件再替换，读者不会看到半个文件。
    @discardableResult
    func write(_ data: Data, to directory: URL, name: String) throws -> URL {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return url
    }

    func files(in directory: URL) -> [URL] {
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        )) ?? []
        return urls.sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    func saveShareRecord(_ record: ShareCaptureRecord) throws -> URL {
        let data = try JSONEncoder.probe.encode(record)
        return try write(data, to: shareRecordDirectory, name: "\(record.createdAt.fileStamp)-\(record.id).json")
    }

    func loadShareRecords() -> [ShareCaptureRecord] {
        files(in: shareRecordDirectory)
            .filter { $0.pathExtension == "json" }
            .compactMap { url in
                guard let data = try? Data(contentsOf: url) else { return nil }
                return try? JSONDecoder.probe.decode(ShareCaptureRecord.self, from: data)
            }
    }

    func deleteShareRecords() throws {
        if FileManager.default.fileExists(atPath: shareRecordDirectory.path) {
            try FileManager.default.removeItem(at: shareRecordDirectory)
        }
    }
}
