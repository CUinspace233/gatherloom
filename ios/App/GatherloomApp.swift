import SwiftUI

/// S0 阶段的主 App 是一组验证探针（ADR 0003、0004、0008），S1 起替换为正式界面。
@main
struct GatherloomApp: App {
    var body: some Scene {
        WindowGroup {
            ProbeHomeView()
        }
    }
}
