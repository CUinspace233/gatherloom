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
    private(set) var recentProjectIDs: [UUID] = []
    private(set) var storageError: String?
    private(set) var uploadErrors: [UUID: String] = [:]
    private(set) var uploadingID: UUID?
    private(set) var recognizingID: UUID?
    private(set) var lastReceipt: CaptureEntry?
    /// Changes whenever server-side entries change, so lists reload.
    private(set) var confirmationCount = 0

    @ObservationIgnored private var isActive = false
    @ObservationIgnored private var isLoadingProjects = false
    @ObservationIgnored private var worker: Task<Void, Never>?
    @ObservationIgnored private var retryTimer: Task<Void, Never>?
    @ObservationIgnored private var retrySeconds = 2

    init(scope: CaptureScope, session: AccountSession) {
        self.scope = scope
        self.session = session
        outbox = CaptureOutbox(scope: scope)
    }

    /// Recently used projects first, at most three.
    var recentProjects: [Project] {
        let byID = Dictionary(uniqueKeysWithValues: projects.map { ($0.id, $0) })
        let recent = recentProjectIDs.compactMap { byID[$0] }
        return Array((recent + projects.filter { !recentProjectIDs.contains($0.id) }).prefix(3))
    }

    /// Reads the outbox again, picking up submissions from the share extension.
    private func reload() {
        do {
            let stored = try outbox.load()
            pending = stored.drafts
            storageError = stored.unreadable ? String(localized: "部分本机记录无法读取，文件已保留。") : nil
        } catch {
            storageError = String(localized: "无法打开本机记录。")
        }
        if projects.isEmpty { projects = (try? outbox.savedProjects()) ?? [] }
        recentProjectIDs = outbox.recentProjectIDs()
    }

    func enqueue(_ draft: CaptureDraft, imageData: Data? = nil) throws {
        try outbox.save(draft, imageData: imageData)
        if let project = draft.projectId { markRecent(project) }
        pending.append(draft)
        startWorker()
    }

    func imageURL(for draft: CaptureDraft) -> URL? {
        outbox.imageURL(for: draft)
    }

    func markRecent(_ projectID: UUID) {
        outbox.markRecent(projectID)
        recentProjectIDs = outbox.recentProjectIDs()
    }

    /// After a project is deleted, unsent drafts for it go to the inbox instead.
    func forgetProject(_ projectID: UUID) {
        for var draft in pending where draft.projectId == projectID {
            draft.projectId = nil
            draft.projectName = nil
            try? outbox.update(draft)
            replace(draft)
        }
        projects.removeAll { $0.id == projectID }
        try? outbox.saveProjects(projects)
        noteServerChange()
        retry()
    }

    func noteServerChange() {
        confirmationCount += 1
    }

    func setActive(_ active: Bool) {
        isActive = active
        if active {
            reload()
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
        reload()
        startWorker()
    }

    /// Deletes every local copy that has not reached the server.
    func discardPending() async {
        retryTimer?.cancel()
        retryTimer = nil
        worker?.cancel()
        await worker?.value
        for draft in pending {
            try? outbox.acknowledge(draft.id)
        }
        uploadErrors.removeAll()
        reload()
    }

    func refreshProjects() async {
        reload()
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
        if recognizingID == id { return String(localized: "正在识别截图文字") }
        if uploadingID == id { return String(localized: "正在上传") }
        if uploadErrors[id] != nil { return String(localized: "已存到本机，上传需要处理") }
        if pending.contains(where: { $0.id == id }) { return String(localized: "已存到本机，等待上传") }
        if let lastReceipt, lastReceipt.clientSubmissionId == id { return lastReceipt.statusText }
        return String(localized: "已保存到服务器")
    }

    private var canUpload: Bool {
        isActive && !session.isSigningOut && session.captureScope == scope
    }

    private var uploader: CaptureUploader {
        let session = session
        let scope = scope
        return CaptureUploader(outbox: outbox) { path, method, body, form in
            try await session.send(path, method: method, body: body, form: form, scope: scope)
        }
    }

    private func startWorker() {
        guard canUpload, worker == nil, retryTimer == nil,
              pending.contains(where: { uploadErrors[$0.id] == nil }) else { return }
        worker = Task { [weak self] in await self?.uploadPending() }
    }

    private func uploadPending() async {
        defer {
            uploadingID = nil
            recognizingID = nil
            worker = nil
            startWorker()
        }
        let uploader = uploader
        while canUpload, !Task.isCancelled,
              var draft = pending.first(where: { uploadErrors[$0.id] == nil }) {
            if draft.kind == .image, !draft.ocrAttempted {
                recognizingID = draft.id
                draft = await uploader.recognizeIfNeeded(draft)
                recognizingID = nil
                replace(draft)
            }
            guard canUpload, !Task.isCancelled else { return }
            uploadingID = draft.id
            let receipt: CaptureEntry
            do {
                receipt = try await uploader.deliver(draft)
            } catch {
                guard !Task.isCancelled, canUpload else { return }
                if let apiError = error as? APIError, (400..<500).contains(apiError.status),
                   apiError.status != 408, apiError.status != 429 {
                    uploadErrors[draft.id] = apiError.localizedDescription
                    continue
                }
                if error is CocoaError {
                    uploadErrors[draft.id] = String(localized: "无法读取本机文件，记录已保留。")
                    continue
                }
                scheduleRetry()
                return
            }
            guard !Task.isCancelled, canUpload else { return }
            do {
                try outbox.acknowledge(draft.id)
            } catch {
                uploadErrors[draft.id] = String(localized: "服务器已接收，无法更新本机队列。可以安全重试。")
                continue
            }
            pending.removeAll { $0.id == draft.id }
            lastReceipt = receipt
            confirmationCount += 1
            retrySeconds = 2
        }
    }

    private func replace(_ draft: CaptureDraft) {
        if let index = pending.firstIndex(where: { $0.id == draft.id }) { pending[index] = draft }
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
