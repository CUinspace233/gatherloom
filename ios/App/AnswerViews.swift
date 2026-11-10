import SwiftUI

struct Conversation: Decodable, Identifiable, Hashable {
    let id: UUID
    let projectId: UUID
    let title: String
    let createdAt: Date
    let updatedAt: Date
}

struct AnswerRun: Decodable, Hashable {
    let id: UUID
    let status: String
    let stage: String?
    let errorCode: String?
    let attempts: Int
    let canRetry: Bool
    let answerMessageId: UUID?

    var isActive: Bool { status == "queued" || status == "running" }
}

struct Citation: Decodable, Identifiable, Hashable {
    let n: Int
    let entryId: UUID
    let revision: Int
    let chunkId: UUID
    let title: String
    let type: EntryKind
    let createdAt: Date
    let snippet: String
    let stale: Bool

    var id: Int { n }
}

struct ChatMessage: Decodable, Identifiable, Hashable {
    let id: UUID
    let role: String
    let content: String
    let insufficient: Bool
    let replyTo: UUID?
    let run: AnswerRun?
    let citations: [Citation]
    let createdAt: Date

    var isQuestion: Bool { role == "user" }
}

private struct AnswerAccepted: Decodable {
    let message: ChatMessage
    let runId: UUID
}

/// The 问答 section of a project: start a question or reopen a conversation.
struct ProjectConversationsSection: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    @State private var conversations: [Conversation] = []

    var body: some View {
        Section("问答") {
            NavigationLink {
                ConversationView(session: session, capture: capture, project: project, conversation: nil)
            } label: {
                Label("向项目提问", systemImage: "bubble.left.and.text.bubble.right")
            }
            ForEach(conversations) { conversation in
                NavigationLink {
                    ConversationView(session: session, capture: capture, project: project, conversation: conversation)
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(conversation.title.isEmpty ? "未命名对话" : conversation.title).lineLimit(1)
                        Text(conversation.updatedAt.formatted(date: .abbreviated, time: .shortened))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .task { await load() }
    }

    private func load() async {
        let page = try? await session.request(
            Page<Conversation>.self, "v1/projects/\(project.id.uuidString)/conversations",
            query: [URLQueryItem(name: "limit", value: "10")], scope: capture.scope
        )
        if let page { conversations = page.items }
    }
}

/// Questions and source-grounded answers within one project.
struct ConversationView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    @State var conversation: Conversation?
    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var pendingMessageID: UUID?
    @State private var isSending = false
    @State private var errorMessage: String?
    @State private var selectedCitation: Citation?
    @State private var pollGeneration = 0

    private struct Ask: Encodable {
        let clientMessageId: UUID
        let text: String
    }

    private struct NewConversation: Encodable {}

    private var trimmedDraft: String { draft.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var isWaiting: Bool {
        messages.contains { message in
            message.isQuestion && (message.run?.isActive ?? false) && answer(to: message) == nil
        }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if messages.isEmpty && !isSending {
                        ContentUnavailableView(
                            "向「\(project.name)」提问", systemImage: "text.bubble",
                            description: Text("只根据这个项目里的资料回答，并标注来源。")
                        )
                    }
                    ForEach(messages) { message in
                        if message.isQuestion {
                            QuestionBubble(message: message, answered: answer(to: message) != nil) {
                                Task { await retry(message.run) }
                            }
                        } else {
                            AnswerBubble(message: message) { selectedCitation = $0 }
                        }
                    }
                    if isWaiting {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("正在查找资料并回答…").foregroundStyle(.secondary)
                        }
                    }
                    if let errorMessage {
                        Text(errorMessage).foregroundStyle(.red).font(.footnote)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding()
            }
            .onChange(of: messages.count) { _, _ in
                withAnimation { proxy.scrollTo("bottom") }
            }
        }
        .safeAreaInset(edge: .bottom) { composer }
        .navigationTitle(conversation.map { $0.title.isEmpty ? "问答" : $0.title } ?? "新问题")
        .navigationBarTitleDisplayMode(.inline)
        .task { await reload() }
        .task(id: pollGeneration) { await poll() }
        .refreshable { await reload() }
        .sheet(item: $selectedCitation) { citation in
            CitationSheet(session: session, capture: capture, citation: citation)
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("向这个项目提问", text: $draft, axis: .vertical)
                .lineLimit(1...5)
                .textFieldStyle(.roundedBorder)
                .disabled(isSending)
            Button {
                Task { await send() }
            } label: {
                Image(systemName: "arrow.up.circle.fill").font(.title2)
            }
            .disabled(trimmedDraft.isEmpty || isSending || isWaiting)
            .accessibilityLabel("发送")
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private func answer(to question: ChatMessage) -> ChatMessage? {
        messages.first { $0.replyTo == question.id }
    }

    private func reload() async {
        guard let conversation else { return }
        do {
            messages = try await session.request(
                [ChatMessage].self, "v1/conversations/\(conversation.id.uuidString)/messages",
                scope: capture.scope
            )
        } catch {
            if !(error is CancellationError) { errorMessage = error.localizedDescription }
        }
    }

    /// Reloads every 2 seconds while a question is still being answered.
    private func poll() async {
        while !Task.isCancelled && isWaiting {
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled else { return }
            await reload()
        }
    }

    private func send() async {
        let text = trimmedDraft
        guard !text.isEmpty else { return }
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            if conversation == nil {
                conversation = try await session.request(
                    Conversation.self, "v1/projects/\(project.id.uuidString)/conversations",
                    method: "POST", body: NewConversation(), scope: capture.scope
                )
            }
            guard let conversation else { return }
            // Reused if this send fails, so a retry cannot ask twice.
            let clientID = pendingMessageID ?? UUID()
            pendingMessageID = clientID
            let accepted = try await session.request(
                AnswerAccepted.self, "v1/conversations/\(conversation.id.uuidString)/messages",
                method: "POST", body: Ask(clientMessageId: clientID, text: text), scope: capture.scope
            )
            pendingMessageID = nil
            draft = ""
            if !messages.contains(where: { $0.id == accepted.message.id }) {
                messages.append(accepted.message)
            }
            pollGeneration += 1
        } catch {
            errorMessage = "提问没有发送成功，可以再试一次。\(error.localizedDescription)"
        }
    }

    private func retry(_ run: AnswerRun?) async {
        guard let run else { return }
        do {
            _ = try await session.send("v1/runs/\(run.id.uuidString)/retry", method: "POST", scope: capture.scope)
            await reload()
            pollGeneration += 1
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct QuestionBubble: View {
    let message: ChatMessage
    let answered: Bool
    let onRetry: () -> Void

    var body: some View {
        VStack(alignment: .trailing, spacing: 6) {
            Text(message.content)
                .padding(12)
                .background(Color.accentColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 16))
                .textSelection(.enabled)
            if !answered, let run = message.run, run.status == "failed" {
                HStack {
                    Text("回答失败").foregroundStyle(.red)
                    if run.canRetry { Button("重试", action: onRetry) }
                }
                .font(.footnote)
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

private struct AnswerBubble: View {
    let message: ChatMessage
    let onCitation: (Citation) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if message.insufficient {
                Label("资料不足", systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(answerText(message.content, cited: Set(message.citations.map(\.n))))
                .textSelection(.enabled)
                .environment(\.openURL, OpenURLAction { url in
                    guard url.scheme == "gatherloom-citation", let n = Int(url.host() ?? ""),
                          let citation = message.citations.first(where: { $0.n == n }) else {
                        return .systemAction
                    }
                    onCitation(citation)
                    return .handled
                })
            if !message.citations.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(message.citations) { citation in
                        Button {
                            onCitation(citation)
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text("[\(citation.n)]").monospacedDigit()
                                Image(systemName: citation.type.symbol)
                                Text(citation.title.isEmpty ? citation.type.title : citation.title).lineLimit(1)
                                if citation.stale {
                                    Text("已变化").foregroundStyle(.orange)
                                }
                            }
                            .font(.caption)
                        }
                        .buttonStyle(.borderless)
                    }
                }
            }
        }
        .padding(12)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Turns [n] markers into tappable links for numbers that have a citation.
func answerText(_ content: String, cited: Set<Int>) -> AttributedString {
    var result = AttributedString()
    var rest = content[...]
    while let match = rest.firstMatch(of: /\[(\d+)\]/) {
        result += AttributedString(String(rest[..<match.range.lowerBound]))
        var marker = AttributedString(String(rest[match.range]))
        if let n = Int(match.1), cited.contains(n) {
            marker.link = URL(string: "gatherloom-citation://\(n)")
            marker.inlinePresentationIntent = .stronglyEmphasized
        }
        result += marker
        rest = rest[match.range.upperBound...]
    }
    result += AttributedString(String(rest))
    return result
}

private struct CitationSheet: View {
    let session: AccountSession
    let capture: CaptureQueue
    let citation: Citation
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(citation.title.isEmpty ? citation.type.title : citation.title).font(.headline)
                    LabeledContent("类型", value: citation.type.title)
                    LabeledContent("记录时间", value: citation.createdAt.formatted(date: .abbreviated, time: .shortened))
                    if citation.stale {
                        Text("这条资料在回答之后被修改、删除或移出了项目，下面是回答时引用的原文。")
                            .font(.footnote)
                            .foregroundStyle(.orange)
                    }
                }
                Section("引用的原文") {
                    Text(citation.snippet).textSelection(.enabled)
                }
                Section {
                    NavigationLink("打开资料") {
                        CitedEntryView(session: session, capture: capture, entryID: citation.entryId)
                    }
                }
            }
            .navigationTitle("引用 [\(citation.n)]")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("完成") { dismiss() } }
        }
    }
}

private struct CitedEntryView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let entryID: UUID
    @State private var entry: CaptureEntry?
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if let entry {
                EntryDetailView(capture: capture, entry: entry)
            } else if let errorMessage {
                ContentUnavailableView("无法打开资料", systemImage: "doc.questionmark", description: Text(errorMessage))
            } else {
                ProgressView()
            }
        }
        .task {
            do {
                entry = try await session.request(CaptureEntry.self, "v1/entries/\(entryID.uuidString)", scope: capture.scope)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}
