import SwiftUI

struct ProbeHomeView: View {
    @State private var device: DeviceInfo?
    @State private var exportURL: URL?
    @State private var exportError = ""

    var body: some View {
        NavigationStack {
            List {
                Section("环境") {
                    if let device {
                        LabeledContent("设备", value: device.model)
                        LabeledContent("系统", value: "\(device.systemName) \(device.systemVersion)")
                        LabeledContent("Bundle ID", value: device.bundleID)
                        LabeledContent("签名能力", value: device.entitlementsVariant)
                        LabeledContent("App Group", value: device.appGroupID)
                        LabeledContent("Keychain 组后缀", value: device.keychainGroupSuffix)
                    }
                }

                Section("签名与共享（ADR 0008）") {
                    NavigationLink("App Group 共享目录") { AppGroupProbeView() }
                    NavigationLink("Keychain 共享访问组") { KeychainProbeView() }
                }

                Section("端侧能力（ADR 0003、0004）") {
                    NavigationLink("截图 OCR（Vision）") { OCRProbeView() }
                    NavigationLink("分享扩展记录") { ShareRecordsView() }
                }

                Section("网络") {
                    NavigationLink("HTTPS 请求服务器") { HealthCheckView() }
                }

                Section {
                    Button("生成导出文件") { export() }
                    if let exportURL {
                        ShareLink(item: exportURL) {
                            Label(exportURL.lastPathComponent, systemImage: "square.and.arrow.up")
                        }
                    }
                    if !exportError.isEmpty {
                        Text(exportError).foregroundStyle(.red)
                    }
                } header: {
                    Text("导出结果")
                } footer: {
                    Text("导出设备信息、OCR 结果与分享扩展记录为一个 JSON。OCR 用图另存于“文件”App → 我的 iPhone → Gatherloom → S0。")
                }
            }
            .navigationTitle("Gatherloom S0")
            .task { device = DeviceInfo.current() }
        }
    }

    private func export() {
        do {
            exportURL = try ProbeExporter.export(device: DeviceInfo.current())
            exportError = ""
        } catch {
            exportError = error.localizedDescription
        }
    }
}
