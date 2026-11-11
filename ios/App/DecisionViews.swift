import SwiftUI

struct Decision: Decodable, Identifiable, Hashable {
    let id: UUID
    let projectId: UUID
    let status: String
    let title: String
    let choice: String
    let rationale: String
    let alternatives: [String]
    let premises: [String]
    let sources: [Citation]
    let basisChanged: Bool
    let version: Int
    let supersededBy: UUID?
    let createdAt: Date
    let confirmedAt: Date?

    var isDraft: Bool { status == "draft" }
}

private struct DecisionRevision: Decodable, Identifiable {
    let version: Int
    let title: String
    let choice: String
    let rationale: String
    let createdAt: Date
    var id: Int { version }
}

private struct DecisionAction: Encodable {
    let action: String
    let version: Int
    var title: String?
    var choice: String?
    var rationale: String?
    var supersededBy: UUID?
}

/// 待确认决定 and 正式决定 rows on the project overview.
struct ProjectDecisionsSection: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    @State private var drafts: [Decision] = []
    @State private var confirmed: [Decision] = []

    var body: some View {
        Section("决定") {
            NavigationLink {
                DecisionListView(session: session, capture: capture, project: project, drafts: true)
            } label: {
                HStack {
                    Text("待确认决定")
                    Spacer()
                    if !drafts.isEmpty {
                        Text("\(drafts.count)")
                            .font(.caption.bold())
                            .padding(.horizontal, 8)
                            .padding(.vertical, 2)
                            .background(Color.orange.opacity(0.2), in: Capsule())
                    }
                }
            }
            NavigationLink {
                DecisionListView(session: session, capture: capture, project: project, drafts: false)
            } label: {
                LabeledContent("正式决定", value: confirmed.isEmpty ? "暂无" : "\(confirmed.count) 条")
            }
        }
        .task { await load() }
    }

    private func load() async {
        let path = "v1/projects/\(project.id.uuidString)/decisions"
        drafts = (try? await session.request(
            [Decision].self, path, query: [URLQueryItem(name: "status", value: "draft")], scope: capture.scope
        )) ?? drafts
        confirmed = (try? await session.request(
            [Decision].self, path, query: [URLQueryItem(name: "status", value: "confirmed")], scope: capture.scope
        )) ?? confirmed
    }
}

struct DecisionListView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    let drafts: Bool
    @State private var decisions: [Decision] = []
    @State private var errorMessage: String?

    var body: some View {
        List {
            if drafts {
                let changed = decisions.filter(\.basisChanged)
                let fresh = decisions.filter { !$0.basisChanged }
                if !fresh.isEmpty { Section("新草稿") { rows(fresh) } }
                if !changed.isEmpty {
                    Section {
                        rows(changed)
                    } header: {
                        Text("依据已变化")
                    } footer: {
                        Text("引用的资料在起草后被修改或删除，确认前请重新检查。")
                    }
                }
            } else {
                rows(decisions)
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .overlay {
            if decisions.isEmpty && errorMessage == nil {
                ContentUnavailableView(
                    drafts ? "没有待确认的决定" : "还没有正式决定",
                    systemImage: "checkmark.seal",
                    description: Text(drafts ? "AI 在资料里发现明确的选项与倾向时，会在这里提出草稿。" : "确认的草稿会出现在这里。")
                )
            }
        }
        .navigationTitle(drafts ? "待确认决定" : "正式决定")
        .task { await load() }
        .refreshable { await load() }
    }

    private func rows(_ items: [Decision]) -> some View {
        ForEach(items) { decision in
            NavigationLink {
                DecisionDetailView(session: session, capture: capture, decision: decision) { await load() }
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text(decision.title).font(.headline)
                    Text(decision.choice).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                    if decision.status == "superseded" {
                        Text("已被后续决定替代").font(.caption).foregroundStyle(.secondary)
                    } else if decision.basisChanged && !decision.isDraft {
                        Text("依据已变化").font(.caption).foregroundStyle(.orange)
                    }
                }
            }
        }
    }

    private func load() async {
        do {
            let path = "v1/projects/\(project.id.uuidString)/decisions"
            if drafts {
                decisions = try await session.request(
                    [Decision].self, path, query: [URLQueryItem(name: "status", value: "draft")], scope: capture.scope
                )
            } else {
                let confirmed = try await session.request(
                    [Decision].self, path, query: [URLQueryItem(name: "status", value: "confirmed")], scope: capture.scope
                )
                let superseded = try await session.request(
                    [Decision].self, path, query: [URLQueryItem(name: "status", value: "superseded")], scope: capture.scope
                )
                decisions = confirmed + superseded
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

struct DecisionDetailView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let onChange: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var decision: Decision
    @State private var title: String
    @State private var choice: String
    @State private var rationale: String
    @State private var isEditing = false
    @State private var isWorking = false
    @State private var errorMessage: String?
    @State private var selected: Citation?
    @State private var revisions: [DecisionRevision] = []
    @State private var others: [Decision] = []

    init(session: AccountSession, capture: CaptureQueue, decision: Decision, onChange: @escaping () async -> Void) {
        self.session = session
        self.capture = capture
        self.onChange = onChange
        _decision = State(initialValue: decision)
        _title = State(initialValue: decision.title)
        _choice = State(initialValue: decision.choice)
        _rationale = State(initialValue: decision.rationale)
    }

    var body: some View {
        Form {
            if decision.isDraft {
                Section {
                    Label("这是 AI 提出的草稿，确认后才会成为正式决定。", systemImage: "sparkles")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    if decision.basisChanged {
                        Text("依据已变化：引用的资料在起草后被修改或删除。").font(.footnote).foregroundStyle(.orange)
                    }
                }
            }
            if isEditing {
                Section("编辑") {
                    TextField("主题", text: $title)
                    TextField("选择", text: $choice, axis: .vertical)
                    TextField("理由", text: $rationale, axis: .vertical)
                }
            } else {
                Section("主题") { Text(decision.title).font(.headline) }
                Section(decision.isDraft ? "建议选择" : "决定") { Text(decision.choice) }
                if !decision.rationale.isEmpty {
                    Section("理由") {
                        CitedText(text: decision.rationale, sources: decision.sources) { selected = $0 }
                    }
                }
            }
            if !decision.alternatives.isEmpty {
                Section("备选项") { ForEach(decision.alternatives, id: \.self) { Text($0) } }
            }
            if !decision.premises.isEmpty {
                Section("适用前提") { ForEach(decision.premises, id: \.self) { Text($0) } }
            }
            if !decision.sources.isEmpty {
                Section("依据") {
                    ForEach(decision.sources) { source in
                        Button {
                            selected = source
                        } label: {
                            HStack {
                                Text("[\(source.n)] \(source.title.isEmpty ? source.type.title : source.title)").lineLimit(1)
                                if source.stale { Text("已变化").foregroundStyle(.orange) }
                            }
                            .font(.footnote)
                        }
                    }
                }
            }
            actions
            if !revisions.isEmpty {
                Section("修订历史") {
                    ForEach(revisions) { revision in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("第 \(revision.version) 次 · \(revision.createdAt.formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                            Text(revision.choice)
                        }
                    }
                }
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .disabled(isWorking)
        .navigationTitle(decision.isDraft ? "决定草稿" : "正式决定")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if decision.status == "draft" || decision.status == "confirmed" {
                Button(isEditing ? "完成编辑" : "编辑") { isEditing.toggle() }
            }
        }
        .sheet(item: $selected) { citation in
            CitationSheet(session: session, capture: capture, citation: citation)
        }
        .task { await loadExtras() }
    }

    @ViewBuilder
    private var actions: some View {
        switch decision.status {
        case "draft":
            Section {
                Button("确认为正式决定") { Task { await act("confirm") } }
                Button("拒绝", role: .destructive) { Task { await act("reject") } }
                Button("稍后再说") { dismiss() }
            }
        case "confirmed":
            Section {
                if isEditing {
                    Button("保存修订") { Task { await act("revise") } }
                }
                let candidates = others.filter { $0.id != decision.id }
                if !candidates.isEmpty {
                    Menu("标记为被替代…") {
                        ForEach(candidates) { other in
                            Button(other.title) { Task { await act("supersede", supersededBy: other.id) } }
                        }
                    }
                }
            }
        default:
            EmptyView()
        }
    }

    private func act(_ action: String, supersededBy: UUID? = nil) async {
        isWorking = true
        defer { isWorking = false }
        var body = DecisionAction(action: action, version: decision.version, supersededBy: supersededBy)
        if isEditing || action == "confirm" {
            if title != decision.title { body.title = title }
            if choice != decision.choice { body.choice = choice }
            if rationale != decision.rationale { body.rationale = rationale }
        }
        do {
            decision = try await session.request(
                Decision.self, "v1/decisions/\(decision.id.uuidString)", method: "PATCH", body: body, scope: capture.scope
            )
            isEditing = false
            errorMessage = nil
            await onChange()
            await loadExtras()
            if action == "reject" { dismiss() }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func loadExtras() async {
        guard !decision.isDraft else { return }
        revisions = (try? await session.request(
            [DecisionRevision].self, "v1/decisions/\(decision.id.uuidString)/revisions", scope: capture.scope
        )) ?? []
        others = (try? await session.request(
            [Decision].self, "v1/projects/\(decision.projectId.uuidString)/decisions",
            query: [URLQueryItem(name: "status", value: "confirmed")], scope: capture.scope
        )) ?? []
    }
}
