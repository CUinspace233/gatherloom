import SwiftUI
import UIKit

final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let appGroup = SharingProbeStore.readAppGroup()
        let keychain = SharingProbeStore.readKeychain()
        let content = NavigationStack {
            Form {
                LabeledContent("App Group", value: appGroup)
                LabeledContent("Keychain", value: keychain)
            }
            .navigationTitle("读取测试码")
            .toolbar {
                Button("完成") { [weak self] in
                    self?.extensionContext?.completeRequest(returningItems: nil)
                }
            }
        }
        let host = UIHostingController(rootView: content)
        addChild(host)
        view.addSubview(host.view)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        host.didMove(toParent: self)
    }
}
