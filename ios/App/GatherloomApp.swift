import SwiftUI

@main
struct GatherloomApp: App {
    @State private var session = AccountSession()

    var body: some Scene {
        WindowGroup {
            Group {
                if session.isRestoring {
                    ProgressView("正在读取登录状态")
                } else if let scope = session.captureScope {
                    SignedInShell(session: session, scope: scope)
                        .id(scope)
                } else {
                    SignInView(session: session)
                }
            }
            .environment(session)
            .task { session.restore() }
        }
    }
}

enum AppSection: Hashable {
    case home, inbox, projects, search
}

private struct OpenDrawerKey: EnvironmentKey {
    static let defaultValue: () -> Void = {}
}

private struct OpenHomeKey: EnvironmentKey {
    static let defaultValue: () -> Void = {}
}

extension EnvironmentValues {
    var openDrawer: () -> Void {
        get { self[OpenDrawerKey.self] }
        set { self[OpenDrawerKey.self] = newValue }
    }

    /// Returns to the capture home screen from any section.
    var openHome: () -> Void {
        get { self[OpenHomeKey.self] }
        set { self[OpenHomeKey.self] = newValue }
    }
}

private struct DrawerButton: ViewModifier {
    @Environment(\.openDrawer) private var openDrawer

    func body(content: Content) -> some View {
        content.toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button("菜单", systemImage: "line.3.horizontal", action: openDrawer)
            }
        }
    }
}

extension View {
    /// Adds the leading button that opens the side drawer. For root screens only.
    func drawerButton() -> some View { modifier(DrawerButton()) }
}

/// The signed-in app: a capture home screen, with everything else behind a side drawer.
private struct SignedInShell: View {
    let session: AccountSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var capture: CaptureQueue
    @State private var section = AppSection.home
    @State private var isDrawerOpen = false
    @State private var showsSettings = false
    @State private var projectToOpen: Project?

    init(session: AccountSession, scope: CaptureScope) {
        self.session = session
        _capture = State(initialValue: CaptureQueue(scope: scope, session: session))
    }

    var body: some View {
        GeometryReader { proxy in
            let width = min(proxy.size.width * 0.82, 340)
            ZStack(alignment: .leading) {
                DrawerView(
                    session: session,
                    capture: capture,
                    section: section,
                    onSection: { go(to: $0) },
                    onProject: { project in
                        projectToOpen = project
                        go(to: .projects)
                    },
                    onSettings: { showsSettings = true }
                )
                .frame(width: width)
                .accessibilityHidden(!isDrawerOpen)
                content
                    .overlay {
                        if isDrawerOpen {
                            RoundedRectangle(cornerRadius: 44)
                                .fill(Color.primary.opacity(0.06))
                                .strokeBorder(Color.primary.opacity(0.14))
                                .ignoresSafeArea()
                                .contentShape(.rect)
                                .onTapGesture { setDrawer(false) }
                                .gesture(DragGesture(minimumDistance: 20).onEnded { value in
                                    if value.translation.width < -40 { setDrawer(false) }
                                })
                                .accessibilityLabel("关闭菜单")
                                .accessibilityAddTraits(.isButton)
                        }
                    }
                    .offset(x: isDrawerOpen ? width : 0)
            }
        }
        .environment(\.openDrawer) { setDrawer(true) }
        .environment(\.openHome) { go(to: .home) }
        .onChange(of: scenePhase, initial: true) { _, _ in updateSync() }
        .onChange(of: session.isSigningOut) { _, _ in updateSync() }
        .onDisappear { capture.setActive(false) }
        .sheet(isPresented: $showsSettings) {
            AccountSettingsView(session: session, capture: capture)
        }
    }

    /// The home screen stays mounted so an unsaved draft survives a trip to another section.
    private var content: some View {
        ZStack {
            NavigationStack {
                CaptureView(capture: capture).drawerButton()
            }
            .opacity(section == .home ? 1 : 0)
            .allowsHitTesting(section == .home)
            .accessibilityHidden(section != .home)
            switch section {
            case .home:
                EmptyView()
            case .inbox:
                NavigationStack {
                    CaptureEntriesView(session: session, capture: capture).drawerButton()
                }
            case .projects:
                ProjectsView(session: session, capture: capture, projectToOpen: $projectToOpen)
            case .search:
                NavigationStack {
                    SearchView(session: session, capture: capture).drawerButton()
                }
            }
        }
        .background(Color.canvas)
    }

    private func go(to target: AppSection) {
        section = target
        setDrawer(false)
    }

    private func setDrawer(_ open: Bool) {
        if open {
            UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        }
        withAnimation(.snappy(duration: 0.3)) { isDrawerOpen = open }
    }

    private func updateSync() {
        capture.setActive(scenePhase == .active && !session.isSigningOut)
    }
}

private struct DrawerView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let section: AppSection
    let onSection: (AppSection) -> Void
    let onProject: (Project) -> Void
    let onSettings: () -> Void

    private var projects: [Project] {
        let recent = capture.recentProjects
        let others = capture.projects.filter { project in !recent.contains { $0.id == project.id } }
        return Array((recent + others).prefix(8))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Gatherloom").font(.system(.title, design: .serif, weight: .semibold))
                Spacer()
                Button("搜索", systemImage: "magnifyingglass") { onSection(.search) }
                    .labelStyle(.iconOnly)
                    .buttonStyle(.glass)
                    .buttonBorderShape(.circle)
                    .controlSize(.large)
            }
            .padding(.bottom, Theme.Space.l)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    row(String(localized: "收集箱"), "tray", selected: section == .inbox) { onSection(.inbox) }
                    row(String(localized: "项目"), "folder", selected: section == .projects) { onSection(.projects) }
                    if !projects.isEmpty {
                        Text("最近项目")
                            .sectionLabel()
                            .padding(.top, Theme.Space.xl)
                            .padding(.bottom, Theme.Space.xs)
                        ForEach(projects) { project in
                            row(project.name, "folder", selected: false) { onProject(project) }
                        }
                    }
                }
            }
            .scrollIndicators(.hidden)
            HStack {
                Button(action: onSettings) {
                    Text(String(session.account?.username.prefix(1) ?? "").uppercased())
                        .font(.headline)
                        .frame(width: 20, height: 20)
                }
                .buttonStyle(.glass)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                .accessibilityLabel("设置")
                Spacer()
                Button("新记录", systemImage: "plus") { onSection(.home) }
                    .primaryAction()
            }
            .padding(.top, Theme.Space.m)
        }
        .padding(.horizontal, Theme.Space.xl)
        .padding(.top, Theme.Space.s)
        .padding(.bottom, Theme.Space.s)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Color(.systemBackground))
    }

    private func row(_ title: String, _ symbol: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: Theme.Space.l) {
                Image(systemName: symbol).frame(width: 24)
                Text(title).lineLimit(1)
                Spacer(minLength: 0)
            }
            .font(.body.weight(selected ? .semibold : .regular))
            .padding(.vertical, 13)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }
}

private struct AccountSettingsView: View {
    let session: AccountSession
    let capture: CaptureQueue
    @Environment(\.dismiss) private var dismiss
    @State private var errorMessage: String?
    @State private var confirmsDiscard = false

    private var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
    }

    var body: some View {
        NavigationStack {
            Form {
                if let account = session.account {
                    Section {
                        HStack(spacing: Theme.Space.l) {
                            Text(String(account.username.prefix(1)).uppercased())
                                .font(.headline)
                                .frame(width: 44, height: 44)
                                .background(Color.primary.opacity(0.1), in: .circle)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(account.username).font(.title3)
                                Text("Gatherloom 账号").font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, Theme.Space.xs)
                    }
                    Section("账号") {
                        LabeledContent {
                            Text(account.timezone)
                        } label: {
                            Label("时区", systemImage: "globe")
                        }
                    }
                }
                Section {
                    LabeledContent {
                        Text(capture.pending.isEmpty ? String(localized: "已全部同步") : String(localized: "\(capture.pending.count) 条未同步"))
                    } label: {
                        Label("同步", systemImage: "arrow.triangle.2.circlepath")
                    }
                    if !capture.pending.isEmpty {
                        Button(role: .destructive) {
                            confirmsDiscard = true
                        } label: {
                            Label("删除本机未同步记录", systemImage: "trash")
                        }
                        .confirmationDialog(
                            "删除 \(capture.pending.count) 条未同步记录？",
                            isPresented: $confirmsDiscard,
                            titleVisibility: .visible
                        ) {
                            Button("删除本机副本", role: .destructive) { Task { await capture.discardPending() } }
                        } message: {
                            Text("这些记录还没有上传到服务器，删除后无法恢复。")
                        }
                    }
                } header: {
                    Text("数据")
                } footer: {
                    if !capture.pending.isEmpty {
                        Text("退出后未同步的记录会保存在本机，下次登录同一账号时继续上传，不会提交到其他账号。")
                    }
                }
                Section("关于") {
                    LabeledContent {
                        Text(version)
                    } label: {
                        Label("版本", systemImage: "info.circle")
                    }
                }
                Section {
                    Button(role: .destructive) {
                        Task {
                            do {
                                try await session.signOut()
                                dismiss()
                            } catch {
                                errorMessage = error.localizedDescription
                            }
                        }
                    } label: {
                        Label("退出登录", systemImage: "rectangle.portrait.and.arrow.right")
                            .foregroundStyle(.red)
                    }
                    .disabled(session.isSigningOut)
                } footer: {
                    if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
                }
            }
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("关闭", systemImage: "xmark") { dismiss() }.disabled(session.isSigningOut)
                }
            }
            .interactiveDismissDisabled(session.isSigningOut)
        }
    }
}
