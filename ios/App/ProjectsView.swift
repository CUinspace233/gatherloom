import SwiftUI

struct Question: Decodable, Identifiable {
    let id: UUID
    let text: String
    let status: Status

    enum Status: String, Codable, CaseIterable {
        case open, resolved

        var title: String { self == .open ? String(localized: "待解决") : String(localized: "已解决") }
    }
}

private struct ProjectRow: View {
    let project: Project

    private var subtitle: String {
        let goal = project.goal.trimmingCharacters(in: .whitespacesAndNewlines)
        return goal.isEmpty ? project.status.title : "\(goal) · \(project.status.title)"
    }

    var body: some View {
        HStack(spacing: Theme.Space.l) {
            Image(systemName: "folder")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(width: 36, height: 36)
                .background(Color.primary.opacity(0.06), in: .rect(cornerRadius: 10))
            VStack(alignment: .leading, spacing: 2) {
                Text(project.name).font(.body.weight(.medium)).lineLimit(1)
                Text(subtitle).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, Theme.Space.m)
        .contentShape(.rect)
    }
}

/// A rounded, softly outlined container for a text field on a sheet.
private struct SoftField: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(Theme.Space.l)
            .background(Color.primary.opacity(0.04), in: .rect(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Color.primary.opacity(0.1)))
    }
}

struct ProjectsView: View {
    let session: AccountSession
    let capture: CaptureQueue
    @State private var projects: [Project] = []
    @State private var nextCursor: String?
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var showsEditor = false
    @State private var openProjectID: UUID?
    @State private var selectedProject: Project?
    @State private var isSearching = false
    @State private var query = ""
    /// Nil shows every project; otherwise the server returns only that status.
    @State private var statusFilter: Project.Status?
    @FocusState private var searchFocused: Bool
    /// A project picked in the drawer, opened as soon as this screen shows.
    @Binding var projectToOpen: Project?

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(visibleProjects) { project in
                        SwipeToDeleteCard(
                            isOpen: openProjectID == project.id,
                            onOpenChange: { openProjectID = $0 ? project.id : nil },
                            onTap: { openProjectID = nil; selectedProject = project },
                            onDelete: { Task { await delete(project) } },
                            showsCard: false
                        ) {
                            ProjectRow(project: project)
                        }
                    }
                    if let nextCursor {
                        Button("加载更多") { Task { await load(after: nextCursor) } }
                            .disabled(isLoading)
                            .padding(.vertical, 8)
                    }
                    if isLoading {
                        ProgressView().padding()
                    } else if let errorMessage {
                        Text(errorMessage).foregroundStyle(.red)
                        Button("重试") { Task { await load() } }
                    } else if projects.isEmpty, let statusFilter {
                        ContentUnavailableView(
                            statusFilter == .active ? "没有进行中的项目" : "没有已归档的项目",
                            systemImage: "folder"
                        )
                    } else if projects.isEmpty {
                        ContentUnavailableView("还没有项目", systemImage: "folder", description: Text("点「新建项目」创建第一个项目。"))
                    } else if visibleProjects.isEmpty {
                        ContentUnavailableView.search(text: trimmedQuery)
                    }
                }
                .padding(.horizontal, Theme.Space.xl)
                .padding(.top, Theme.Space.s)
            }
            .scrollDismissesKeyboard(.immediately)
            .safeAreaInset(edge: .bottom) { bottomBar }
            .canvas()
            .navigationTitle("项目")
            .navigationBarTitleDisplayMode(.inline)
            .drawerButton()
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Picker("状态", selection: $statusFilter) {
                            Text("全部项目").tag(Project.Status?.none)
                            ForEach(Project.Status.allCases, id: \.self) { status in
                                Text(status.title).tag(Project.Status?.some(status))
                            }
                        }
                    } label: {
                        Label("筛选", systemImage: statusFilter == nil
                            ? "line.3.horizontal.decrease"
                            : "line.3.horizontal.decrease.circle.fill")
                    }
                }
            }
            .onChange(of: statusFilter) { _, _ in
                projects = []
                nextCursor = nil
                Task { await load() }
            }
            .onChange(of: projectToOpen, initial: true) { _, project in
                guard let project else { return }
                selectedProject = project
                projectToOpen = nil
            }
            .navigationDestination(item: $selectedProject) { project in
                ProjectDetailView(session: session, capture: capture, project: project, onSaved: save)
            }
            .sheet(isPresented: $showsEditor) {
                ProjectEditor(session: session, onSaved: save)
            }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private var trimmedQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// Filters the projects already loaded; it does not query the server.
    private var visibleProjects: [Project] {
        guard isSearching, !trimmedQuery.isEmpty else { return projects }
        return projects.filter {
            $0.name.localizedCaseInsensitiveContains(trimmedQuery) || $0.goal.localizedCaseInsensitiveContains(trimmedQuery)
        }
    }

    private var bottomBar: some View {
        HStack(spacing: Theme.Space.m) {
            if isSearching {
                HStack(spacing: Theme.Space.s) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("搜索项目", text: $query)
                        .focused($searchFocused)
                        .submitLabel(.search)
                }
                .padding(.horizontal, Theme.Space.l)
                .frame(height: 50)
                .glassEffect(.regular, in: .capsule)
                Button("关闭", systemImage: "xmark") {
                    query = ""
                    isSearching = false
                }
                .labelStyle(.iconOnly)
                .buttonStyle(.glass)
                .buttonBorderShape(.circle)
                .controlSize(.large)
            } else {
                Button("搜索项目", systemImage: "magnifyingglass") {
                    isSearching = true
                    searchFocused = true
                }
                .labelStyle(.iconOnly)
                .buttonStyle(.glass)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                Spacer()
                Button("新建项目", systemImage: "plus") { showsEditor = true }
                    .primaryAction()
                    .disabled(isLoading)
            }
        }
        .padding(.horizontal, Theme.Space.xl)
        .padding(.bottom, Theme.Space.s)
    }

    private func delete(_ project: Project) async {
        do {
            _ = try await session.send("v1/projects/\(project.id.uuidString)", method: "DELETE")
            withAnimation { projects.removeAll { $0.id == project.id } }
            openProjectID = nil
            capture.forgetProject(project.id)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func save(_ project: Project) {
        if let statusFilter, project.status != statusFilter {
            projects.removeAll { $0.id == project.id }
        } else if let index = projects.firstIndex(where: { $0.id == project.id }) {
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
            var query = cursor.map { [URLQueryItem(name: "cursor", value: $0)] } ?? []
            if let statusFilter { query.append(URLQueryItem(name: "status", value: statusFilter.rawValue)) }
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
                Text(project.goal.isEmpty ? String(localized: "还没有设置目标") : project.goal)
                LabeledContent("状态", value: project.status.title)
            }
            ProjectSummarySection(session: session, capture: capture, project: project)
            Section("待解问题") {
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
                Button("添加待解问题", systemImage: "plus") {
                    selectedQuestion = nil
                    showsQuestionEditor = true
                }
                .disabled(isLoading)
            }
            ProjectDecisionsSection(session: session, capture: capture, project: project)
            ProjectReviewsSection(session: session, capture: capture, project: project)
            ProjectConversationsSection(session: session, capture: capture, project: project)
            Section("资料") {
                NavigationLink("全部资料") {
                    CaptureEntriesView(session: session, capture: capture, project: project)
                }
            }
        }
        .canvas(grouped: true)
        .navigationTitle(project.name)
        .onAppear { capture.markRecent(project.id) }
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
    @FocusState private var nameFocused: Bool

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
            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Space.m) {
                    prompt("你在做什么？")
                    TextField("给项目起个名字", text: $name)
                        .focused($nameFocused)
                        .modifier(SoftField())
                    prompt("想达成什么？").padding(.top, Theme.Space.s)
                    TextField("描述项目、目标、主题等…", text: $goal, axis: .vertical)
                        .lineLimit(5...12)
                        .modifier(SoftField())
                    if project != nil {
                        HStack {
                            Text("状态")
                            Spacer()
                            Picker("状态", selection: $status) {
                                ForEach(Project.Status.allCases, id: \.self) { Text($0.title).tag($0) }
                            }
                            .labelsHidden()
                        }
                        .padding(.leading, Theme.Space.l)
                        .padding(.vertical, Theme.Space.xs)
                        .background(Color.primary.opacity(0.04), in: .rect(cornerRadius: 22))
                        .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Color.primary.opacity(0.1)))
                        .padding(.top, Theme.Space.s)
                    }
                    if let errorMessage {
                        Text(errorMessage).font(.footnote).foregroundStyle(.red).padding(.horizontal, Theme.Space.l)
                    }
                }
                .padding(.horizontal, Theme.Space.l)
                .padding(.top, Theme.Space.s)
            }
            .scrollDismissesKeyboard(.interactively)
            .disabled(isSaving)
            .navigationTitle(project == nil ? String(localized: "新建项目") : String(localized: "编辑项目"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭", systemImage: "xmark") { dismiss() }.disabled(isSaving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存", systemImage: "checkmark") { Task { await save() } }
                        .disabled(isSaving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .onAppear { if project == nil { nameFocused = true } }
            .interactiveDismissDisabled(isSaving)
        }
    }

    private func prompt(_ title: LocalizedStringKey) -> some View {
        Text(title)
            .font(.subheadline.weight(.medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, Theme.Space.l)
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
                TextField("还需要弄清楚什么？", text: $text, axis: .vertical)
                if question != nil {
                    Picker("状态", selection: $status) {
                        ForEach(Question.Status.allCases, id: \.self) { Text($0.title).tag($0) }
                    }
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
                if let question {
                    QuestionRelationsSection(session: session, projectID: project.id, questionID: question.id)
                }
            }
            .disabled(isSaving)
            .navigationTitle(question == nil ? String(localized: "添加待解问题") : String(localized: "编辑待解问题"))
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
