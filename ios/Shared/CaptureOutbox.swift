import CryptoKit
import Foundation

/// One local submission. Persisted as `manifest.json` in its own directory,
/// next to the image file for screenshots.
struct CaptureDraft: Codable, Identifiable {
    struct Extract: Codable {
        var title: String
        var text: String
        var canonicalUrl: String?
        var capturedAt: Date?
    }

    struct OCRLine: Codable {
        let text: String
        let box: [Double]
    }

    struct OCR: Codable {
        let text: String
        let lines: [OCRLine]
        let engine: String
    }

    struct ImageFile: Codable {
        let fileName: String
        let sha256: String
        let size: Int
        let contentType: String
    }

    let clientSubmissionId: UUID
    var kind: EntryKind
    let text: String
    let note: String
    var url: String?
    var extract: Extract?
    var image: ImageFile?
    var ocr: OCR?
    var ocrAttempted: Bool
    var attachmentId: UUID?
    var projectId: UUID?
    var projectName: String?
    let createdAt: Date

    var id: UUID { clientSubmissionId }

    var headline: String {
        if let title = extract?.title, !title.isEmpty { return title }
        if !text.isEmpty { return text }
        if let url { return url }
        return kind.title
    }

    init(
        kind: EntryKind, text: String, note: String, url: String? = nil, extract: Extract? = nil,
        image: ImageFile? = nil, project: Project?, id: UUID = UUID()
    ) {
        clientSubmissionId = id
        self.kind = kind
        self.text = text
        self.note = note
        self.url = url
        self.extract = extract
        self.image = image
        ocrAttempted = false
        projectId = project?.id
        projectName = project?.name
        createdAt = Date()
    }

    init(from decoder: Decoder) throws {
        // Drafts written before links and screenshots only had text fields.
        let c = try decoder.container(keyedBy: CodingKeys.self)
        clientSubmissionId = try c.decode(UUID.self, forKey: .clientSubmissionId)
        kind = try c.decodeIfPresent(EntryKind.self, forKey: .kind) ?? .thought
        text = try c.decode(String.self, forKey: .text)
        note = try c.decode(String.self, forKey: .note)
        url = try c.decodeIfPresent(String.self, forKey: .url)
        extract = try c.decodeIfPresent(Extract.self, forKey: .extract)
        image = try c.decodeIfPresent(ImageFile.self, forKey: .image)
        ocr = try c.decodeIfPresent(OCR.self, forKey: .ocr)
        ocrAttempted = try c.decodeIfPresent(Bool.self, forKey: .ocrAttempted) ?? false
        attachmentId = try c.decodeIfPresent(UUID.self, forKey: .attachmentId)
        projectId = try c.decodeIfPresent(UUID.self, forKey: .projectId)
        projectName = try c.decodeIfPresent(String.self, forKey: .projectName)
        createdAt = try c.decode(Date.self, forKey: .createdAt)
    }

    struct Submission: Encodable {
        let clientSubmissionId: UUID
        let type: EntryKind
        let text: String
        let note: String
        let projectId: UUID?
        let url: String?
        let attachmentId: UUID?
        let clientOcr: OCR?
        let clientExtract: Extract?
    }

    func submission(attachmentId: UUID?) -> Submission {
        Submission(
            clientSubmissionId: id, type: kind, text: text, note: note, projectId: projectId,
            url: url, attachmentId: attachmentId, clientOcr: kind == .image ? ocr : nil,
            clientExtract: kind == .url ? extract : nil
        )
    }
}

/// Per-account queue of submissions in the App Group container, shared by the
/// app and the share extension.
struct CaptureOutbox {
    private let scope: CaptureScope
    private let manager = FileManager.default
    private static let manifest = "manifest.json"

    init(scope: CaptureScope) {
        self.scope = scope
    }

    private func directory() throws -> URL {
        guard let container = manager.containerURL(forSecurityApplicationGroupIdentifier: AppGroup.identifier) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let serverKey = SHA256.hash(data: Data(scope.server.absoluteString.utf8))
            .map { String(format: "%02x", $0) }.joined()
        let directory = container.appending(path: "Library/Application Support/Outbox/\(serverKey)/\(scope.accountID)")
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    func load() throws -> (drafts: [CaptureDraft], unreadable: Bool) {
        let root = try directory()
        let items = try manager.contentsOfDirectory(at: root, includingPropertiesForKeys: [.contentModificationDateKey])
        var drafts: [CaptureDraft] = []
        var unreadable = false
        for item in items {
            let name = item.lastPathComponent
            if name.hasPrefix(".") {
                removeIfStale(item)
                continue
            }
            let isLegacy = item.pathExtension == "json"
            guard let id = UUID(uuidString: isLegacy ? item.deletingPathExtension().lastPathComponent : name) else {
                continue
            }
            do {
                let file = isLegacy ? item : item.appending(path: Self.manifest)
                let draft = try JSONDecoder().decode(CaptureDraft.self, from: Data(contentsOf: file))
                guard draft.id == id else { throw CocoaError(.fileReadCorruptFile) }
                drafts.append(draft)
            } catch {
                unreadable = true
            }
        }
        return (drafts.sorted { $0.createdAt < $1.createdAt }, unreadable)
    }

    /// Writes the image and manifest into a hidden directory, then renames it,
    /// so a submission is either fully present or absent.
    func save(_ draft: CaptureDraft, imageData: Data? = nil) throws {
        let root = try directory()
        let staging = root.appending(path: ".\(draft.id)-\(UUID())")
        try manager.createDirectory(at: staging, withIntermediateDirectories: false)
        do {
            if let imageData, let image = draft.image {
                try imageData.write(to: staging.appending(path: image.fileName), options: [.completeFileProtectionUntilFirstUserAuthentication])
            }
            try JSONEncoder().encode(draft).write(
                to: staging.appending(path: Self.manifest), options: [.completeFileProtectionUntilFirstUserAuthentication]
            )
            try manager.moveItem(at: staging, to: root.appending(path: draft.id.uuidString))
        } catch {
            try? manager.removeItem(at: staging)
            throw error
        }
    }

    /// Rewrites the manifest of a submission that already exists.
    func update(_ draft: CaptureDraft) throws {
        let folder = try directory().appending(path: draft.id.uuidString)
        guard manager.fileExists(atPath: folder.path) else {
            // Text drafts saved by older builds are single JSON files.
            return try JSONEncoder().encode(draft).write(
                to: directory().appending(path: "\(draft.id).json"),
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
        }
        try JSONEncoder().encode(draft).write(
            to: folder.appending(path: Self.manifest), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        )
    }

    func imageURL(for draft: CaptureDraft) -> URL? {
        guard let image = draft.image, let root = try? directory() else { return nil }
        return root.appending(path: draft.id.uuidString).appending(path: image.fileName)
    }

    func acknowledge(_ id: UUID) throws {
        let root = try directory()
        for item in [root.appending(path: id.uuidString), root.appending(path: "\(id).json")]
        where manager.fileExists(atPath: item.path) {
            try manager.removeItem(at: item)
        }
    }

    func savedProjects() throws -> [Project] {
        try read([Project].self, name: "projects.json") ?? []
    }

    func saveProjects(_ projects: [Project]) throws {
        try write(projects, name: "projects.json")
    }

    func recentProjectIDs() -> [UUID] {
        (try? read([UUID].self, name: "recent-projects.json")) ?? []
    }

    func markRecent(_ projectID: UUID) {
        let ids = [projectID] + recentProjectIDs().filter { $0 != projectID }
        try? write(Array(ids.prefix(10)), name: "recent-projects.json")
    }

    private func removeIfStale(_ item: URL) {
        let modified = (try? item.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
        if let modified, modified < Date().addingTimeInterval(-3600) {
            try? manager.removeItem(at: item)
        }
    }

    private func read<T: Decodable>(_ type: T.Type, name: String) throws -> T? {
        let url = try directory().appending(path: name)
        guard manager.fileExists(atPath: url.path) else { return nil }
        return try JSONDecoder().decode(type, from: Data(contentsOf: url))
    }

    private func write(_ value: some Encodable, name: String) throws {
        let url = try directory().appending(path: name)
        try JSONEncoder().encode(value).write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
}
