import CryptoKit
import Foundation

struct CaptureScope: Hashable {
    let server: URL
    let accountID: UUID
}

struct CaptureDraft: Codable, Identifiable {
    let clientSubmissionId: UUID
    let text: String
    let note: String
    let projectId: UUID?
    let projectName: String?
    let createdAt: Date

    var id: UUID { clientSubmissionId }

    var submission: Submission {
        Submission(clientSubmissionId: id, text: text, note: note, projectId: projectId)
    }

    struct Submission: Encodable {
        let clientSubmissionId: UUID
        let text: String
        let note: String
        let projectId: UUID?
    }
}

struct CaptureEntry: Decodable, Identifiable {
    let id: UUID
    let clientSubmissionId: UUID
    let text: String
    let note: String
    let possibleDuplicate: Bool
}

struct CaptureOutbox {
    private let scope: CaptureScope
    private let manager = FileManager.default

    init(scope: CaptureScope) {
        self.scope = scope
    }

    private func directory() throws -> URL {
        guard let container = manager.containerURL(
            forSecurityApplicationGroupIdentifier: "group.io.github.cuinspace233.gatherloom"
        ) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let serverKey = SHA256.hash(data: Data(scope.server.absoluteString.utf8))
            .map { String(format: "%02x", $0) }.joined()
        let directory = container.appending(path: "Library/Application Support/Outbox/\(serverKey)/\(scope.accountID)")
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    func load() throws -> (drafts: [CaptureDraft], unreadable: Bool) {
        let files = try manager.contentsOfDirectory(at: directory(), includingPropertiesForKeys: nil)
        var drafts: [CaptureDraft] = []
        var unreadable = false
        for file in files where file.pathExtension == "json" {
            guard let id = UUID(uuidString: file.deletingPathExtension().lastPathComponent) else { continue }
            do {
                let draft = try JSONDecoder().decode(CaptureDraft.self, from: Data(contentsOf: file))
                guard draft.id == id else { throw CocoaError(.fileReadCorruptFile) }
                drafts.append(draft)
            } catch {
                unreadable = true
            }
        }
        return (drafts.sorted { $0.createdAt < $1.createdAt }, unreadable)
    }

    func save(_ draft: CaptureDraft) throws {
        try write(draft, name: "\(draft.id).json")
    }

    func acknowledge(_ id: UUID) throws {
        try manager.removeItem(at: directory().appending(path: "\(id).json"))
    }

    func savedProjects() throws -> [Project] {
        let url = try directory().appending(path: "projects.json")
        guard manager.fileExists(atPath: url.path) else { return [] }
        return try JSONDecoder().decode([Project].self, from: Data(contentsOf: url))
    }

    func saveProjects(_ projects: [Project]) throws {
        try write(projects, name: "projects.json")
    }

    private func write(_ value: some Encodable, name: String) throws {
        let url = try directory().appending(path: name)
        try JSONEncoder().encode(value).write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
}
