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
                    SignedInTabs(session: session, scope: scope)
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

private struct SignedInTabs: View {
    let session: AccountSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var capture: CaptureQueue
    @State private var showsSettings = false

    init(session: AccountSession, scope: CaptureScope) {
        self.session = session
        _capture = State(initialValue: CaptureQueue(scope: scope, session: session))
    }

    var body: some View {
        TabView {
            NavigationStack {
                CaptureView(capture: capture)
                    .navigationTitle("记录")
                    .toolbar {
                        Button("账号设置", systemImage: "person.crop.circle") { showsSettings = true }
                    }
            }
            .tabItem { Label("记录", systemImage: "square.and.pencil") }
            NavigationStack {
                CaptureEntriesView(session: session, capture: capture)
            }
            .tabItem { Label("收集箱", systemImage: "tray") }
            ProjectsView(session: session, capture: capture)
                .tabItem { Label("项目", systemImage: "folder") }
            NavigationStack {
                ContentUnavailableView("搜索", systemImage: "magnifyingglass", description: Text("在这里查找收集过的资料。"))
                    .navigationTitle("搜索")
            }
            .tabItem { Label("搜索", systemImage: "magnifyingglass") }
        }
        .onChange(of: scenePhase, initial: true) { _, _ in updateSync() }
        .onChange(of: session.isSigningOut) { _, _ in updateSync() }
        .onDisappear { capture.setActive(false) }
        .sheet(isPresented: $showsSettings) {
            AccountSettingsView(session: session, capture: capture)
        }
    }

    private func updateSync() {
        capture.setActive(scenePhase == .active && !session.isSigningOut)
    }
}

private struct AccountSettingsView: View {
    let session: AccountSession
    let capture: CaptureQueue
    @Environment(\.dismiss) private var dismiss
    @State private var errorMessage: String?
    @State private var confirmsDiscard = false

    var body: some View {
        NavigationStack {
            Form {
                if let account = session.account {
                    LabeledContent("用户名", value: account.username)
                    LabeledContent("时区", value: account.timezone)
                    LabeledContent("服务地址", value: session.serverAddress)
                }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
                if !capture.pending.isEmpty {
                    Section {
                        Text("有 \(capture.pending.count) 条记录尚未同步。退出后会保存在本机，下次登录同一账号时继续上传，不会提交到其他账号。")
                            .foregroundStyle(.secondary)
                        Button("删除本机未同步记录", role: .destructive) { confirmsDiscard = true }
                    }
                }
                Button("退出登录", role: .destructive) {
                    Task {
                        do {
                            try await session.signOut()
                            dismiss()
                        } catch {
                            errorMessage = error.localizedDescription
                        }
                    }
                }
                .disabled(session.isSigningOut)
            }
            .navigationTitle("账号设置")
            .toolbar { Button("完成") { dismiss() }.disabled(session.isSigningOut) }
            .interactiveDismissDisabled(session.isSigningOut)
            .confirmationDialog(
                "删除 \(capture.pending.count) 条未同步记录？", isPresented: $confirmsDiscard, titleVisibility: .visible
            ) {
                Button("删除本机副本", role: .destructive) { Task { await capture.discardPending() } }
            } message: {
                Text("这些记录还没有上传到服务器，删除后无法恢复。")
            }
        }
    }
}
