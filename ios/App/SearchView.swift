import SwiftUI

struct SearchHit: Decodable, Identifiable {
    let entry: CaptureEntry
    let score: Double
    let chunkId: UUID
    let source: String
    let snippet: String
    let highlights: [[Int]]
    let matchedBy: [String]

    var id: UUID { entry.id }
}

struct SearchPage: Decodable {
    let items: [SearchHit]
    let nextCursor: String?
    let degraded: Bool
}

/// Searches every entry of the account, or one project's, by keywords and meaning.
struct SearchView: View {
    let session: AccountSession
    let capture: CaptureQueue
    @State private var query = ""
    @State private var projectID: UUID?
    @State private var kind: EntryKind?
    @State private var period = Period.all
    @State private var hits: [SearchHit] = []
    @State private var nextCursor: String?
    @State private var degraded = false
    @State private var searchedQuery: String?
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var selectedEntry: CaptureEntry?
    @FocusState private var searchFocused: Bool

    enum Period: String, CaseIterable, Identifiable {
        case week, month, all

        var id: Self { self }
        var title: String {
            switch self {
            case .week: String(localized: "近 7 天")
            case .month: String(localized: "近 30 天")
            case .all: String(localized: "全部时间")
            }
        }
        var since: Date? {
            switch self {
            case .week: Calendar.current.date(byAdding: .day, value: -7, to: .now)
            case .month: Calendar.current.date(byAdding: .day, value: -30, to: .now)
            case .all: nil
            }
        }
    }

    private struct SearchKey: Equatable {
        let query: String
        let projectID: UUID?
        let kind: EntryKind?
        let period: Period
    }

    private var trimmedQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        List {
            if degraded {
                Label("语义检索暂不可用，仅按关键词匹配。", systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, Theme.Space.s)
                    .plainRow()
            }
            ForEach(hits) { hit in
                Button { selectedEntry = hit.entry } label: {
                    EntryRow(
                        kind: hit.entry.type,
                        headline: hit.entry.headline,
                        detail: detail(for: hit.entry),
                        snippet: highlighted(hit.snippet, hit.highlights)
                    )
                }
                .buttonStyle(.plain)
                .plainRow()
            }
            if isLoading {
                ProgressView().frame(maxWidth: .infinity).plainRow()
            } else if let errorMessage {
                Text(errorMessage).foregroundStyle(.secondary).plainRow()
                Button("重试") { Task { await search() } }.plainRow()
            } else if let nextCursor {
                Button("加载更多") { Task { await search(after: nextCursor) } }.plainRow()
            }
        }
        .listStyle(.plain)
        .canvas()
        .scrollDismissesKeyboard(.immediately)
        .overlay {
            if trimmedQuery.isEmpty {
                ContentUnavailableView(
                    "搜索资料", systemImage: "magnifyingglass",
                    description: Text("按关键词或意思查找想法、链接和截图。")
                )
            } else if !isLoading, errorMessage == nil, searchedQuery == trimmedQuery, hits.isEmpty {
                ContentUnavailableView.search(text: trimmedQuery)
            }
        }
        .safeAreaInset(edge: .bottom) { searchBar }
        .navigationTitle("搜索")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { filterMenu }
        .navigationDestination(item: $selectedEntry) { EntryDetailView(capture: capture, entry: $0) }
        .task(id: SearchKey(query: trimmedQuery, projectID: projectID, kind: kind, period: period)) {
            // Wait for typing to pause before searching.
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            await search()
        }
        .task { await capture.refreshProjects() }
        .onAppear { searchFocused = true }
    }

    private var searchBar: some View {
        HStack(spacing: Theme.Space.s) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("搜索资料", text: $query)
                .focused($searchFocused)
                .submitLabel(.search)
            if !query.isEmpty {
                Button("清除", systemImage: "xmark.circle.fill") { query = "" }
                    .labelStyle(.iconOnly)
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, Theme.Space.l)
        .frame(height: 50)
        .glassEffect(.regular, in: .capsule)
        .padding(.horizontal, Theme.Space.xl)
        .padding(.bottom, Theme.Space.s)
    }

    /// Type, age, and where the entry lives.
    private func detail(for entry: CaptureEntry) -> String {
        let names = projectNames(for: entry)
        let place = names.isEmpty ? String(localized: "收集箱") : names.joined(separator: "、")
        return [entry.type.title, entry.createdAt.formatted(.relative(presentation: .named)), place]
            .joined(separator: " · ")
    }

    private var filterMenu: some View {
        Menu {
            Picker("项目", selection: $projectID) {
                Text("全部项目").tag(UUID?.none)
                ForEach(capture.projects) { project in
                    Text(project.name).tag(UUID?.some(project.id))
                }
            }
            Picker("类型", selection: $kind) {
                Text("全部类型").tag(EntryKind?.none)
                ForEach([EntryKind.thought, .url, .image], id: \.self) { kind in
                    Label(kind.title, systemImage: kind.symbol).tag(EntryKind?.some(kind))
                }
            }
            Picker("时间", selection: $period) {
                ForEach(Period.allCases) { period in
                    Text(period.title).tag(period)
                }
            }
        } label: {
            let filtered = projectID != nil || kind != nil || period != .all
            Label("筛选", systemImage: filtered
                ? "line.3.horizontal.decrease.circle.fill"
                : "line.3.horizontal.decrease")
        }
    }

    private func projectNames(for entry: CaptureEntry) -> [String] {
        entry.projectIds.compactMap { id in capture.projects.first { $0.id == id }?.name }
    }

    private func search(after cursor: String? = nil) async {
        let text = trimmedQuery
        guard !text.isEmpty else {
            hits = []
            nextCursor = nil
            degraded = false
            searchedQuery = nil
            errorMessage = nil
            return
        }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        var items = [URLQueryItem(name: "q", value: text), URLQueryItem(name: "limit", value: "20")]
        if let projectID { items.append(URLQueryItem(name: "project_id", value: projectID.uuidString)) }
        if let kind { items.append(URLQueryItem(name: "type", value: kind.rawValue)) }
        if let since = period.since {
            items.append(URLQueryItem(name: "since", value: since.formatted(.iso8601)))
        }
        if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            let page = try await session.request(SearchPage.self, "v1/search", query: items, scope: capture.scope)
            guard !Task.isCancelled, text == trimmedQuery else { return }
            hits = cursor == nil ? page.items : hits + page.items
            nextCursor = page.nextCursor
            degraded = page.degraded
            searchedQuery = text
        } catch {
            guard !(error is CancellationError) else { return }
            errorMessage = String(localized: "搜索失败。\(error.localizedDescription)")
        }
    }
}

/// Bolds the given ranges. Offsets count Unicode scalars, as the server does.
func highlighted(_ text: String, _ ranges: [[Int]]) -> AttributedString {
    let scalars = Array(text.unicodeScalars)
    func piece(_ range: Range<Int>) -> String {
        var view = String.UnicodeScalarView()
        view.append(contentsOf: scalars[range])
        return String(view)
    }
    var result = AttributedString()
    var position = 0
    for range in ranges where range.count == 2 {
        let (start, end) = (range[0], range[1])
        guard start >= position, start < end, end <= scalars.count else { continue }
        result += AttributedString(piece(position..<start))
        var match = AttributedString(piece(start..<end))
        match.inlinePresentationIntent = .stronglyEmphasized
        match.foregroundColor = .primary
        result += match
        position = end
    }
    result += AttributedString(piece(position..<scalars.count))
    return result
}
