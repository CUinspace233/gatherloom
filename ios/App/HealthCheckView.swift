import SwiftUI

/// 真机通过 HTTPS 请求服务器（S0 出口：真机可请求 HTTPS）。
/// 默认 ATS 只允许 HTTPS 且证书有效，这正是要验证的条件，因此不放宽 ATS。
struct HealthCheckView: View {
    @AppStorage("s0.healthURL") private var urlString = "https://"
    @State private var log: [String] = []
    @State private var isRunning = false

    var body: some View {
        List {
            Section {
                TextField("https://api.example.com/healthz", text: $urlString)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button(isRunning ? "请求中…" : "请求") { Task { await check() } }
                    .disabled(isRunning)
            } footer: {
                Text("分别在 Wi‑Fi 和蜂窝网络下各测一次。错误码 -1022 表示被 ATS 拦截（非 HTTPS），-1202 表示证书不受信任。")
            }
            Section("结果") {
                ForEach(Array(log.enumerated()), id: \.offset) { _, line in
                    Text(line).font(.footnote).textSelection(.enabled)
                }
            }
        }
        .navigationTitle("HTTPS")
    }

    private func check() async {
        guard let url = URL(string: urlString), url.scheme == "https" else {
            log.insert("✗ 请输入 https:// 开头的地址", at: 0)
            return
        }
        isRunning = true
        defer { isRunning = false }

        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let started = Date()
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            let status = (response as? HTTPURLResponse)?.statusCode ?? -1
            let body = String(decoding: data.prefix(300), as: UTF8.self)
            log.insert("✓ \(Date().formatted(date: .omitted, time: .standard)) HTTP \(status) · \(ms) ms\n\(body)", at: 0)
        } catch {
            let error = error as NSError
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            log.insert("✗ \(error.domain) \(error.code) · \(ms) ms\n\(error.localizedDescription)", at: 0)
        }
    }
}
