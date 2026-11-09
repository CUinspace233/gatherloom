#if DEBUG
import SwiftUI

struct DebugConnectionView: View {
    @AppStorage("GatherloomDebugServerURL") private var serverAddress = ""
    @Environment(\.dismiss) private var dismiss
    @State private var isTesting = false
    @State private var result: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("服务地址") {
                    TextField("http://192.168.1.10:8000", text: $serverAddress)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .disabled(isTesting)
                    Button("测试连接") {
                        Task { await checkConnection() }
                    }
                    .disabled(isTesting)
                }
                if isTesting {
                    ProgressView("正在连接")
                }
                if let result {
                    Text(result)
                }
            }
            .navigationTitle("服务连接")
            .toolbar {
                Button("完成") { dismiss() }
            }
            .task {
                if ProcessInfo.processInfo.arguments.contains("--check-health") {
                    await checkConnection()
                }
            }
        }
    }

    private func checkConnection() async {
        guard !isTesting else { return }
        let address = serverAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: address),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              let url = components.url else {
            showResult("请输入完整的 HTTP 或 HTTPS 服务地址。")
            return
        }

        isTesting = true
        result = nil
        defer { isTesting = false }

        let configuration = URLSessionConfiguration.ephemeral
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForResource = 12
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }

        do {
            let (data, response) = try await session.data(from: url.appendingPathComponent("healthz"))
            guard let response = response as? HTTPURLResponse else {
                showResult("连接失败。服务没有返回 HTTP 响应。")
                return
            }
            guard response.statusCode == 200 else {
                showResult("连接失败。服务返回 HTTP \(response.statusCode)。")
                return
            }
            guard (try? JSONDecoder().decode(HealthStatus.self, from: data))?.status == "ok" else {
                showResult("连接失败。服务返回了意外的健康状态。")
                return
            }
            showResult("连接成功 · HTTP 200 · status=ok")
        } catch {
            showResult("连接失败。\(error.localizedDescription)")
        }
    }

    private func showResult(_ message: String) {
        result = message
        print("Gatherloom /healthz: \(message)")
    }

    private struct HealthStatus: Decodable {
        let status: String
    }
}
#endif
