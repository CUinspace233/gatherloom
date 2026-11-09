import SwiftUI
import UIKit

/// 分享扩展入口（Info.plist 中 NSExtensionPrincipalClass）。
/// S0 探针：读取分享内容（Safari JS 预处理结果 / 图片 / URL / 文字），在扩展内执行 OCR，
/// 记录耗时与内存，并写入 App Group 供主 App 查看。
final class ShareViewController: UIViewController {
    private let model = ShareProbeModel()

    override func viewDidLoad() {
        super.viewDidLoad()

        model.onDone = { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        }

        let host = UIHostingController(rootView: ShareProbeView(model: model))
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)

        let items = (extensionContext?.inputItems as? [NSExtensionItem]) ?? []
        Task { await model.process(items: items) }
    }
}
