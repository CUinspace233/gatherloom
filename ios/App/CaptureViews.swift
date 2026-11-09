import SwiftUI

struct CaptureView: View {
    let capture: CaptureQueue
    @State private var text = ""
    @State private var note = ""
    @State private var projectID: UUID?
    @State private var lastSaved: CaptureDraft?
    @State private var errorMessage: String?
    @FocusState private var isEditing: Bool

    var body: some View {
        Form {
            Section("个人想法") {
                TextField("记下一个想法", text: $text, axis: .vertical)
                    .lineLimit(4...12)
                    .focused($isEditing)
                    .accessibilityIdentifier("capture-text")
                TextField("备注（可选）", text: $note, axis: .vertical)
                    .lineLimit(1...5)
            }
            Section("归属") {
                Picker("保存到", selection: $projectID) {
                    Text("收集箱").tag(nil as UUID?)
                    ForEach(capture.projects) { project in
                        Text(project.name).tag(Optional(project.id))
                    }
                }
            }
            Section {
                Button("保存记录", action: save)
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if let error = errorMessage ?? capture.storageError {
                    Text(error).foregroundStyle(.red)
                }
            }
            if let lastSaved {
                Section("最近保存") {
                    Text(capture.status(for: lastSaved.id))
                    if capture.lastReceipt?.clientSubmissionId == lastSaved.id,
                       capture.lastReceipt?.possibleDuplicate == true {
                        Text("可能与已有记录重复，已作为新记录保存。")
                            .foregroundStyle(.secondary)
                    }
                    NavigationLink("查看记录") {
                        CaptureDetailView(capture: capture, draft: lastSaved)
                    }
                }
            }
            if !capture.pending.isEmpty {
                Section {
                    LabeledContent("本机未同步", value: "\(capture.pending.count) 条")
                    Button("重试上传", action: capture.retry)
                }
            }
        }
        .scrollDismissesKeyboard(.immediately)
        .task { await capture.refreshProjects() }
        .refreshable { await capture.refreshProjects() }
    }

    private func save() {
        let content = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let remark = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !content.isEmpty, content.unicodeScalars.count <= 20_000,
              remark.unicodeScalars.count <= 4_000,
              !content.contains("\0"), !remark.contains("\0") else {
            errorMessage = "请检查正文和备注。正文最多 20,000 字，备注最多 4,000 字。"
            return
        }
        let project = capture.projects.first { $0.id == projectID }
        if projectID != nil && project == nil {
            errorMessage = "所选项目已不可用，请重新选择归属。"
            return
        }
        do {
            lastSaved = try capture.enqueue(text: content, note: remark, project: project)
            text = ""
            note = ""
            isEditing = false
            errorMessage = nil
        } catch {
            errorMessage = "保存到本机失败。文字仍在输入框里，可以重试。"
        }
    }
}

struct CaptureEntriesView: View {
    let session: AccountSession
    let capture: CaptureQueue
    var project: Project? = nil
    @State private var entries: [CaptureEntry] = []
    @State private var nextCursor: String?
    @State private var requestID: UUID?
    @State private var errorMessage: String?

    private var localDrafts: [CaptureDraft] {
        capture.pending.filter { $0.projectId == project?.id }.reversed()
    }

    var body: some View {
        List {
            if !localDrafts.isEmpty {
                Section("本机未同步") {
                    ForEach(localDrafts) { draft in
                        NavigationLink {
                            CaptureDetailView(capture: capture, draft: draft)
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(draft.text).lineLimit(2)
                                Text(capture.status(for: draft.id)).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            let pendingIDs = Set(localDrafts.map(\.id))
            ForEach(entries.filter { !pendingIDs.contains($0.clientSubmissionId) }) { entry in
                NavigationLink {
                    CaptureDetailView(entry: entry)
                } label: {
                    Text(entry.text).lineLimit(2)
                }
            }
            if requestID != nil {
                ProgressView()
            } else if let errorMessage {
                Text(errorMessage).foregroundStyle(.secondary)
                Button("刷新记录") { Task { await load() } }
            } else if let nextCursor {
                Button("加载更多") { Task { await load(after: nextCursor) } }
            } else if entries.isEmpty && localDrafts.isEmpty {
                ContentUnavailableView("还没有记录", systemImage: "tray")
            }
        }
        .navigationTitle(project == nil ? "收集箱" : "项目记录")
        .task(id: capture.confirmationCount) { await load() }
        .refreshable { await load() }
    }

    private func load(after cursor: String? = nil) async {
        if cursor != nil && requestID != nil { return }
        let id = UUID()
        requestID = id
        errorMessage = nil
        defer { if requestID == id { requestID = nil } }
        do {
            var query = project.map { [URLQueryItem(name: "project_id", value: $0.id.uuidString)] }
                ?? [URLQueryItem(name: "inbox", value: "true")]
            if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
            let page = try await session.request(Page<CaptureEntry>.self, "v1/entries", query: query, scope: capture.scope)
            guard requestID == id, !Task.isCancelled else { return }
            if cursor == nil {
                entries = page.items
            } else {
                entries.append(contentsOf: page.items)
            }
            nextCursor = page.nextCursor
        } catch {
            guard requestID == id, !Task.isCancelled else { return }
            errorMessage = "暂时无法刷新服务器记录。\(error.localizedDescription)"
        }
    }
}

private struct CaptureDetailView: View {
    let text: String
    let note: String
    let projectName: String?
    let possibleDuplicate: Bool
    let capture: CaptureQueue?
    let draftID: UUID?

    init(capture: CaptureQueue, draft: CaptureDraft) {
        self.capture = capture
        draftID = draft.id
        text = draft.text
        note = draft.note
        projectName = draft.projectName ?? "收集箱"
        possibleDuplicate = false
    }

    init(entry: CaptureEntry) {
        capture = nil
        draftID = nil
        text = entry.text
        note = entry.note
        projectName = nil
        possibleDuplicate = entry.possibleDuplicate
    }

    var body: some View {
        Form {
            Section("个人想法") { Text(text).textSelection(.enabled) }
            if !note.isEmpty {
                Section("备注") { Text(note).textSelection(.enabled) }
            }
            Section("保存状态") {
                if let projectName { LabeledContent("归属", value: projectName) }
                if let capture, let draftID {
                    Text(capture.status(for: draftID))
                    if let error = capture.uploadErrors[draftID] {
                        Text(error).foregroundStyle(.red)
                        Button("重试上传", action: capture.retry)
                    }
                } else {
                    Text("已保存到服务器")
                }
                if possibleDuplicate { Text("可能与已有记录重复").foregroundStyle(.secondary) }
            }
        }
        .navigationTitle("记录详情")
        .navigationBarTitleDisplayMode(.inline)
    }
}
