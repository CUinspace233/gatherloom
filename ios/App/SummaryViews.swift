import SwiftUI

struct SummaryPoint: Decodable, Hashable {
    let text: String
    let citations: [Int]
}

struct SummaryCoverage: Decodable, Hashable {
    let entries: Int
    let ready: Int
    let represented: Int
    let partial: Bool
}

struct ProjectSummary: Decodable, Hashable {
    let version: Int
    let understanding: String
    let keyPoints: [SummaryPoint]
    let uncertainties: [String]
    let changeNote: String
    let sources: [Citation]
    let coverage: SummaryCoverage
    let basedOn: Date?
    let createdAt: Date
}

struct ProjectSummaries: Decodable {
    struct Item: Decodable, Identifiable, Hashable {
        let version: Int
        let changeNote: String
        let createdAt: Date
        var id: Int { version }
    }

    let current: ProjectSummary?
    let history: [Item]
    let updating: Bool
}

/// Text with tappable [n] markers that open the matching citation.
struct CitedText: View {
    let text: String
    let sources: [Citation]
    let onCitation: (Citation) -> Void

    var body: some View {
        Text(answerText(text, cited: Set(sources.map(\.n))))
            .readingText()
            .textSelection(.enabled)
            .environment(\.openURL, OpenURLAction { url in
                guard url.scheme == "gatherloom-citation", let n = Int(url.host() ?? ""),
                      let citation = sources.first(where: { $0.n == n }) else {
                    return .systemAction
                }
                onCitation(citation)
                return .handled
            })
    }
}

extension ProjectSummary {
    var coverageText: String {
        var parts: [String] = []
        if let basedOn {
            parts.append(String(localized: "基于截至 \(basedOn.formatted(date: .abbreviated, time: .shortened)) 的已处理内容"))
        }
        if coverage.partial {
            parts.append(String(localized: "部分覆盖：参考了 \(coverage.represented) / \(coverage.ready) 条资料"))
        }
        if coverage.entries > coverage.ready {
            parts.append(String(localized: "\(coverage.entries - coverage.ready) 条资料仍在整理"))
        }
        return parts.joined(separator: " · ")
    }
}

/// The AI summary card at the top of a project.
struct ProjectSummarySection: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    @State private var summaries: ProjectSummaries?
    @State private var selected: Citation?

    var body: some View {
        Section {
            if let current = summaries?.current {
                CitedText(text: current.understanding, sources: current.sources) { selected = $0 }
                    .lineLimit(8)
                Text(current.coverageText).font(.caption).foregroundStyle(.secondary)
                if summaries?.updating == true {
                    Label("有新变化，正在更新…", systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                NavigationLink("完整总结与历史版本") {
                    SummaryDetailView(
                        session: session, capture: capture, project: project,
                        summary: current, history: summaries?.history ?? []
                    )
                }
            } else if summaries?.updating == true {
                Label("正在生成第一版总结…", systemImage: "hourglass").foregroundStyle(.secondary)
            } else {
                Text("加入资料后，这里会自动总结项目的当前认识。").foregroundStyle(.secondary)
            }
        } header: {
            Label("AI 总结", systemImage: "sparkles")
        } footer: {
            if summaries?.current != nil { Text("AI 整理，可能有误；点 [n] 查看依据。") }
        }
        .task { await load() }
        .sheet(item: $selected) { citation in
            CitationSheet(session: session, capture: capture, citation: citation)
        }
    }

    private func load() async {
        summaries = try? await session.request(
            ProjectSummaries.self, "v1/projects/\(project.id.uuidString)/summaries", scope: capture.scope
        )
    }
}

struct SummaryDetailView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    let summary: ProjectSummary
    var history: [ProjectSummaries.Item] = []
    @State private var selected: Citation?

    var body: some View {
        List {
            Section("当前认识") {
                CitedText(text: summary.understanding, sources: summary.sources) { selected = $0 }
            }
            if !summary.keyPoints.isEmpty {
                Section("重要依据") {
                    ForEach(summary.keyPoints, id: \.self) { point in
                        CitedText(text: point.text, sources: summary.sources) { selected = $0 }
                    }
                }
            }
            if !summary.uncertainties.isEmpty {
                Section("不确定项") {
                    ForEach(summary.uncertainties, id: \.self) { Text($0) }
                }
            }
            Section("这一版") {
                LabeledContent("版本", value: String(localized: "第 \(summary.version) 版"))
                if !summary.changeNote.isEmpty { LabeledContent("变化", value: summary.changeNote) }
                Text(summary.coverageText).font(.footnote).foregroundStyle(.secondary)
            }
            if history.count > 1 {
                Section("历史版本") {
                    ForEach(history.filter { $0.version != summary.version }) { item in
                        NavigationLink {
                            SummaryVersionView(session: session, capture: capture, project: project, version: item.version)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("第 \(item.version) 版 · \(item.createdAt.formatted(date: .abbreviated, time: .shortened))")
                                if !item.changeNote.isEmpty {
                                    Text(item.changeNote).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
        }
        .canvas(grouped: true)
        .navigationTitle("AI 总结")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $selected) { citation in
            CitationSheet(session: session, capture: capture, citation: citation)
        }
    }
}

private struct SummaryVersionView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    let version: Int
    @State private var summary: ProjectSummary?
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if let summary {
                SummaryDetailView(session: session, capture: capture, project: project, summary: summary)
            } else if let errorMessage {
                ContentUnavailableView("无法打开", systemImage: "doc.questionmark", description: Text(errorMessage))
            } else {
                ProgressView()
            }
        }
        .task {
            do {
                summary = try await session.request(
                    ProjectSummary.self, "v1/projects/\(project.id.uuidString)/summaries/\(version)",
                    scope: capture.scope
                )
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
