import Foundation

enum AppGroup {
    static let identifier = "group.io.github.cuinspace233.gatherloom"
    static let defaults = UserDefaults(suiteName: identifier) ?? .standard
    static let serverKey = "GatherloomServerURL"
    static let accountKey = "GatherloomActiveAccount"
    /// The production backend. Release builds always use it; debug builds
    /// default to it and may point at a local server instead.
    static let productionServer = "https://gatherloom.cuinspace.com"

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

        var title: String { self == .active ? String(localized: "进行中") : String(localized: "已归档") }
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
        case .thought: String(localized: "个人想法")
        case .url: String(localized: "链接")
        case .image: String(localized: "截图")
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
    var card: KnowledgeCard?

    var headline: String {
        if let title, !title.isEmpty { return title }
        if !text.isEmpty { return text }
        if let sourceUrl { return sourceUrl }
        return type == .image ? String(localized: "截图") : String(localized: "记录")
    }

    var statusText: String { ProcessingState.text(for: processing) }
}

/// AI-generated summary of an entry. Shown as "AI 整理", never cited as a source.
struct KnowledgeCard: Decodable, Hashable {
    let title: String
    let summary: String
    let keyPoints: [String]
    let keywords: [String]
    let model: String
}

/// Server-side processing of an entry's current revision.
struct ProcessingState: Decodable, Hashable {
    let status: String
    let searchable: Bool
    let stage: String?
    let failedStep: String?
    let errorCode: String?
    let attempts: Int
    let canRetry: Bool
    let updatedAt: Date

    /// Still changing on the server, so worth polling.
    var isActive: Bool { ["queued", "running", "searchable"].contains(status) }

    var failedStepText: String? {
        switch failedStep {
        case "extract": String(localized: "读取网页正文")
        case "index": String(localized: "建立检索索引")
        case "card": String(localized: "生成知识卡片")
        default: nil
        }
    }

    var errorText: String? {
        switch errorCode {
        case "web_auth": String(localized: "网页读取服务的密钥无效，请联系管理员。")
        case "web_quota": String(localized: "网页读取服务的额度不足，请联系管理员。")
        case "web_forbidden": String(localized: "网页读取服务拒绝了请求。")
        case "web_timeout", "web_network", "web_provider_error", "web_rate_limit":
            String(localized: "网页读取服务暂时不可用，请稍后重试。")
        case "web_page_unavailable": String(localized: "网页暂时无法访问，可能需要登录或已失效。")
        case "web_empty": String(localized: "没有读取到网页正文，可以从 Safari 打开网页后重新分享。")
        case "web_too_long", "web_response_too_large": String(localized: "网页内容过长，未保存正文，请改为分享所需的内容。")
        case "web_invalid_url", "web_request_rejected", "web_invalid_response":
            String(localized: "无法读取这个链接的正文，可以从 Safari 打开网页后重新分享。")
        default: nil
        }
    }

    static func text(for status: String) -> String {
        switch status {
        case "queued", "running": String(localized: "已保存到服务器，正在整理")
        case "searchable": String(localized: "已可检索")
        case "succeeded": String(localized: "整理完成")
        case "partial": String(localized: "已保存，部分整理失败")
        case "failed": String(localized: "已保存，整理失败")
        default: String(localized: "已保存到服务器")
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
