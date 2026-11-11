import PhotosUI
import SwiftUI

struct CaptureView: View {
    let capture: CaptureQueue
    @State private var text = ""
    @State private var note = ""
    @State private var projectID: UUID?
    @State private var photo: PhotosPickerItem?
    @State private var image: ImagePayload.Prepared?
    @State private var preview: UIImage?
    @State private var isLoadingPhoto = false
    @State private var lastSaved: CaptureDraft?
    @State private var errorMessage: String?
    @FocusState private var focusedField: Field?

    private enum Field { case text, note }

    private var trimmedText: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var link: String? { LinkDetector.webURL(in: trimmedText) }

    var body: some View {
        Form {
            Section {
                if let preview {
                    Image(uiImage: preview)
                        .resizable()
                        .scaledToFit()
                        .frame(maxHeight: 240)
                        .frame(maxWidth: .infinity)
                        .accessibilityLabel("已选截图")
                    Button("移除截图", role: .destructive) { clearImage() }
                } else {
                    TextField("记下想法，或粘贴链接", text: $text, axis: .vertical)
                        .lineLimit(4...12)
                        .focused($focusedField, equals: .text)
                        .accessibilityIdentifier("capture-text")
                    PhotosPicker(selection: $photo, matching: .images) {
                        Label(isLoadingPhoto ? "正在读取图片" : "选择截图", systemImage: "photo")
                    }
                    .disabled(isLoadingPhoto)
                }
            } header: {
                Text(image != nil ? "截图" : link != nil ? "链接" : "个人想法")
            } footer: {
                if image != nil {
                    Text("保存后在本机识别截图文字，再上传。")
                } else if link != nil {
                    Text("将作为链接保存。")
                }
            }
            Section("我的备注") {
                TextField("备注（可选）", text: $note, axis: .vertical)
                    .focused($focusedField, equals: .note)
                    .lineLimit(1...5)
            }
            Section("归属") {
                ProjectPicker(capture: capture, selection: $projectID)
            }
            Section {
                Button("保存记录", action: save)
                    .disabled(trimmedText.isEmpty && image == nil)
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
                    NavigationLink("查看记录") { savedDestination(lastSaved) }
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
        .background {
            OutsideFieldTap(isEnabled: focusedField != nil) { focusedField = nil }
                .allowsHitTesting(false)
        }
        .task { await capture.refreshProjects() }
        .refreshable { await capture.refreshProjects() }
        .onChange(of: photo) { _, item in
            guard let item else { return }
            Task { await loadPhoto(item) }
        }
    }

    @ViewBuilder
    private func savedDestination(_ draft: CaptureDraft) -> some View {
        if let receipt = capture.lastReceipt, receipt.clientSubmissionId == draft.id,
           !capture.pending.contains(where: { $0.id == draft.id }) {
            EntryDetailView(capture: capture, entry: receipt)
        } else {
            PendingDetailView(capture: capture, draftID: draft.id)
        }
    }

    private func loadPhoto(_ item: PhotosPickerItem) async {
        isLoadingPhoto = true
        defer { isLoadingPhoto = false }
        errorMessage = nil
        guard let data = try? await item.loadTransferable(type: Data.self),
              let prepared = ImagePayload.prepare(data) else {
            errorMessage = "无法读取这张图片。"
            photo = nil
            return
        }
        image = prepared
        preview = ImagePayload.downsampled(prepared.data, maxPixels: 1024)
        focusedField = nil
    }

    private func clearImage() {
        image = nil
        preview = nil
        photo = nil
    }

    private func save() {
        let remark = note.trimmingCharacters(in: .whitespacesAndNewlines)
        let content = trimmedText
        guard content.unicodeScalars.count <= 20_000, remark.unicodeScalars.count <= 4_000,
              !content.contains("\0"), !remark.contains("\0") else {
            errorMessage = "请检查正文和备注。正文最多 20,000 字，备注最多 4,000 字。"
            return
        }
        let project = capture.projects.first { $0.id == projectID }
        if projectID != nil && project == nil {
            errorMessage = "所选项目已不可用，请重新选择归属。"
            return
        }
        let draft: CaptureDraft
        if let image {
            draft = CaptureDraft(kind: .image, text: "", note: remark, image: image.file, project: project)
        } else if let link {
            draft = CaptureDraft(kind: .url, text: "", note: remark, url: link, project: project)
        } else if !content.isEmpty {
            draft = CaptureDraft(kind: .thought, text: content, note: remark, project: project)
        } else {
            return
        }
        do {
            try capture.enqueue(draft, imageData: image?.data)
            lastSaved = draft
            text = ""
            note = ""
            clearImage()
            focusedField = nil
            errorMessage = nil
        } catch {
            errorMessage = "保存到本机失败。内容仍在输入框里，可以重试。"
        }
    }
}

/// Inbox, recent projects first, then every other active project.
struct ProjectPicker: View {
    let capture: CaptureQueue
    @Binding var selection: UUID?

    var body: some View {
        let recent = capture.recentProjects
        let others = capture.projects.filter { project in !recent.contains { $0.id == project.id } }
        Picker("保存到", selection: $selection) {
            Text("收集箱").tag(nil as UUID?)
            if !recent.isEmpty {
                Section("最近项目") {
                    ForEach(recent) { Text($0.name).tag(Optional($0.id)) }
                }
            }
            if !others.isEmpty {
                Section("其他项目") {
                    ForEach(others) { Text($0.name).tag(Optional($0.id)) }
                }
            }
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
                            PendingDetailView(capture: capture, draftID: draft.id)
                        } label: {
                            EntryRow(kind: draft.kind, headline: draft.headline, status: capture.status(for: draft.id))
                        }
                    }
                }
            }
            let pendingIDs = Set(localDrafts.map(\.id))
            ForEach(entries.filter { !pendingIDs.contains($0.clientSubmissionId) }) { entry in
                NavigationLink {
                    EntryDetailView(capture: capture, entry: entry)
                } label: {
                    EntryRow(kind: entry.type, headline: entry.headline, status: entry.statusText)
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
        .navigationTitle(project == nil ? "收集箱" : "项目资料")
        .task(id: capture.confirmationCount) { await load() }
        .refreshable {
            capture.retry()
            await load()
        }
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

private struct EntryRow: View {
    let kind: EntryKind
    let headline: String
    let status: String

    var body: some View {
        Label {
            VStack(alignment: .leading, spacing: 4) {
                Text(headline).lineLimit(2)
                Text(status).font(.caption).foregroundStyle(.secondary)
            }
        } icon: {
            Image(systemName: kind.symbol).foregroundStyle(.secondary)
        }
    }
}

/// A submission still in the local outbox.
struct PendingDetailView: View {
    let capture: CaptureQueue
    let draftID: UUID

    var body: some View {
        Form {
            if let draft = capture.pending.first(where: { $0.id == draftID }) {
                content(draft)
            } else {
                Section("保存状态") { Text(capture.status(for: draftID)) }
            }
        }
        .navigationTitle("记录详情")
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private func content(_ draft: CaptureDraft) -> some View {
        switch draft.kind {
        case .thought:
            Section("个人想法") { Text(draft.text).textSelection(.enabled) }
        case .url:
            Section("链接") {
                if let title = draft.extract?.title, !title.isEmpty { Text(title).font(.headline) }
                if let url = draft.url { Text(url).textSelection(.enabled) }
            }
            if let text = draft.extract?.text, !text.isEmpty {
                Section("分享时页面正文") { Text(text).lineLimit(12).textSelection(.enabled) }
            }
        case .image:
            Section("截图") {
                if let url = capture.imageURL(for: draft), let data = try? Data(contentsOf: url),
                   let image = ImagePayload.downsampled(data, maxPixels: 1600) {
                    Image(uiImage: image).resizable().scaledToFit().frame(maxHeight: 420).frame(maxWidth: .infinity)
                }
            }
            if let ocr = draft.ocr, !ocr.text.isEmpty {
                Section("识别文字") { Text(ocr.text).textSelection(.enabled) }
            }
        }
        if !draft.note.isEmpty {
            Section("我的备注") { Text(draft.note).textSelection(.enabled) }
        }
        Section("保存状态") {
            LabeledContent("归属", value: draft.projectName ?? "收集箱")
            Text(capture.status(for: draft.id))
            if let error = capture.uploadErrors[draft.id] {
                Text(error).foregroundStyle(.red)
                Button("重试上传", action: capture.retry)
            }
        }
    }
}

/// An entry saved on the server.
struct EntryDetailView: View {
    let capture: CaptureQueue
    @Environment(AccountSession.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var entry: CaptureEntry
    @State private var errorMessage: String?
    @State private var isWorking = false
    @State private var isEditing = false
    @State private var confirmsDelete = false
    @State private var processing: ProcessingState?
    @State private var pollGeneration = 0
    @State private var showsFullExtract = false
    @State private var relations: [RelationSuggestion] = []

    init(capture: CaptureQueue, entry: CaptureEntry) {
        self.capture = capture
        _entry = State(initialValue: entry)
    }

    var body: some View {
        Form {
            content
            if let card = entry.card {
                cardSection(card)
            }
            EntryRelationsSection(
                session: session, capture: capture, entryID: entry.id, relations: relations
            ) { await loadRelations() }
            if !entry.note.isEmpty {
                Section("我的备注") { Text(entry.note).textSelection(.enabled) }
            }
            Section("项目") {
                ForEach(entry.projectIds, id: \.self) { id in
                    HStack {
                        Text(capture.projects.first { $0.id == id }?.name ?? "已归档的项目")
                        Spacer()
                        Button("移出") { Task { await setProject(id, included: false) } }
                            .buttonStyle(.borderless)
                    }
                }
                let available = capture.projects.filter { !entry.projectIds.contains($0.id) }
                if entry.projectIds.isEmpty { Text("在收集箱中").foregroundStyle(.secondary) }
                if !available.isEmpty {
                    Menu("加入项目") {
                        ForEach(available) { project in
                            Button(project.name) { Task { await setProject(project.id, included: true) } }
                        }
                    }
                }
            }
            Section("处理状态") {
                Text(ProcessingState.text(for: processing?.status ?? entry.processing))
                if let processing, processing.canRetry {
                    if let step = processing.failedStepText {
                        LabeledContent("失败步骤", value: step)
                    }
                    if processing.searchable {
                        Text("原文已可检索，其余步骤可以重试。").foregroundStyle(.secondary)
                    }
                    Button("重试") { Task { await retry() } }
                }
                if entry.possibleDuplicate { Text("可能与已有记录重复").foregroundStyle(.secondary) }
                LabeledContent("保存时间", value: entry.createdAt.formatted(date: .abbreviated, time: .shortened))
                if entry.revision > 1 { LabeledContent("版本", value: "第 \(entry.revision) 版") }
            }
            if let errorMessage {
                Text(errorMessage).foregroundStyle(.red)
            }
            Section {
                Button("删除记录", role: .destructive) { confirmsDelete = true }
                    // Anchored here so the dialog points at this button.
                    .confirmationDialog("删除这条记录？", isPresented: $confirmsDelete, titleVisibility: .visible) {
                        Button("删除", role: .destructive) { Task { await delete() } }
                    } message: {
                        Text("记录会从收集箱、项目和搜索中移除。")
                    }
            }
        }
        .disabled(isWorking)
        .navigationTitle(entry.type.title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { Button("编辑") { isEditing = true } }
        .task { await reload() }
        .task(id: "\(entry.revision)-\(pollGeneration)") { await pollProcessing() }
        .refreshable { await reload() }
        .sheet(isPresented: $isEditing) {
            EntryEditor(entry: entry) { text, note in try await save(text: text, note: note) }
        }
    }

    private func cardSection(_ card: KnowledgeCard) -> some View {
        Section {
            Text(card.title).font(.headline)
            Text(card.summary)
            if !card.keyPoints.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(card.keyPoints.enumerated()), id: \.offset) { _, point in
                        Label(point, systemImage: "circle.fill")
                            .labelStyle(BulletLabelStyle())
                    }
                }
            }
            if !card.keywords.isEmpty {
                Text(card.keywords.joined(separator: " · "))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Label("知识卡片", systemImage: "sparkles")
        } footer: {
            Text("AI 整理，可能有误；原文以上方内容为准。")
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private var content: some View {
        switch entry.type {
        case .thought:
            Section("个人想法") { Text(entry.text).textSelection(.enabled) }
        case .url:
            Section("链接") {
                if let title = entry.title { Text(title).font(.headline) }
                if let source = entry.sourceUrl, let url = URL(string: source) {
                    Link(source, destination: url)
                }
                if !entry.text.isEmpty { Text(entry.text).textSelection(.enabled) }
            }
            if let text = entry.extractText, !text.isEmpty {
                Section("分享时页面正文") {
                    Text(text).lineLimit(showsFullExtract ? nil : 20).textSelection(.enabled)
                    if text.count > 600 {
                        Button(showsFullExtract ? "收起" : "展开全文") { showsFullExtract.toggle() }
                    }
                }
            }
        case .image:
            Section("截图") {
                if let attachment = entry.attachment {
                    AttachmentImage(scope: capture.scope, attachmentID: attachment.id)
                }
            }
            if let text = entry.ocrText, !text.isEmpty {
                Section("识别文字") { Text(text).textSelection(.enabled) }
            }
        }
    }

    private func loadRelations() async {
        relations = await EntryRelationsSection.load(session: session, capture: capture, entryID: entry.id) ?? relations
    }

    private func reload() async {
        await loadRelations()
        do {
            entry = try await session.request(CaptureEntry.self, "v1/entries/\(entry.id.uuidString)", scope: capture.scope)
            errorMessage = nil
        } catch {
            if !(error is CancellationError) { errorMessage = error.localizedDescription }
        }
    }

    /// Polls while the server is still working, then refreshes the entry once.
    private func pollProcessing() async {
        let path = "v1/entries/\(entry.id.uuidString)/processing"
        while !Task.isCancelled {
            do {
                let state = try await session.request(ProcessingState.self, path, scope: capture.scope)
                processing = state
                if !state.isActive {
                    if state.status != entry.processing { await reload() }
                    return
                }
            } catch {
                if error is CancellationError { return }
            }
            try? await Task.sleep(for: .seconds(3))
        }
    }

    private func retry() async {
        isWorking = true
        defer { isWorking = false }
        do {
            processing = try await session.request(
                ProcessingState.self, "v1/entries/\(entry.id.uuidString)/retry",
                method: "POST", scope: capture.scope
            )
            capture.noteServerChange()
            pollGeneration += 1
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func setProject(_ projectID: UUID, included: Bool) async {
        isWorking = true
        defer { isWorking = false }
        do {
            _ = try await session.send(
                "v1/projects/\(projectID.uuidString)/entries/\(entry.id.uuidString)",
                method: included ? "PUT" : "DELETE", scope: capture.scope
            )
            if included { capture.markRecent(projectID) }
            capture.noteServerChange()
            await reload()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func save(text: String?, note: String) async throws {
        let body = EntryPatch(baseRevision: entry.revision, text: text, note: note)
        _ = try await session.send("v1/entries/\(entry.id.uuidString)", method: "PATCH", body: body, scope: capture.scope)
        capture.noteServerChange()
        await reload()
    }

    private func delete() async {
        isWorking = true
        defer { isWorking = false }
        do {
            _ = try await session.send("v1/entries/\(entry.id.uuidString)", method: "DELETE", scope: capture.scope)
            capture.noteServerChange()
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private struct EntryPatch: Encodable {
        let baseRevision: Int
        let text: String?
        let note: String
    }
}

private struct BulletLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            configuration.icon.font(.system(size: 5)).foregroundStyle(.secondary)
            configuration.title
        }
    }
}

private struct EntryEditor: View {
    let entry: CaptureEntry
    let onSave: (String?, String) async throws -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @State private var note: String
    @State private var isSaving = false
    @State private var errorMessage: String?

    init(entry: CaptureEntry, onSave: @escaping (String?, String) async throws -> Void) {
        self.entry = entry
        self.onSave = onSave
        _text = State(initialValue: entry.text)
        _note = State(initialValue: entry.note)
    }

    var body: some View {
        NavigationStack {
            Form {
                if entry.type == .thought {
                    Section("个人想法") {
                        TextField("想法", text: $text, axis: .vertical).lineLimit(4...12)
                    }
                }
                Section("我的备注") {
                    TextField("备注", text: $note, axis: .vertical).lineLimit(2...8)
                }
                Section {
                    Text("保存后生成新版本，旧版本仍保留。").foregroundStyle(.secondary)
                    if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
                }
            }
            .disabled(isSaving)
            .navigationTitle("编辑记录")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await save() } }.disabled(!canSave)
                }
            }
        }
    }

    private var trimmedText: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var trimmedNote: String { note.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var canSave: Bool {
        (entry.type != .thought || !trimmedText.isEmpty)
            && (trimmedText != entry.text || trimmedNote != entry.note)
    }

    private func save() async {
        isSaving = true
        defer { isSaving = false }
        do {
            try await onSave(entry.type == .thought ? trimmedText : nil, trimmedNote)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

/// Loads a private attachment with the signed-in session.
struct AttachmentImage: View {
    let scope: CaptureScope
    let attachmentID: UUID
    /// Normalized boxes (x, y, width, height, top-left origin) to highlight.
    var highlights: [[Double]] = []
    @Environment(AccountSession.self) private var session
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFit()
                    .overlay {
                        GeometryReader { geometry in
                            ForEach(Array(highlights.enumerated()), id: \.offset) { _, box in
                                if box.count == 4 {
                                    Rectangle()
                                        .fill(Color.yellow.opacity(0.25))
                                        .stroke(Color.orange, lineWidth: 2)
                                        .frame(width: box[2] * geometry.size.width, height: box[3] * geometry.size.height)
                                        .offset(x: box[0] * geometry.size.width, y: box[1] * geometry.size.height)
                                }
                            }
                        }
                    }
                    .frame(maxHeight: 420).frame(maxWidth: .infinity)
            } else if failed {
                Text("无法加载图片").foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task(id: attachmentID) {
            do {
                let data = try await session.send("v1/attachments/\(attachmentID.uuidString)", scope: scope)
                image = ImagePayload.downsampled(data, maxPixels: 2048)
                failed = image == nil
            } catch {
                failed = !(error is CancellationError)
            }
        }
    }
}
