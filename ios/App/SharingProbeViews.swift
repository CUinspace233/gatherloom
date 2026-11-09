import SwiftUI

struct AppGroupProbeView: View {
    @State private var log: [String] = []

    var body: some View {
        List {
            Section {
                Button("写入并读回") { roundTrip() }
            } footer: {
                Text("通过：能写入共享容器并读回相同内容。之后从 Safari 分享一个网页，回到“分享扩展记录”能看到扩展写入的记录，才算跨进程共享成功。")
            }
            Section("结果") {
                ForEach(Array(log.enumerated()), id: \.offset) { _, line in
                    Text(line).font(.footnote).textSelection(.enabled)
                }
            }
        }
        .navigationTitle("App Group")
    }

    private func roundTrip() {
        log.removeAll()
        log.append("App Group ID：\(SharedConfig.appGroupID)")
        do {
            let store = try AppGroupStore()
            log.append("容器：\(store.containerURL.path)")
            let payload = "probe \(Date().ISO8601Format())"
            let url = try store.write(Data(payload.utf8), to: store.probeDirectory, name: "roundtrip.txt")
            let readBack = try String(contentsOf: url, encoding: .utf8)
            log.append(readBack == payload ? "✓ 写入并读回一致" : "✗ 读回内容不一致")
            log.append("扩展记录数：\(store.loadShareRecords().count)")
        } catch {
            log.append("✗ \(error.localizedDescription)")
        }
    }
}

struct KeychainProbeView: View {
    @State private var value = "s0-" + UUID().uuidString.prefix(8).lowercased()
    @State private var log: [String] = []

    var body: some View {
        List {
            Section {
                TextField("探针值", text: $value)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("写入共享访问组") { run("写入") { try $0.save(value) } }
                Button("读取") {
                    run("读取") { store in
                        let v = try store.read()
                        log.append(v.map { "读到：\($0)" } ?? "条目不存在")
                    }
                }
                Button("删除", role: .destructive) { run("删除") { try $0.delete() } }
            } footer: {
                Text("通过：这里写入后，从任意 App 打开 Gatherloom 分享扩展，“Keychain 共享”一栏显示读取成功。")
            }
            Section("结果") {
                ForEach(Array(log.enumerated()), id: \.offset) { _, line in
                    Text(line).font(.footnote).textSelection(.enabled)
                }
            }
        }
        .navigationTitle("Keychain")
    }

    private func run(_ name: String, _ action: (KeychainStore) throws -> Void) {
        do {
            let store = try KeychainStore.shared()
            log.append("访问组：\(store.accessGroup)")
            try action(store)
            log.append("✓ \(name)完成")
        } catch {
            log.append("✗ \(name)：\(error.localizedDescription)")
        }
    }
}

struct ShareRecordsView: View {
    @State private var records: [ShareCaptureRecord] = []
    @State private var error = ""

    var body: some View {
        List {
            if !error.isEmpty {
                Text(error).foregroundStyle(.red)
            }
            if records.isEmpty && error.isEmpty {
                Text("还没有记录。在 Safari 或相册中点分享 → Gatherloom。")
                    .foregroundStyle(.secondary)
            }
            ForEach(records) { record in
                NavigationLink {
                    ShareRecordSummary(record: record).navigationTitle(record.kind)
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(record.title ?? record.url ?? record.kind).lineLimit(2)
                        Text(subtitle(record)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .navigationTitle("分享扩展记录")
        .toolbar {
            Button("清空", role: .destructive) {
                try? AppGroupStore().deleteShareRecords()
                reload()
            }
        }
        .refreshable { reload() }
        .onAppear { reload() }
    }

    private func subtitle(_ r: ShareCaptureRecord) -> String {
        var parts = [r.kind, String(format: "%.0f ms", r.elapsedMs)]
        if let e = r.preprocess?.extractor { parts.append(e) }
        if let n = r.preprocess?.textLength { parts.append("\(n) 字") }
        if let peak = r.memory?.peakMB { parts.append(String(format: "峰值 %.0f MB", peak)) }
        return parts.joined(separator: " · ")
    }

    private func reload() {
        do {
            records = try AppGroupStore().loadShareRecords()
            error = ""
        } catch {
            records = []
            self.error = error.localizedDescription
        }
    }
}
