import Foundation
import Observation

@MainActor
@Observable
final class CaptureQueue {
    let scope: CaptureScope
    private let session: AccountSession
    private let outbox: CaptureOutbox

    private(set) var pending: [CaptureDraft] = []
    private(set) var projects: [Project] = []
    private(set) var storageError: String?
    private(set) var uploadErrors: [UUID: String] = [:]
    private(set) var uploadingID: UUID?
    private(set) var lastReceipt: CaptureEntry?
    private(set) var confirmationCount = 0

    @ObservationIgnored private var isActive = false
    @ObservationIgnored private var didLoad = false
    @ObservationIgnored private var isLoadingProjects = false
    @ObservationIgnored private var worker: Task<Void, Never>?
    @ObservationIgnored private var retryTimer: Task<Void, Never>?
    @ObservationIgnored private var retrySeconds = 2

    init(scope: CaptureScope, session: AccountSession) {
        self.scope = scope
        self.session = session
        outbox = CaptureOutbox(scope: scope)
    }

    private func loadLocal() {
        guard !didLoad else { return }
        didLoad = true
        do {
            let stored = try outbox.load()
            pending = stored.drafts
            if stored.unreadable { storageError = "部分本机记录无法读取，文件已保留。" }
        } catch {
            storageError = "无法打开本机记录。"
        }
        projects = (try? outbox.savedProjects()) ?? []
    }

    func enqueue(text: String, note: String, project: Project?) throws -> CaptureDraft {
        loadLocal()
        let draft = CaptureDraft(
            clientSubmissionId: UUID(), text: text, note: note,
            projectId: project?.id, projectName: project?.name, createdAt: Date()
        )
        try outbox.save(draft)
        pending.append(draft)
        startWorker()
        return draft
    }

    func setActive(_ active: Bool) {
        isActive = active
        if active {
            loadLocal()
            startWorker()
        } else {
            retryTimer?.cancel()
            retryTimer = nil
            worker?.cancel()
        }
    }

    func retry() {
        retryTimer?.cancel()
        retryTimer = nil
        retrySeconds = 2
        uploadErrors.removeAll()
        startWorker()
    }

    func refreshProjects() async {
        loadLocal()
        guard !isLoadingProjects else { return }
        isLoadingProjects = true
        defer { isLoadingProjects = false }
        do {
            var results: [Project] = []
            var cursor: String?
            repeat {
                var query = [URLQueryItem(name: "status", value: "active")]
                if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
                let page = try await session.request(Page<Project>.self, "v1/projects", query: query, scope: scope)
                results.append(contentsOf: page.items)
                cursor = page.nextCursor
            } while cursor != nil
            projects = results
            try? outbox.saveProjects(results)
        } catch {
            // Keep the last known choices available while offline.
        }
    }

    func status(for id: UUID) -> String {
        if uploadingID == id { return "正在上传" }
        if uploadErrors[id] != nil { return "已存到本机，上传需要处理" }
        if pending.contains(where: { $0.id == id }) { return "已存到本机，等待上传" }
        return "已保存到服务器"
    }

    private var canUpload: Bool {
        isActive && !session.isSigningOut && session.captureScope == scope
    }

    private func startWorker() {
        guard canUpload, worker == nil, retryTimer == nil,
              pending.contains(where: { uploadErrors[$0.id] == nil }) else { return }
        worker = Task { [weak self] in await self?.uploadPending() }
    }

    private func uploadPending() async {
        defer {
            uploadingID = nil
            worker = nil
            startWorker()
        }
        while canUpload, !Task.isCancelled,
              let draft = pending.first(where: { uploadErrors[$0.id] == nil }) {
            uploadingID = draft.id
            let receipt: CaptureEntry
            do {
                receipt = try await session.request(
                    CaptureEntry.self, "v1/entries", method: "POST", body: draft.submission, scope: scope
                )
            } catch {
                guard !Task.isCancelled, canUpload else { return }
                if let apiError = error as? APIError, (400..<500).contains(apiError.status),
                   apiError.status != 408, apiError.status != 429 {
                    uploadErrors[draft.id] = apiError.localizedDescription
                    continue
                }
                scheduleRetry()
                return
            }
            guard !Task.isCancelled, canUpload else { return }
            guard receipt.clientSubmissionId == draft.id else {
                uploadErrors[draft.id] = "服务器返回了不同的提交编号，本机记录已保留。"
                continue
            }
            do {
                try outbox.acknowledge(draft.id)
            } catch {
                uploadErrors[draft.id] = "服务器已接收，无法更新本机队列。可以安全重试。"
                continue
            }
            pending.removeAll { $0.id == draft.id }
            lastReceipt = receipt
            confirmationCount += 1
            retrySeconds = 2
        }
    }

    private func scheduleRetry() {
        let delay = retrySeconds
        retrySeconds = min(delay * 2, 30)
        retryTimer = Task { [weak self] in
            do {
                try await Task.sleep(for: .seconds(delay))
            } catch {
                return
            }
            self?.retryTimer = nil
            self?.startWorker()
        }
    }
}
