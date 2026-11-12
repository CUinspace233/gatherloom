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
    @State private var showsNote = false
    @State private var showsPhotoPicker = false
    @Environment(AccountSession.self) private var session
    @FocusState private var focusedField: Field?

    private enum Field { case text, note }

    private var trimmedText: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var link: String? { LinkDetector.webURL(in: trimmedText) }

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            center
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Theme.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .safeAreaInset(edge: .bottom) { composer }
        .canvas()
        .navigationBarTitleDisplayMode(.inline)
        .photosPicker(isPresented: $showsPhotoPicker, selection: $photo, matching: .images)
        .background {
            OutsideFieldTap(isEnabled: focusedField != nil) { focusedField = nil }
                .allowsHitTesting(false)
        }
        .task { await capture.refreshProjects() }
        .onChange(of: photo) { _, item in
            guard let item else { return }
            Task { await loadPhoto(item) }
        }
    }

    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: .now)
        let time = switch hour {
        case 5..<11: String(localized: "早上好")
        case 11..<13: String(localized: "中午好")
        case 13..<18: String(localized: "下午好")
        case 18..<23: String(localized: "晚上好")
        default: String(localized: "夜深了")
        }
        guard let name = session.account?.username, !name.isEmpty else { return time }
        return String(localized: "\(time)，\(name)")
    }

    /// The quiet middle of the screen: a greeting, or the chosen screenshot.
    @ViewBuilder
    private var center: some View {
        if let preview {
            VStack(spacing: Theme.Space.m) {
                Image(uiImage: preview)
                    .resizable()
                    .scaledToFit()
                    .frame(maxHeight: 280)
                    .clipShape(.rect(cornerRadius: Theme.Radius.bubble))
                    .accessibilityLabel("已选截图")
                Text("保存后在本机识别截图文字，再上传。").sectionLabel()
                Button("移除截图", role: .destructive) { clearImage() }.font(.footnote)
            }
        } else {
            VStack(spacing: Theme.Space.m) {
                Text(Date.now, format: .dateTime.month().day().weekday(.wide)).sectionLabel()
                Text(greeting)
                    .font(.title)
                    .multilineTextAlignment(.center)
                if let lastSaved {
                    VStack(spacing: Theme.Space.xs) {
                        Text(capture.status(for: lastSaved.id)).foregroundStyle(.secondary)
                        if capture.lastReceipt?.clientSubmissionId == lastSaved.id,
                           capture.lastReceipt?.possibleDuplicate == true {
                            Text("可能与已有记录重复，已作为新记录保存。").foregroundStyle(.secondary)
                        }
                        NavigationLink("查看记录") { savedDestination(lastSaved) }
                    }
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .padding(.top, Theme.Space.s)
                }
            }
        }
    }

    private var canSave: Bool { !trimmedText.isEmpty || image != nil }

    /// The one input surface: destination and sync status above, then the glass field.
    private var composer: some View {
        VStack(alignment: .leading, spacing: Theme.Space.s) {
            if let error = errorMessage ?? capture.storageError {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
            HStack(spacing: Theme.Space.s) {
                ProjectPicker(capture: capture, selection: $projectID)
                    .labelsHidden()
                    .tint(.secondary)
                    .padding(.leading, -Theme.Space.m)
                if link != nil { Text("将作为链接保存").sectionLabel() }
                Spacer(minLength: 0)
                if !capture.pending.isEmpty {
                    Button("未同步 \(capture.pending.count) 条 · 重试", action: capture.retry)
                        .font(.footnote)
                        .foregroundStyle(.warm)
                }
            }
            VStack(spacing: 0) {
                if showsNote {
                    TextField("备注（可选）", text: $note, axis: .vertical)
                        .font(.subheadline)
                        .focused($focusedField, equals: .note)
                        .lineLimit(1...3)
                        .padding(.horizontal, Theme.Space.l)
                        .padding(.top, Theme.Space.m)
                    Divider().padding(.horizontal, Theme.Space.l).padding(.top, Theme.Space.s)
                }
                HStack(alignment: .bottom, spacing: Theme.Space.s) {
                    Menu {
                        Button(isLoadingPhoto ? String(localized: "正在读取图片") : String(localized: "选择截图"), systemImage: "photo") {
                            showsPhotoPicker = true
                        }
                        .disabled(isLoadingPhoto)
                        Button(showsNote ? String(localized: "收起备注") : String(localized: "添加备注"), systemImage: "text.bubble") {
                            showsNote.toggle()
                            focusedField = showsNote ? .note : .text
                        }
                    } label: {
                        Image(systemName: "plus")
                            .frame(width: 38, height: 38)
                            .background(Color.primary.opacity(0.08), in: .circle)
                    }
                    .accessibilityLabel("更多选项")
                    if image == nil {
                        TextField("记下想法，或粘贴链接", text: $text, axis: .vertical)
                            .lineLimit(1...8)
                            .focused($focusedField, equals: .text)
                            .accessibilityIdentifier("capture-text")
                            .padding(.vertical, Theme.Space.s)
                    } else {
                        Text("已选截图")
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, Theme.Space.s)
                    }
                    Button(action: save) {
                        Image(systemName: "arrow.up")
                            .fontWeight(.semibold)
                            .foregroundStyle(.onAccent)
                            .frame(width: 38, height: 38)
                            .background(Color.accentColor, in: .circle)
                    }
                    .buttonStyle(.plain)
                    .disabled(!canSave)
                    .opacity(canSave ? 1 : 0.3)
                    .accessibilityLabel("保存记录")
                }
                .padding(6)
            }
            .glassEffect(.regular, in: .rect(cornerRadius: 25))
        }
        .padding(.horizontal, Theme.Space.l)
        .padding(.bottom, Theme.Space.s)
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
            errorMessage = String(localized: "无法读取这张图片。")
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
            errorMessage = String(localized: "请检查正文和备注。正文最多 20,000 字，备注最多 4,000 字。")
            return
        }
        let project = capture.projects.first { $0.id == projectID }
        if projectID != nil && project == nil {
            errorMessage = String(localized: "所选项目已不可用，请重新选择归属。")
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
            showsNote = false
            clearImage()
            focusedField = nil
            errorMessage = nil
        } catch {
            errorMessage = String(localized: "保存到本机失败。内容仍在输入框里，可以重试。")
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

/// The inbox, or one project's sources: browse by day, filter by type, search, and triage by swiping.
struct CaptureEntriesView: View {
    let session: AccountSession
    let capture: CaptureQueue
    var project: Project? = nil
    @Environment(\.openHome) private var openHome
    @State private var entries: [CaptureEntry] = []
    @State private var nextCursor: String?
    @State private var requestID: UUID?
    @State private var errorMessage: String?
    @State private var kind: EntryKind?
    @State private var selectedEntry: CaptureEntry?
    @State private var selectedDraftID: UUID?
    @State private var assigning: CaptureEntry?
    @State private var deleting: CaptureEntry?
    @State private var isSearching = false
    @State private var query = ""
    @State private var hits: [SearchHit] = []
    @State private var searchCursor: String?
    @State private var searchedQuery: String?
    @State private var isSearchLoading = false
    @FocusState private var searchFocused: Bool

    private struct SearchKey: Equatable {
        let query: String
        let kind: EntryKind?
    }

    private var trimmedQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var localDrafts: [CaptureDraft] {
        capture.pending.filter { $0.projectId == project?.id && (kind == nil || $0.kind == kind) }.reversed()
    }

    /// Server entries that are not still in the local outbox, narrowed by the type filter.
    private var visibleEntries: [CaptureEntry] {
        let pendingIDs = Set(capture.pending.map(\.id))
        return entries.filter { !pendingIDs.contains($0.clientSubmissionId) && (kind == nil || $0.type == kind) }
    }

    /// Entries grouped by calendar day, newest day first.
    private var days: [(day: Date, entries: [CaptureEntry])] {
        let calendar = Calendar.current
        return Dictionary(grouping: visibleEntries) { calendar.startOfDay(for: $0.createdAt) }
            .map { (day: $0.key, entries: $0.value.sorted { $0.createdAt > $1.createdAt }) }
            .sorted { $0.day > $1.day }
    }

    /// The search endpoint has no inbox scope, so inbox results are the hits that belong to no project.
    private var visibleHits: [SearchHit] {
        project == nil ? hits.filter { $0.entry.projectIds.isEmpty } : hits
    }

    var body: some View {
        List {
            if isSearching {
                searchRows
            } else {
                browseRows
            }
        }
        .listStyle(.plain)
        .canvas()
        .scrollDismissesKeyboard(.immediately)
        .safeAreaInset(edge: .bottom) { bottomBar }
        .navigationTitle(project == nil ? String(localized: "收集箱") : String(localized: "项目资料"))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Picker("类型", selection: $kind) {
                        Text("全部类型").tag(EntryKind?.none)
                        ForEach([EntryKind.thought, .url, .image], id: \.self) { kind in
                            Label(kind.title, systemImage: kind.symbol).tag(EntryKind?.some(kind))
                        }
                    }
                } label: {
                    Label("筛选", systemImage: kind == nil
                        ? "line.3.horizontal.decrease"
                        : "line.3.horizontal.decrease.circle.fill")
                }
            }
        }
        .navigationDestination(item: $selectedEntry) { EntryDetailView(capture: capture, entry: $0) }
        .navigationDestination(item: $selectedDraftID) { PendingDetailView(capture: capture, draftID: $0) }
        .sheet(item: $assigning) { entry in
            ProjectChooser(projects: capture.projects.filter { $0.status == .active }) { chosen in
                Task { await setProject(chosen.id, included: true, for: entry) }
            }
        }
        .confirmationDialog("删除这条记录？", isPresented: deletePrompt, titleVisibility: .visible, presenting: deleting) { entry in
            Button("删除记录", role: .destructive) { Task { await delete(entry) } }
        } message: { _ in
            Text("记录会从收集箱、项目和搜索中移除。")
        }
        .task(id: capture.confirmationCount) { await load() }
        .task(id: SearchKey(query: isSearching ? trimmedQuery : "", kind: kind)) {
            // Wait for typing to pause before searching.
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            await search()
        }
        .task { await capture.refreshProjects() }
        .refreshable {
            capture.retry()
            await load()
        }
    }

    private var deletePrompt: Binding<Bool> {
        Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })
    }

    @ViewBuilder
    private var browseRows: some View {
        if !capture.pending.isEmpty {
            HStack {
                Text("\(capture.pending.count) 条未同步")
                Spacer()
                Button("重试", action: capture.retry).buttonStyle(.borderless)
            }
            .font(.footnote)
            .foregroundStyle(.warm)
            .padding(.horizontal, Theme.Space.m)
            .padding(.vertical, Theme.Space.s)
            .background(Color.warm.opacity(0.12), in: .rect(cornerRadius: 12))
            .plainRow()
        }
        ForEach(localDrafts) { draft in
            Button { selectedDraftID = draft.id } label: {
                EntryRow(kind: draft.kind, headline: draft.headline, detail: "\(draft.kind.title) · \(capture.status(for: draft.id))")
            }
            .buttonStyle(.plain)
            .plainRow()
        }
        ForEach(days, id: \.day) { group in
            Section {
                ForEach(group.entries) { entry in
                    entryButton(entry) {
                        EntryRow(kind: entry.type, headline: entry.headline, detail: detail(for: entry))
                    }
                }
            } header: {
                Text(dayTitle(group.day)).sectionLabel().padding(.leading, Theme.Space.xs)
            }
            .listSectionSeparator(.hidden)
        }
        if requestID != nil {
            ProgressView().frame(maxWidth: .infinity).plainRow()
        } else if let errorMessage {
            Text(errorMessage).foregroundStyle(.secondary).plainRow()
            Button("刷新记录") { Task { await load() } }.plainRow()
        } else if let nextCursor {
            Button("加载更多") { Task { await load(after: nextCursor) } }.plainRow()
        } else if visibleEntries.isEmpty && localDrafts.isEmpty {
            if kind != nil {
                ContentUnavailableView("没有符合筛选的记录", systemImage: "line.3.horizontal.decrease").plainRow()
            } else if project == nil {
                ContentUnavailableView("收集箱已清空", systemImage: "tray", description: Text("新记录会先出现在这里。")).plainRow()
            } else {
                ContentUnavailableView("还没有记录", systemImage: "tray").plainRow()
            }
        }
    }

    @ViewBuilder
    private var searchRows: some View {
        ForEach(visibleHits) { hit in
            entryButton(hit.entry) {
                EntryRow(
                    kind: hit.entry.type,
                    headline: hit.entry.headline,
                    detail: detail(for: hit.entry),
                    snippet: highlighted(hit.snippet, hit.highlights)
                )
            }
        }
        if isSearchLoading {
            ProgressView().frame(maxWidth: .infinity).plainRow()
        } else if let errorMessage {
            Text(errorMessage).foregroundStyle(.secondary).plainRow()
        } else if let searchCursor {
            Button("加载更多") { Task { await search(after: searchCursor) } }.plainRow()
        } else if !trimmedQuery.isEmpty, searchedQuery == trimmedQuery, visibleHits.isEmpty {
            ContentUnavailableView.search(text: trimmedQuery).plainRow()
        }
    }

    /// A tappable row with the triage swipe actions.
    private func entryButton(_ entry: CaptureEntry, @ViewBuilder label: () -> some View) -> some View {
        Button { selectedEntry = entry } label: { label() }
            .buttonStyle(.plain)
            .plainRow()
            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                Button("删除", systemImage: "trash") { deleting = entry }.tint(.red)
                if let project {
                    Button("移出项目", systemImage: "folder.badge.minus") {
                        Task { await setProject(project.id, included: false, for: entry) }
                    }
                } else {
                    Button("归入项目", systemImage: "folder.badge.plus") { assigning = entry }
                }
            }
    }

    private var bottomBar: some View {
        HStack(spacing: Theme.Space.m) {
            if isSearching {
                HStack(spacing: Theme.Space.s) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField(project == nil ? String(localized: "搜索收集箱") : String(localized: "搜索项目资料"), text: $query)
                        .focused($searchFocused)
                        .submitLabel(.search)
                }
                .padding(.horizontal, Theme.Space.l)
                .frame(height: 50)
                .glassEffect(.regular, in: .capsule)
                Button("关闭", systemImage: "xmark") {
                    query = ""
                    isSearching = false
                    errorMessage = nil
                }
                .labelStyle(.iconOnly)
                .buttonStyle(.glass)
                .buttonBorderShape(.circle)
                .controlSize(.large)
            } else {
                Button("搜索", systemImage: "magnifyingglass") {
                    errorMessage = nil
                    isSearching = true
                    searchFocused = true
                }
                .labelStyle(.iconOnly)
                .buttonStyle(.glass)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                Spacer()
                if project == nil {
                    Button("新记录", systemImage: "plus", action: openHome).primaryAction()
                }
            }
        }
        .padding(.horizontal, Theme.Space.xl)
        .padding(.bottom, Theme.Space.s)
    }

    private func dayTitle(_ day: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(day) { return String(localized: "今天") }
        if calendar.isDateInYesterday(day) { return String(localized: "昨天") }
        return day.formatted(.dateTime.year().month().day())
    }

    /// Type and age, plus the processing state only while it still needs attention.
    private func detail(for entry: CaptureEntry) -> String {
        var parts = [entry.type.title, entry.createdAt.formatted(.relative(presentation: .named))]
        if !["succeeded", "searchable"].contains(entry.processing) { parts.append(entry.statusText) }
        return parts.joined(separator: " · ")
    }

    private func setProject(_ projectID: UUID, included: Bool, for entry: CaptureEntry) async {
        do {
            _ = try await session.send(
                "v1/projects/\(projectID.uuidString)/entries/\(entry.id.uuidString)",
                method: included ? "PUT" : "DELETE", scope: capture.scope
            )
            if included { capture.markRecent(projectID) }
            withAnimation { remove(entry) }
            capture.noteServerChange()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func delete(_ entry: CaptureEntry) async {
        do {
            _ = try await session.send("v1/entries/\(entry.id.uuidString)", method: "DELETE", scope: capture.scope)
            withAnimation { remove(entry) }
            capture.noteServerChange()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func remove(_ entry: CaptureEntry) {
        entries.removeAll { $0.id == entry.id }
        hits.removeAll { $0.entry.id == entry.id }
    }

    private func load(after cursor: String? = nil) async {
        if cursor != nil && requestID != nil { return }
        let id = UUID()
        requestID = id
        if !isSearching { errorMessage = nil }
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
            if !isSearching {
                errorMessage = String(localized: "暂时无法刷新服务器记录。\(error.localizedDescription)")
            }
        }
    }

    private func search(after cursor: String? = nil) async {
        let text = trimmedQuery
        guard isSearching, !text.isEmpty else {
            hits = []
            searchCursor = nil
            searchedQuery = nil
            return
        }
        isSearchLoading = true
        errorMessage = nil
        defer { isSearchLoading = false }
        var items = [URLQueryItem(name: "q", value: text), URLQueryItem(name: "limit", value: "20")]
        if let project { items.append(URLQueryItem(name: "project_id", value: project.id.uuidString)) }
        if let kind { items.append(URLQueryItem(name: "type", value: kind.rawValue)) }
        if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            let page = try await session.request(SearchPage.self, "v1/search", query: items, scope: capture.scope)
            guard !Task.isCancelled, text == trimmedQuery else { return }
            hits = cursor == nil ? page.items : hits + page.items
            searchCursor = page.nextCursor
            searchedQuery = text
        } catch {
            guard !(error is CancellationError) else { return }
            errorMessage = String(localized: "搜索失败。\(error.localizedDescription)")
        }
    }
}

extension View {
    /// A list row with no separator and the page's own margins and background.
    func plainRow() -> some View {
        listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 0, leading: Theme.Space.xl, bottom: 0, trailing: Theme.Space.xl))
    }
}

struct EntryRow: View {
    let kind: EntryKind
    let headline: String
    let detail: String
    var snippet: AttributedString? = nil

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Space.l) {
            Image(systemName: kind.symbol)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(width: 36, height: 36)
                .background(Color.primary.opacity(0.06), in: .rect(cornerRadius: 10))
            VStack(alignment: .leading, spacing: 2) {
                Text(headline).lineLimit(2)
                if let snippet {
                    Text(snippet).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                }
                Text(detail).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, Theme.Space.s)
        .contentShape(.rect)
    }
}

/// Picks the project an inbox entry should move into.
private struct ProjectChooser: View {
    let projects: [Project]
    let onChoose: (Project) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List(projects) { project in
                Button {
                    onChoose(project)
                    dismiss()
                } label: {
                    Label(project.name, systemImage: "folder")
                }
            }
            .overlay {
                if projects.isEmpty {
                    ContentUnavailableView("还没有项目", systemImage: "folder")
                }
            }
            .navigationTitle("归入项目")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭", systemImage: "xmark") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
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
        .canvas(grouped: true)
        .navigationTitle("记录详情")
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private func content(_ draft: CaptureDraft) -> some View {
        switch draft.kind {
        case .thought:
            Section("个人想法") { Text(draft.text).readingText().textSelection(.enabled) }
        case .url:
            Section("链接") {
                if let title = draft.extract?.title, !title.isEmpty { Text(title).font(.headline) }
                if let url = draft.url { Text(url).textSelection(.enabled) }
            }
            if let text = draft.extract?.text, !text.isEmpty {
                Section("分享时页面正文") { Text(text).readingText().lineLimit(12).textSelection(.enabled) }
            }
        case .image:
            Section("截图") {
                if let url = capture.imageURL(for: draft), let data = try? Data(contentsOf: url),
                   let image = ImagePayload.downsampled(data, maxPixels: 1600) {
                    Image(uiImage: image).resizable().scaledToFit().frame(maxHeight: 420).frame(maxWidth: .infinity)
                }
            }
            if let ocr = draft.ocr, !ocr.text.isEmpty {
                Section("识别文字") { Text(ocr.text).readingText().textSelection(.enabled) }
            }
        }
        if !draft.note.isEmpty {
            Section("我的备注") { Text(draft.note).textSelection(.enabled) }
        }
        Section("保存状态") {
            LabeledContent("归属", value: draft.projectName ?? String(localized: "收集箱"))
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
                        Text(capture.projects.first { $0.id == id }?.name ?? String(localized: "已归档的项目"))
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
                if entry.revision > 1 { LabeledContent("版本", value: String(localized: "第 \(entry.revision) 版")) }
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
        .canvas(grouped: true)
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
            Text(card.summary).readingText()
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
            Section("个人想法") { Text(entry.text).readingText().textSelection(.enabled) }
        case .url:
            Section("链接") {
                if let title = entry.title { Text(title).font(.headline) }
                if let source = entry.sourceUrl, let url = URL(string: source) {
                    Link(source, destination: url)
                }
                if !entry.text.isEmpty { Text(entry.text).readingText().textSelection(.enabled) }
            }
            if let text = entry.extractText, !text.isEmpty {
                Section("分享时页面正文") {
                    Text(text).readingText().lineLimit(showsFullExtract ? nil : 20).textSelection(.enabled)
                    if text.count > 600 {
                        Button(showsFullExtract ? String(localized: "收起") : String(localized: "展开全文")) { showsFullExtract.toggle() }
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
                Section("识别文字") { Text(text).readingText().textSelection(.enabled) }
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
