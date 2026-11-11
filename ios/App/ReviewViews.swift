import SwiftUI

struct ReviewSummary: Decodable, Identifiable, Hashable {
    let id: UUID
    let kind: String
    let periodStart: Date
    let periodEnd: Date
    let unread: Bool
    let createdAt: Date

    var title: String {
        let range = "\(periodStart.formatted(.dateTime.month().day()))–\(periodEnd.formatted(.dateTime.month().day()))"
        return kind == "auto" ? "周回顾 \(range)" : "阶段回顾 \(range)"
    }
}

private struct ReviewSections: Decodable {
    let newInformation: [SummaryPoint]
    let understandingChanges: [SummaryPoint]
    let decisions: [SummaryPoint]
    let openQuestions: [SummaryPoint]
    let nextSteps: [SummaryPoint]
}

private struct ReviewDetail: Decodable {
    let id: UUID
    let kind: String
    let periodStart: Date
    let periodEnd: Date
    let sections: ReviewSections
    let sources: [Citation]
}

private struct ReviewRun: Decodable {
    let runId: UUID
}

private struct RunStatus: Decodable {
    let status: String
    let reviewId: UUID?
    let errorCode: String?
}

/// Latest reviews on the project overview, with on-demand generation.
struct ProjectReviewsSection: View {
    let session: AccountSession
    let capture: CaptureQueue
    let project: Project
    @State private var reviews: [ReviewSummary] = []
    @State private var isGenerating = false
    @State private var errorMessage: String?

    var body: some View {
        Section {
            ForEach(reviews.prefix(3)) { review in
                NavigationLink {
                    ReviewDetailView(session: session, capture: capture, reviewID: review.id) { await load() }
                } label: {
                    HStack {
                        if review.unread {
                            Circle().fill(Color.accentColor).frame(width: 8, height: 8)
                        }
                        Text(review.title)
                    }
                }
            }
            if isGenerating {
                HStack {
                    ProgressView()
                    Text("正在生成回顾…").foregroundStyle(.secondary)
                }
            } else {
                Button("生成阶段回顾", systemImage: "calendar.badge.clock") { Task { await generate() } }
            }
            if let errorMessage { Text(errorMessage).font(.footnote).foregroundStyle(.red) }
        } header: {
            Text("回顾")
        } footer: {
            Text("每周一自动回顾上一周有变化的项目；手动回顾覆盖上次回顾结束至今。")
        }
        .task { await load() }
    }

    private func load() async {
        reviews = (try? await session.request(
            [ReviewSummary].self, "v1/projects/\(project.id.uuidString)/reviews", scope: capture.scope
        )) ?? reviews
    }

    private func generate() async {
        isGenerating = true
        errorMessage = nil
        defer { isGenerating = false }
        do {
            let run = try await session.request(
                ReviewRun.self, "v1/projects/\(project.id.uuidString)/reviews", method: "POST", scope: capture.scope
            )
            while !Task.isCancelled {
                try await Task.sleep(for: .seconds(2))
                let state = try await session.request(
                    RunStatus.self, "v1/runs/\(run.runId.uuidString)", scope: capture.scope
                )
                if state.status == "succeeded" { break }
                if state.status == "failed" || state.status == "cancelled" {
                    errorMessage = "回顾生成失败，可以稍后再试。"
                    break
                }
            }
            await load()
        } catch {
            if !(error is CancellationError) { errorMessage = error.localizedDescription }
        }
    }
}

struct ReviewDetailView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let reviewID: UUID
    var onRead: () async -> Void = {}
    @State private var review: ReviewDetail?
    @State private var errorMessage: String?
    @State private var selected: Citation?

    var body: some View {
        Group {
            if let review {
                List {
                    Section {
                        Text("\(review.periodStart.formatted(date: .abbreviated, time: .omitted)) 至 \(review.periodEnd.formatted(date: .abbreviated, time: .omitted))")
                            .foregroundStyle(.secondary)
                    } footer: {
                        Text("AI 整理，可能有误；点 [n] 查看依据。")
                    }
                    section("本期新增信息", review.sections.newInformation, review.sources)
                    section("认识变化", review.sections.understandingChanges, review.sources)
                    section("正式决定", review.sections.decisions, review.sources)
                    section("未解决问题", review.sections.openQuestions, review.sources)
                    section("下一阶段建议（AI 建议）", review.sections.nextSteps, review.sources)
                }
            } else if let errorMessage {
                ContentUnavailableView("无法打开回顾", systemImage: "doc.questionmark", description: Text(errorMessage))
            } else {
                ProgressView()
            }
        }
        .navigationTitle(review?.kind == "auto" ? "周回顾" : "阶段回顾")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $selected) { citation in
            CitationSheet(session: session, capture: capture, citation: citation)
        }
        .task { await load() }
    }

    @ViewBuilder
    private func section(_ title: String, _ points: [SummaryPoint], _ sources: [Citation]) -> some View {
        if !points.isEmpty {
            Section(title) {
                ForEach(points, id: \.self) { point in
                    CitedText(text: point.text, sources: sources) { selected = $0 }
                }
            }
        }
    }

    private func load() async {
        do {
            review = try await session.request(ReviewDetail.self, "v1/reviews/\(reviewID.uuidString)", scope: capture.scope)
            _ = try? await session.send("v1/reviews/\(reviewID.uuidString)/read", method: "PATCH", scope: capture.scope)
            await onRead()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
