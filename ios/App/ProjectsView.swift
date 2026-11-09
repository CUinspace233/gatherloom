import SwiftUI

struct Project: Codable, Identifiable {
    let id: UUID
    let name: String
    let goal: String
    let status: Status

    enum Status: String, Codable, CaseIterable {
        case active, archived

        var title: String { self == .active ? "进行中" : "已归档" }
    }
}

struct Question: Decodable, Identifiable {
    let id: UUID
    let text: String
    let status: Status

    enum Status: String, Codable, CaseIterable {
        case open, resolved

        var title: String { self == .open ? "待解决" : "已解决" }
    }
}

struct Page<Item: Decodable>: Decodable {
    let items: [Item]
    let nextCursor: String?
}

struct ProjectsView: View {
    let session: AccountSession
    let capture: CaptureQueue
    @State private var projects: [Project] = []
    @State private var nextCursor: String?
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var showsEditor = false

    var body: some View {
        NavigationStack {
            List {
                ForEach(projects) { project in
                    NavigationLink {
                        ProjectDetailView(session: session, capture: capture, project: project, onSaved: save)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(project.name)
                            Text(project.status.title).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                if let nextCursor {
                    Button("加载更多") { Task { await load(after: nextCursor) } }
                        .disabled(isLoading)
                }
                if isLoading {
                    ProgressView()
                } else if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                    Button("重试") { Task { await load() } }
                } else if projects.isEmpty {
                    ContentUnavailableView("还没有项目", systemImage: "folder", description: Text("点加号创建第一个项目。"))
                }
            }
            .navigationTitle("项目")
            .toolbar {
                Button("新建项目", systemImage: "plus") { showsEditor = true }
                    .disabled(isLoading)
            }
            .sheet(isPresented: $showsEditor) {
                ProjectEditor(session: session, onSaved: save)
            }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func save(_ project: Project) {
        if let index = projects.firstIndex(where: { $0.id == project.id }) {
            projects[index] = project
        } else {
            projects.insert(project, at: 0)
        }
    }

    private func load(after cursor: String? = nil) async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let query = cursor.map { [URLQueryItem(name: "cursor", value: $0)] } ?? []
            let page = try await session.request(Page<Project>.self, "v1/projects", query: query)
            if cursor == nil {
                projects = page.items
            } else {
                projects.append(contentsOf: page.items)
            }
            nextCursor = page.nextCursor
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct ProjectDetailView: View {
    let session: AccountSession
    let capture: CaptureQueue
    @State var project: Project
    let onSaved: (Project) -> Void
    @State private var questions: [Question] = []
    @State private var nextCursor: String?
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var showsProjectEditor = false
    @State private var showsQuestionEditor = false
    @State private var selectedQuestion: Question?

    var body: some View {
        List {
            Section("项目目标") {
                Text(project.goal.isEmpty ? "还没有设置目标" : project.goal)
                LabeledContent("状态", value: project.status.title)
            }
            Section("记录") {
                NavigationLink("查看项目记录") {
                    CaptureEntriesView(session: session, capture: capture, project: project)
                }
            }
            Section("问题") {
                ForEach(questions) { question in
                    Button {
                        selectedQuestion = question
                        showsQuestionEditor = true
                    } label: {
                        LabeledContent(question.text, value: question.status.title)
                    }
                    .tint(.primary)
                    .disabled(isLoading)
                }
                if let nextCursor {
                    Button("加载更多") { Task { await load(after: nextCursor) } }
                        .disabled(isLoading)
                }
                if isLoading {
                    ProgressView()
                } else if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                    Button("重试") { Task { await load() } }
                }
                Button("添加问题", systemImage: "plus") {
                    selectedQuestion = nil
                    showsQuestionEditor = true
                }
                .disabled(isLoading)
            }
        }
        .navigationTitle(project.name)
        .toolbar {
            Button("编辑项目") { showsProjectEditor = true }.disabled(isLoading)
        }
        .sheet(isPresented: $showsProjectEditor) {
            ProjectEditor(session: session, project: project) { saved in
                project = saved
                onSaved(saved)
            }
        }
        .sheet(isPresented: $showsQuestionEditor) {
            QuestionEditor(session: session, project: project, question: selectedQuestion, onSaved: save)
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func save(_ question: Question) {
        if let index = questions.firstIndex(where: { $0.id == question.id }) {
            questions[index] = question
        } else {
            questions.insert(question, at: 0)
        }
    }

    private func load(after cursor: String? = nil) async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let path = "v1/projects/\(project.id)"
            if cursor == nil {
                project = try await session.request(Project.self, path)
                onSaved(project)
            }
            let query = cursor.map { [URLQueryItem(name: "cursor", value: $0)] } ?? []
            let page = try await session.request(Page<Question>.self, "\(path)/questions", query: query)
            if cursor == nil {
                questions = page.items
            } else {
                questions.append(contentsOf: page.items)
            }
            nextCursor = page.nextCursor
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct ProjectEditor: View {
    let session: AccountSession
    let project: Project?
    let onSaved: (Project) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name: String
    @State private var goal: String
    @State private var status: Project.Status
    @State private var isSaving = false
    @State private var errorMessage: String?

    init(session: AccountSession, project: Project? = nil, onSaved: @escaping (Project) -> Void) {
        self.session = session
        self.project = project
        self.onSaved = onSaved
        _name = State(initialValue: project?.name ?? "")
        _goal = State(initialValue: project?.goal ?? "")
        _status = State(initialValue: project?.status ?? .active)
    }

    var body: some View {
        NavigationStack {
            Form {
                TextField("项目名称", text: $name)
                TextField("项目目标", text: $goal, axis: .vertical)
                if project != nil {
                    Picker("状态", selection: $status) {
                        ForEach(Project.Status.allCases, id: \.self) { Text($0.title).tag($0) }
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .disabled(isSaving)
            .navigationTitle(project == nil ? "新建项目" : "编辑项目")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(isSaving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await save() } }
                        .disabled(isSaving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .interactiveDismissDisabled(isSaving)
        }
    }

    private func save() async {
        guard !isSaving else { return }
        isSaving = true
        defer { isSaving = false }
        do {
            let path = project.map { "v1/projects/\($0.id)" } ?? "v1/projects"
            let body = Payload(name: name, goal: goal, status: project == nil ? nil : status)
            let saved = try await session.request(Project.self, path, method: project == nil ? "POST" : "PATCH", body: body)
            onSaved(saved)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private struct Payload: Encodable {
        let name: String
        let goal: String
        let status: Project.Status?
    }
}

private struct QuestionEditor: View {
    let session: AccountSession
    let project: Project
    let question: Question?
    let onSaved: (Question) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @State private var status: Question.Status
    @State private var isSaving = false
    @State private var errorMessage: String?

    init(session: AccountSession, project: Project, question: Question?, onSaved: @escaping (Question) -> Void) {
        self.session = session
        self.project = project
        self.question = question
        self.onSaved = onSaved
        _text = State(initialValue: question?.text ?? "")
        _status = State(initialValue: question?.status ?? .open)
    }

    var body: some View {
        NavigationStack {
            Form {
                TextField("问题", text: $text, axis: .vertical)
                if question != nil {
                    Picker("状态", selection: $status) {
                        ForEach(Question.Status.allCases, id: \.self) { Text($0.title).tag($0) }
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .disabled(isSaving)
            .navigationTitle(question == nil ? "添加问题" : "编辑问题")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(isSaving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await save() } }
                        .disabled(isSaving || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .interactiveDismissDisabled(isSaving)
        }
    }

    private func save() async {
        guard !isSaving else { return }
        isSaving = true
        defer { isSaving = false }
        do {
            let path = question.map { "v1/questions/\($0.id)" } ?? "v1/projects/\(project.id)/questions"
            let body = Payload(text: text, status: question == nil ? nil : status)
            let saved = try await session.request(Question.self, path, method: question == nil ? "POST" : "PATCH", body: body)
            onSaved(saved)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private struct Payload: Encodable {
        let text: String
        let status: Question.Status?
    }
}
