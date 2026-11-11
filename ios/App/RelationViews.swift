import SwiftUI

struct RelationSuggestion: Decodable, Identifiable, Hashable {
    struct EntryBrief: Decodable, Hashable {
        let id: UUID
        let title: String
        let type: EntryKind
    }

    struct QuestionBrief: Decodable, Hashable {
        let id: UUID
        let text: String
        let status: String
    }

    let id: UUID
    let projectId: UUID
    let entry: EntryBrief
    let targetEntry: EntryBrief?
    let targetQuestion: QuestionBrief?
    let reason: String
    let status: String
}

private struct RelationDecision: Encodable {
    let status: String
}

/// 关联建议 for an entry: related entries and open questions, accept or dismiss.
struct EntryRelationsSection: View {
    let session: AccountSession
    let capture: CaptureQueue
    let entryID: UUID
    let relations: [RelationSuggestion]
    let onChange: () async -> Void

    static func load(session: AccountSession, capture: CaptureQueue, entryID: UUID) async -> [RelationSuggestion]? {
        try? await session.request(
            [RelationSuggestion].self, "v1/entries/\(entryID.uuidString)/relations", scope: capture.scope
        )
    }

    var body: some View {
        if !relations.isEmpty {
            Section {
                ForEach(relations) { relation in
                    VStack(alignment: .leading, spacing: 6) {
                        target(relation)
                        Text(relation.reason).font(.footnote).foregroundStyle(.secondary)
                        if relation.status == "suggested" {
                            HStack {
                                Button("接受") { Task { await decide(relation, "accepted") } }
                                Button("忽略", role: .destructive) { Task { await decide(relation, "dismissed") } }
                            }
                            .buttonStyle(.borderless)
                            .font(.footnote)
                        } else {
                            Label("已确认关联", systemImage: "checkmark").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            } header: {
                Label("关联建议", systemImage: "link")
            } footer: {
                Text("AI 建议，接受后保留，忽略后不再出现。")
            }
        }
    }

    @ViewBuilder
    private func target(_ relation: RelationSuggestion) -> some View {
        if let question = relation.targetQuestion {
            Label("与待解问题相关：\(question.text)", systemImage: "questionmark.circle")
        } else if let other = [relation.targetEntry, relation.entry].compactMap({ $0 }).first(where: { $0.id != entryID }) {
            NavigationLink {
                CitedEntryView(session: session, capture: capture, entryID: other.id)
            } label: {
                Label("相关资料：\(other.title.isEmpty ? other.type.title : other.title)", systemImage: other.type.symbol)
            }
        }
    }

    private func decide(_ relation: RelationSuggestion, _ status: String) async {
        _ = try? await session.send(
            "v1/relations/\(relation.id.uuidString)", method: "PATCH",
            body: RelationDecision(status: status), scope: capture.scope
        )
        await onChange()
    }
}

/// Entries suggested as relevant to one open question.
struct QuestionRelationsSection: View {
    let session: AccountSession
    let projectID: UUID
    let questionID: UUID
    @State private var relations: [RelationSuggestion] = []

    var body: some View {
        Section("相关资料（AI 建议）") {
            if relations.isEmpty {
                Text("还没有建议。加入新资料后会自动发现。").foregroundStyle(.secondary)
            }
            ForEach(relations) { relation in
                VStack(alignment: .leading, spacing: 4) {
                    Label(relation.entry.title.isEmpty ? relation.entry.type.title : relation.entry.title,
                          systemImage: relation.entry.type.symbol)
                    Text(relation.reason).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
        .task {
            relations = (try? await session.request(
                [RelationSuggestion].self, "v1/projects/\(projectID.uuidString)/relations",
                query: [URLQueryItem(name: "question_id", value: questionID.uuidString)]
            )) ?? []
        }
    }
}
