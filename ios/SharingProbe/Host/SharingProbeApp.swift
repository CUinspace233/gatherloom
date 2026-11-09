import SwiftUI

@main
struct SharingProbeApp: App {
    @State private var appGroupResult = "待写入"
    @State private var keychainResult = "待写入"

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                Form {
                    Button("写入新测试码", action: writeProbe)
                    LabeledContent("App Group", value: appGroupResult)
                    LabeledContent("Keychain", value: keychainResult)
                    Text("写入后，在 Safari 分享任意网页，选 Gatherloom Probe。核对扩展读到的测试码。")
                }
                .navigationTitle("Gatherloom Probe")
                .task {
                    if ProcessInfo.processInfo.arguments.contains("--write-sharing-probe") {
                        writeProbe()
                    }
                }
            }
        }
    }

    private func writeProbe() {
        let code = String(UUID().uuidString.prefix(8))
        appGroupResult = SharingProbeStore.writeAppGroup(code)
        keychainResult = SharingProbeStore.writeKeychain(code)
        print("Gatherloom Probe write | App Group: \(appGroupResult) | Keychain: \(keychainResult)")
    }
}
