import Foundation

enum AppGroup {
    static let identifier = "group.io.github.cuinspace233.gatherloom"
    static let defaults = UserDefaults(suiteName: identifier) ?? .standard
    static let serverKey = "GatherloomServerURL"
    static let accountKey = "GatherloomActiveAccount"

    /// The account the main app is signed in to, shared with the extension.
    static var activeScope: CaptureScope? {
        guard let server = defaults.string(forKey: serverKey).flatMap(URL.init(string:)),
              let account = defaults.string(forKey: accountKey).flatMap(UUID.init(uuidString:)) else {
            return nil
        }
        return CaptureScope(server: server, accountID: account)
    }
}

struct CaptureScope: Hashable {
    let server: URL
    let accountID: UUID
}

struct Project: Codable, Identifiable, Hashable {
    let id: UUID
    let name: String
    let goal: String
    let status: Status

    enum Status: String, Codable, CaseIterable {
        case active, archived

        var title: String { self == .active ? "进行中" : "已归档" }
    }
}

struct Page<Item: Decodable>: Decodable {
    let items: [Item]
    let nextCursor: String?
}

enum EntryKind: String, Codable {
    case thought, url, image

    var title: String {
        switch self {
        case .thought: "个人想法"
        case .url: "链接"
        case .image: "截图"
        }
    }

    var symbol: String {
        switch self {
        case .thought: "text.quote"
        case .url: "link"
        case .image: "photo"
        }
    }
}

struct AttachmentInfo: Codable, Hashable {
    let id: UUID
    let clientSubmissionId: UUID
    let contentType: String
    let size: Int
    let sha256: String
}

/// An entry as stored on the server.
struct CaptureEntry: Decodable, Identifiable, Hashable {
    let id: UUID
    let clientSubmissionId: UUID
    let type: EntryKind
    let revision: Int
    let text: String
    let note: String
    let sourceUrl: String?
    let title: String?
    let attachment: AttachmentInfo?
    let projectIds: [UUID]
    let possibleDuplicate: Bool
    let processing: String
    let createdAt: Date
    var ocrText: String?
    var extractText: String?

    var headline: String {
        if let title, !title.isEmpty { return title }
        if !text.isEmpty { return text }
        if let sourceUrl { return sourceUrl }
        return type == .image ? "截图" : "记录"
    }

    var statusText: String {
        switch processing {
        case "queued", "running": "已保存到服务器，正在整理"
        case "succeeded": "整理完成"
        case "failed": "已保存，部分整理失败"
        default: "已保存到服务器"
        }
    }
}

enum LinkDetector {
    /// Returns the text when it is a single http(s) URL.
    static func webURL(in text: String) -> String? {
        guard !text.isEmpty, text.count <= 2048, !text.contains(where: \.isWhitespace),
              let components = URLComponents(string: text),
              let scheme = components.scheme?.lowercased(), scheme == "http" || scheme == "https",
              components.host?.isEmpty == false, components.user == nil, components.password == nil else {
            return nil
        }
        return text
    }
}
