import SwiftUI
import UIKit

struct SignInView: View {
    private enum Field: Hashable {
        case server, username, password, invitation
    }

    @Bindable var session: AccountSession
    @FocusState private var focusedField: Field?

    @State private var username = ""
    @State private var password = ""
    @State private var invitation = ""
    @State private var isRegistering = false
    @State private var isSubmitting = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                #if DEBUG
                Section("服务地址") {
                    TextField("https://", text: $session.serverAddress)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focusedField, equals: .server)
                        .submitLabel(.next)
                        .onSubmit { focusedField = .username }
                    Button("使用正式服务器") { session.serverAddress = AppGroup.productionServer }
                        .disabled(session.serverAddress == AppGroup.productionServer)
                }
                #endif
                Section(isRegistering ? "创建账号" : "登录") {
                    TextField("用户名", text: $username)
                        .textContentType(.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focusedField, equals: .username)
                        .submitLabel(.next)
                        .onSubmit { focusedField = .password }
                    SecureField("密码", text: $password)
                        .textContentType(isRegistering ? nil : .password)
                        .focused($focusedField, equals: .password)
                        .submitLabel(isRegistering ? .next : .done)
                        .onSubmit { focusedField = isRegistering ? .invitation : nil }
                    if isRegistering {
                        Text("密码须为 8–128 个字符。")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                        TextField("邀请码", text: $invitation)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focusedField, equals: .invitation)
                            .submitLabel(.done)
                            .onSubmit { focusedField = nil }
                    }
                }
                if isRegistering {
                    Section("数据去向") {
                        Text("你保存的想法、链接正文和截图文字会存放在海外/港澳的 Gatherloom 服务器上，并发送给阿里云百炼（通义千问）和 DeepSeek，用于生成摘要、建立检索、回答问题和写总结与回顾。截图图片本身不会发送给模型服务。")
                        Text("删除资料后，它会立即从搜索和问答中移除，几分钟内从服务器清除；备份会保留最多 14 天，从备份恢复时会再次执行删除。")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
                if let message = errorMessage ?? session.notice {
                    Text(message).foregroundStyle(.red)
                }
                Section {
                    Button(isRegistering ? "注册并登录" : "登录") { Task { await submit() } }
                        .disabled(!canSubmit)
                    Button(isRegistering ? "已有账号，去登录" : "使用邀请码注册") {
                        focusedField = nil
                        isRegistering.toggle()
                        errorMessage = nil
                    }
                }
            }
            .scrollDismissesKeyboard(.immediately)
            .scrollBounceBehavior(.always)
            .background {
                OutsideFieldTap(isEnabled: focusedField != nil) { focusedField = nil }
                    .allowsHitTesting(false)
            }
            .disabled(isSubmitting)
            .navigationTitle("Gatherloom")
        }
    }

    private var canSubmit: Bool {
        !session.serverAddress.isEmpty && !username.isEmpty && (8...128).contains(password.unicodeScalars.count)
            && (!isRegistering || !invitation.isEmpty)
    }

    private func submit() async {
        guard !isSubmitting else { return }
        focusedField = nil
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        do {
            try await session.signIn(
                username: username,
                password: password,
                invitation: isRegistering ? invitation : nil
            )
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

/// Clears text focus when the user taps outside any text field.
struct OutsideFieldTap: UIViewRepresentable {
    let isEnabled: Bool
    let onTap: () -> Void

    func makeUIView(context: Context) -> TapSurface {
        TapSurface()
    }

    func updateUIView(_ view: TapSurface, context: Context) {
        view.onTap = onTap
        view.tap.isEnabled = isEnabled
    }

    final class TapSurface: UIView, UIGestureRecognizerDelegate {
        var onTap: (() -> Void)?
        private weak var listeningWindow: UIWindow?

        lazy var tap: UITapGestureRecognizer = {
            let recognizer = UITapGestureRecognizer(target: self, action: #selector(dismissKeyboard))
            recognizer.delegate = self
            recognizer.cancelsTouchesInView = false
            recognizer.delaysTouchesEnded = false
            return recognizer
        }()

        override func didMoveToWindow() {
            super.didMoveToWindow()
            guard listeningWindow !== window else { return }
            listeningWindow?.removeGestureRecognizer(tap)
            window?.addGestureRecognizer(tap)
            listeningWindow = window
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            guard bounds.contains(touch.location(in: self)) else { return false }
            var view = touch.view
            while let hitView = view {
                if hitView is UITextField || hitView is UITextView { return false }
                view = hitView.superview
            }
            return true
        }

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            true
        }

        @objc private func dismissKeyboard() {
            onTap?()
        }
    }
}
