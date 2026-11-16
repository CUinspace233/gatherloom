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

private struct DrawerGestureActions {
    var isEnabled = false
    var onBegan: () -> Void = {}
    var onChanged: (CGFloat) -> Void = { _ in }
    var onEnded: (CGFloat, CGFloat, Bool) -> Void = { _, _, _ in }
}

private struct DrawerGestureKey: EnvironmentKey {
    static let defaultValue = DrawerGestureActions()
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

    fileprivate var drawerGesture: DrawerGestureActions {
        get { self[DrawerGestureKey.self] }
        set { self[DrawerGestureKey.self] = newValue }
    }
}

private struct DrawerButton: ViewModifier {
    let swipeEnabled: Bool
    @Environment(\.openDrawer) private var openDrawer
    @Environment(\.drawerGesture) private var drawerGesture

    private var gestureActions: DrawerGestureActions {
        var actions = drawerGesture
        actions.isEnabled = actions.isEnabled && swipeEnabled
        return actions
    }

    func body(content: Content) -> some View {
        content
            .contentShape(.rect)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("菜单", systemImage: "line.3.horizontal", action: openDrawer)
                }
            }
            .gesture(DrawerPanGesture(actions: gestureActions, opensDrawer: true))
    }
}

extension View {
    /// Adds the menu button and full-width drawer pan. For root screens only,
    /// so detail screens keep the navigation stack's swipe-to-go-back gesture.
    func drawerButton(swipeEnabled: Bool = true) -> some View {
        modifier(DrawerButton(swipeEnabled: swipeEnabled))
    }
}

/// The settled state stays unchanged during a drag, so cancellation can restore it.
private struct DrawerPosition {
    var isOpen = false
    var translation: CGFloat?

    func offset(width: CGFloat) -> CGFloat {
        min(width, max(0, (isOpen ? width : 0) + (translation ?? 0)))
    }

    mutating func finish(width: CGFloat, velocity: CGFloat, cancelled: Bool) {
        guard translation != nil else { return }
        if !cancelled {
            if abs(velocity) > 550 {
                isOpen = velocity > 0
            } else {
                isOpen = offset(width: width) > width / 2
            }
        }
        translation = nil
    }
}

/// Rejects vertical and opposite-direction pans before they can claim scrolling
/// or row swipe actions. Once accepted, the direction stays locked until release.
private struct DrawerPanGesture: UIGestureRecognizerRepresentable {
    let actions: DrawerGestureActions
    let opensDrawer: Bool

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator {
        Coordinator(opensDrawer: opensDrawer)
    }

    func makeUIGestureRecognizer(context: Context) -> UIPanGestureRecognizer {
        let pan = UIPanGestureRecognizer()
        pan.maximumNumberOfTouches = 1
        pan.delegate = context.coordinator
        pan.isEnabled = actions.isEnabled
        return pan
    }

    func updateUIGestureRecognizer(_ pan: UIPanGestureRecognizer, context: Context) {
        pan.isEnabled = actions.isEnabled
    }

    func handleUIGestureRecognizerAction(_ pan: UIPanGestureRecognizer, context: Context) {
        // Window coordinates stay stable while the content itself moves.
        let translation = pan.translation(in: pan.view?.window).x
        let velocity = pan.velocity(in: pan.view?.window).x
        switch pan.state {
        case .began:
            actions.onBegan()
            actions.onChanged(translation)
        case .changed:
            actions.onChanged(translation)
        case .ended:
            actions.onEnded(translation, velocity, false)
        case .cancelled, .failed:
            actions.onEnded(translation, velocity, true)
        default:
            break
        }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        let opensDrawer: Bool

        init(opensDrawer: Bool) {
            self.opensDrawer = opensDrawer
        }

        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
            guard let pan = gestureRecognizer as? UIPanGestureRecognizer else { return false }
            let velocity = pan.velocity(in: pan.view?.window)
            return abs(velocity.x) > abs(velocity.y) * 1.5
                && (opensDrawer ? velocity.x > 0 : velocity.x < 0)
        }

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            true
        }
    }
}

/// The signed-in app: a capture home screen, with everything else behind a side drawer.
private struct SignedInShell: View {
    let session: AccountSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var capture: CaptureQueue
    @State private var section = AppSection.home
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drawer = DrawerPosition()
    @State private var showsSettings = false
    @State private var projectToOpen: Project?

    init(session: AccountSession, scope: CaptureScope) {
        self.session = session
        _capture = State(initialValue: CaptureQueue(scope: scope, session: session))
    }

    var body: some View {
        GeometryReader { proxy in
            let width = min(proxy.size.width * 0.82, 340)
            let offset = drawer.offset(width: width)
            let progress = offset / max(width, 1)
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
                    onSettings: { setDrawer(false); showsSettings = true }
                )
                .frame(width: width)
                .allowsHitTesting(drawer.isOpen)
                .accessibilityHidden(!drawer.isOpen)
                content
                    .allowsHitTesting(!drawer.isOpen)
                    .accessibilityHidden(drawer.isOpen)
                    .overlay {
                        RoundedRectangle(cornerRadius: 44)
                            .fill(Color.primary.opacity(0.06))
                            .strokeBorder(Color.primary.opacity(0.14))
                            .ignoresSafeArea()
                            .contentShape(.rect)
                            .opacity(progress)
                            .allowsHitTesting(drawer.isOpen)
                            .onTapGesture { setDrawer(false) }
                            .accessibilityHidden(!drawer.isOpen)
                            .accessibilityLabel("关闭菜单")
                            .accessibilityAddTraits(.isButton)
                    }
                    .offset(x: offset)
            }
            .environment(\.drawerGesture, gestureActions(width: width, opensDrawer: true))
            .gesture(DrawerPanGesture(
                actions: gestureActions(width: width, opensDrawer: false),
                opensDrawer: false
            ))
        }
        .environment(\.openDrawer) { setDrawer(true) }
        .environment(\.openHome) { go(to: .home) }
        .onChange(of: scenePhase, initial: true) { _, phase in
            if phase != .active { drawer.translation = nil }
            updateSync()
        }
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
        if open { dismissKeyboard() }
        withAnimation(drawerAnimation) {
            drawer.isOpen = open
            drawer.translation = nil
        }
    }

    private var drawerAnimation: Animation {
        reduceMotion ? .easeOut(duration: 0.2) : .snappy(duration: 0.3)
    }

    private func gestureActions(width: CGFloat, opensDrawer: Bool) -> DrawerGestureActions {
        DrawerGestureActions(
            isEnabled: scenePhase == .active && !showsSettings && drawer.isOpen != opensDrawer,
            onBegan: dismissKeyboard,
            onChanged: { translation in
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { drawer.translation = translation }
            },
            onEnded: { translation, velocity, cancelled in
                guard drawer.translation != nil else { return }
                // Include the final movement before choosing where to settle.
                drawer.translation = translation
                withAnimation(drawerAnimation) {
                    drawer.finish(width: width, velocity: velocity, cancelled: cancelled)
                }
            }
        )
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
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
