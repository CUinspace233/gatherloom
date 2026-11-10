import SwiftUI
import UIKit
import UniformTypeIdentifiers

final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let model = ShareModel(items: extensionContext?.inputItems as? [NSExtensionItem] ?? []) { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        }
        let host = UIHostingController(rootView: ShareView(model: model))
        addChild(host)
        view.addSubview(host.view)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        host.didMove(toParent: self)
    }
}

@MainActor
@Observable
final class ShareModel {
    enum Phase {
        case loading
        case ready
        case saving(String)
        case finished(String)
        case blocked(String)
    }

    struct Candidate: Identifiable {
        let id = UUID()
        let provider: NSItemProvider
        let thumbnail: UIImage
    }

    private(set) var phase = Phase.loading
    private(set) var kind = EntryKind.thought
    private(set) var text = ""
    private(set) var url: String?
    private(set) var extract: CaptureDraft.Extract?
    private(set) var images: [Candidate] = []
    private(set) var projects: [Project] = []
    var selectedImage: UUID?
    var note = ""
    var projectID: UUID?
    var errorMessage: String?

    private let items: [NSExtensionItem]
    private let finish: () -> Void
    private let scope = AppGroup.activeScope

    init(items: [NSExtensionItem], finish: @escaping () -> Void) {
        self.items = items
        self.finish = finish
        Task { await load() }
    }

    func cancel() { finish() }

    private func load() async {
        guard let scope else {
            phase = .blocked("请先打开 Gatherloom 登录，再从这里保存。")
            return
        }
        let outbox = CaptureOutbox(scope: scope)
        let recent = outbox.recentProjectIDs()
        let saved = (try? outbox.savedProjects()) ?? []
        projects = saved.sorted { lhs, rhs in
            (recent.firstIndex(of: lhs.id) ?? .max) < (recent.firstIndex(of: rhs.id) ?? .max)
        }

        var pageURL: String?
        for provider in items.flatMap({ $0.attachments ?? [] }) {
            if provider.hasItemConformingToTypeIdentifier(UTType.propertyList.identifier),
               let page = try? await provider.loadItem(forTypeIdentifier: UTType.propertyList.identifier) as? NSDictionary,
               let results = page[NSExtensionJavaScriptPreprocessingResultsKey] as? [String: Any] {
                pageURL = results["url"] as? String
                let canonical = (results["canonicalUrl"] as? String).flatMap { LinkDetector.webURL(in: $0) }
                extract = CaptureDraft.Extract(
                    title: String((results["title"] as? String ?? "").prefix(500)),
                    text: String((results["text"] as? String ?? "").prefix(100_000)),
                    canonicalUrl: canonical,
                    capturedAt: Date()
                )
            } else if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                if let data = await imageData(from: provider),
                   let thumbnail = ImagePayload.downsampled(data, maxPixels: 400) {
                    images.append(Candidate(provider: provider, thumbnail: thumbnail))
                }
            } else if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier),
                      let item = try? await provider.loadItem(forTypeIdentifier: UTType.url.identifier) {
                if let link = (item as? URL)?.absoluteString ?? (item as? String), LinkDetector.webURL(in: link) != nil {
                    pageURL = pageURL ?? link
                }
            } else if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
                      let item = try? await provider.loadItem(forTypeIdentifier: UTType.plainText.identifier) as? String {
                text = item.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        if !images.isEmpty {
            kind = .image
            if images.count == 1 { selectedImage = images[0].id }
        } else if let link = pageURL ?? LinkDetector.webURL(in: text) {
            kind = .url
            url = link
            if text == link { text = "" }
        } else if text.isEmpty {
            phase = .blocked("没有可保存的文字、链接或图片。")
            return
        }
        text = String(text.prefix(20_000))
        phase = .ready
    }

    var canSave: Bool {
        if case .ready = phase {
            return kind != .image || selectedImage != nil
        }
        return false
    }

    func save() async {
        guard let scope, canSave else { return }
        // The app may have switched accounts while the sheet was open.
        guard AppGroup.activeScope == scope else {
            phase = .blocked("Gatherloom 已切换账号。请重新分享，以免保存到错误的账号。")
            return
        }
        let remark = String(note.trimmingCharacters(in: .whitespacesAndNewlines).prefix(4000))
        let project = projects.first { $0.id == projectID }
        let outbox = CaptureOutbox(scope: scope)
        phase = .saving("正在存到本机")
        var draft: CaptureDraft
        do {
            switch kind {
            case .image:
                guard let candidate = images.first(where: { $0.id == selectedImage }),
                      let data = await imageData(from: candidate.provider),
                      let prepared = ImagePayload.prepare(data) else {
                    throw CocoaError(.fileReadCorruptFile)
                }
                draft = CaptureDraft(kind: .image, text: "", note: remark, image: prepared.file, project: project)
                try outbox.save(draft, imageData: prepared.data)
            case .url:
                draft = CaptureDraft(kind: .url, text: text, note: remark, url: url, extract: extract, project: project)
                try outbox.save(draft)
            case .thought:
                draft = CaptureDraft(kind: .thought, text: text, note: remark, project: project)
                try outbox.save(draft)
            }
            if let project { outbox.markRecent(project.id) }
        } catch {
            phase = .ready
            errorMessage = "无法存到本机。请重试，或打开 Gatherloom 保存。"
            return
        }
        let uploaded = await tryUpload(draft, scope: scope, outbox: outbox)
        phase = .finished(uploaded ? "已保存到服务器" : "已存到本机，打开 Gatherloom 后继续上传")
        try? await Task.sleep(for: .seconds(1.2))
        finish()
    }

    /// One short upload attempt. Anything that fails stays in the outbox for
    /// the app. Tokens are never refreshed here, so the app keeps the only
    /// refresh token in use.
    private func tryUpload(_ draft: CaptureDraft, scope: CaptureScope, outbox: CaptureOutbox) async -> Bool {
        guard let credentials = try? CredentialStore.load(accountID: scope.accountID, server: scope.server),
              let client = try? APIClient(address: scope.server.absoluteString) else {
            return false
        }
        let uploader = CaptureUploader(outbox: outbox) { path, method, body, form in
            try await client.send(path, method: method, body: body, form: form, accessToken: credentials.accessToken)
        }
        var draft = draft
        if draft.kind == .image {
            phase = .saving("正在识别截图文字")
            draft = await uploader.recognizeIfNeeded(draft)
        }
        phase = .saving("正在上传")
        let attempt = draft
        let delivered = await withTaskGroup(of: Bool.self) { group in
            group.addTask { (try? await uploader.deliver(attempt)) != nil }
            group.addTask {
                try? await Task.sleep(for: .seconds(10))
                return false
            }
            let first = await group.next() ?? false
            group.cancelAll()
            return first
        }
        if delivered { try? outbox.acknowledge(draft.id) }
        return delivered
    }

    private func imageData(from provider: NSItemProvider) async -> Data? {
        guard let item = try? await provider.loadItem(forTypeIdentifier: UTType.image.identifier) else { return nil }
        if let url = item as? URL { return try? Data(contentsOf: url) }
        if let data = item as? Data { return data }
        if let image = item as? UIImage { return image.pngData() }
        return nil
    }
}

struct ShareView: View {
    @Bindable var model: ShareModel

    var body: some View {
        NavigationStack {
            Group {
                switch model.phase {
                case .loading:
                    ProgressView("正在读取内容")
                case let .blocked(message):
                    ContentUnavailableView("无法保存", systemImage: "exclamationmark.triangle", description: Text(message))
                case let .saving(message):
                    ProgressView(message)
                case let .finished(message):
                    ContentUnavailableView(message, systemImage: "checkmark.circle")
                case .ready:
                    form
                }
            }
            .navigationTitle("存到 Gatherloom")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { model.cancel() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { Task { await model.save() } }.disabled(!model.canSave)
                }
            }
        }
    }

    private var form: some View {
        Form {
            switch model.kind {
            case .image:
                Section {
                    if model.images.count > 1 {
                        Text("一次保存一张。请选择要保存的图片，其他图片不会保存。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    ScrollView(.horizontal) {
                        HStack {
                            ForEach(model.images) { candidate in
                                Image(uiImage: candidate.thumbnail)
                                    .resizable().scaledToFill()
                                    .frame(width: 96, height: 128).clipped()
                                    .overlay {
                                        RoundedRectangle(cornerRadius: 6)
                                            .stroke(model.selectedImage == candidate.id ? Color.accentColor : .clear, lineWidth: 3)
                                    }
                                    .clipShape(RoundedRectangle(cornerRadius: 6))
                                    .onTapGesture { model.selectedImage = candidate.id }
                                    .accessibilityAddTraits(model.selectedImage == candidate.id ? .isSelected : [])
                            }
                        }
                    }
                } header: { Text("截图") }
            case .url:
                Section("链接") {
                    if let title = model.extract?.title, !title.isEmpty { Text(title).font(.headline) }
                    if let url = model.url { Text(url).font(.footnote).foregroundStyle(.secondary).lineLimit(2) }
                    if let length = model.extract?.text.count, length > 0 {
                        Text("已读取页面正文 \(length) 字").font(.footnote).foregroundStyle(.secondary)
                    }
                }
            case .thought:
                Section("文字") { Text(model.text).lineLimit(8) }
            }
            Section("我的备注") {
                TextField("备注（可选）", text: $model.note, axis: .vertical).lineLimit(1...4)
            }
            Section("归属") {
                Picker("保存到", selection: $model.projectID) {
                    Text("收集箱").tag(nil as UUID?)
                    ForEach(model.projects) { Text($0.name).tag(Optional($0.id)) }
                }
            }
            if let error = model.errorMessage {
                Text(error).foregroundStyle(.red)
            }
        }
    }
}
