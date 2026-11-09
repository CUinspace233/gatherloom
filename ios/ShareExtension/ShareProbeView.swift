import SwiftUI

struct ShareProbeView: View {
    @ObservedObject var model: ShareProbeModel

    var body: some View {
        NavigationStack {
            Group {
                switch model.phase {
                case .running:
                    ProgressView("正在处理分享内容…")
                case .finished:
                    if let record = model.record {
                        ShareRecordSummary(record: record, saveMessage: model.saveMessage)
                    }
                }
            }
            .navigationTitle("Gatherloom S0")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { model.onDone() }
                        .disabled(model.phase == .running)
                }
            }
        }
    }
}
