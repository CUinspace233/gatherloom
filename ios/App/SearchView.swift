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

    enum Period: String, CaseIterable, Identifiable {
        case week, month, all

        var id: Self { self }
        var title: String {
            switch self {
            case .week: "近 7 天"
            case .month: "近 30 天"
            case .all: "全部时间"
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
            }
            ForEach(hits) { hit in
                NavigationLink {
                    EntryDetailView(capture: capture, entry: hit.entry)
                } label: {
                    SearchHitRow(hit: hit, projectNames: projectNames(for: hit.entry))
                }
            }
            if isLoading {
                ProgressView()
            } else if let errorMessage {
                Text(errorMessage).foregroundStyle(.secondary)
                Button("重试") { Task { await search() } }
            } else if let nextCursor {
                Button("加载更多") { Task { await search(after: nextCursor) } }
            } else if trimmedQuery.isEmpty {
                ContentUnavailableView(
                    "搜索资料", systemImage: "magnifyingglass",
                    description: Text("按关键词或意思查找想法、链接和截图。")
                )
            } else if searchedQuery == trimmedQuery && hits.isEmpty {
                ContentUnavailableView.search(text: trimmedQuery)
            }
        }
        .navigationTitle("搜索")
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "搜索资料")
        .toolbar { filterMenu }
        .task(id: SearchKey(query: trimmedQuery, projectID: projectID, kind: kind, period: period)) {
            // Wait for typing to pause before searching.
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            await search()
        }
        .task { await capture.refreshProjects() }
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
                : "line.3.horizontal.decrease.circle")
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
            errorMessage = "搜索失败。\(error.localizedDescription)"
        }
    }
}

private struct SearchHitRow: View {
    let hit: SearchHit
    let projectNames: [String]

    var body: some View {
        Label {
            VStack(alignment: .leading, spacing: 4) {
                Text(hit.entry.headline).font(.headline).lineLimit(2)
                Text(highlighted(hit.snippet, hit.highlights))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                if !projectNames.isEmpty {
                    Text(projectNames.joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.tint)
                }
            }
        } icon: {
            Image(systemName: hit.entry.type.symbol).foregroundStyle(.secondary)
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
