import SwiftUI

struct RevisionText: Decodable {
    struct OCRLine: Decodable {
        let text: String
        let box: [Double]
        let start: Int?
        let end: Int?
    }

    let revision: Int
    let currentRevision: Int
    let type: EntryKind
    let text: String
    let note: String
    let title: String?
    let extractText: String?
    let ocrText: String?
    let ocrLines: [OCRLine]
    let attachmentId: UUID?
}

/// The cited revision's full original text, scrolled to and highlighting the
/// cited range. Offsets count Unicode scalars, as the server does.
struct SourceTextView: View {
    let session: AccountSession
    let capture: CaptureQueue
    let citation: Citation
    @State private var revision: RevisionText?
    @State private var errorMessage: String?

    private struct Paragraph: Identifiable {
        let id: Int
        let range: Range<Int>
        let text: String
    }

    private var range: Range<Int> { citation.startOffset..<max(citation.startOffset, citation.endOffset) }

    var body: some View {
        Group {
            if let revision {
                content(revision)
            } else if let errorMessage {
                ContentUnavailableView("无法打开原文", systemImage: "doc.questionmark", description: Text(errorMessage))
            } else {
                ProgressView()
            }
        }
        .navigationTitle("引用 [\(citation.n)] 原文")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            do {
                revision = try await session.request(
                    RevisionText.self,
                    "v1/entries/\(citation.entryId.uuidString)/revisions/\(citation.revision)",
                    scope: capture.scope
                )
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func body(of revision: RevisionText) -> String? {
        switch citation.source {
        case "extract": revision.extractText
        case "ocr": revision.ocrText
        case "text": revision.text
        default: nil
        }
    }

    @ViewBuilder
    private func content(_ revision: RevisionText) -> some View {
        let paragraphs = Self.paragraphs(body(of: revision) ?? "")
        let target = paragraphs.first { $0.range.overlaps(range) }?.id
        ScrollViewReader { proxy in
            List {
                if revision.revision != revision.currentRevision {
                    Section {
                        Text("这是回答时引用的第 \(revision.revision) 版，资料之后已经修改。")
                            .foregroundStyle(.orange)
                        NavigationLink("查看最新版本") {
                            CitedEntryView(session: session, capture: capture, entryID: citation.entryId)
                        }
                    }
                }
                if citation.source == "ocr", let attachment = revision.attachmentId {
                    Section("截图") {
                        AttachmentImage(
                            scope: capture.scope, attachmentID: attachment,
                            highlights: revision.ocrLines.compactMap { line in
                                guard let start = line.start, let end = line.end,
                                      (start..<max(start, end)).overlaps(range) else { return nil }
                                return line.box
                            }
                        )
                    }
                }
                if paragraphs.isEmpty {
                    Section("引用的内容") { Text(citation.snippet).textSelection(.enabled) }
                } else {
                    Section(sectionTitle(revision)) {
                        ForEach(paragraphs) { paragraph in
                            Text(highlightedParagraph(paragraph))
                                .textSelection(.enabled)
                                .id(paragraph.id)
                                .listRowBackground(
                                    paragraph.range.overlaps(range) ? Color.yellow.opacity(0.12) : nil
                                )
                        }
                    }
                }
            }
            .task(id: target) {
                guard let target else { return }
                try? await Task.sleep(for: .milliseconds(150))
                withAnimation { proxy.scrollTo(target, anchor: .center) }
            }
        }
    }

    private func sectionTitle(_ revision: RevisionText) -> String {
        switch citation.source {
        case "extract": revision.title.map { "网页正文 · \($0)" } ?? "网页正文"
        case "ocr": "识别文字"
        default: "原文"
        }
    }

    private func highlightedParagraph(_ paragraph: Paragraph) -> AttributedString {
        let lower = max(range.lowerBound, paragraph.range.lowerBound) - paragraph.range.lowerBound
        let upper = min(range.upperBound, paragraph.range.upperBound) - paragraph.range.lowerBound
        guard lower < upper else { return AttributedString(paragraph.text) }
        var result = highlighted(paragraph.text, [[lower, upper]])
        if let marked = result.range(of: String(String.UnicodeScalarView(
            Array(paragraph.text.unicodeScalars)[lower..<upper]
        ))) {
            result[marked].backgroundColor = .yellow.opacity(0.45)
        }
        return result
    }

    /// Splits text into paragraphs at newlines, keeping scalar offsets.
    private static func paragraphs(_ text: String) -> [Paragraph] {
        var result: [Paragraph] = []
        var start = 0
        var current = String.UnicodeScalarView()
        func flush(at end: Int) {
            let piece = String(current)
            if !piece.trimmingCharacters(in: .whitespaces).isEmpty {
                result.append(Paragraph(id: result.count, range: start..<end, text: piece))
            }
            current = String.UnicodeScalarView()
        }
        for (offset, scalar) in text.unicodeScalars.enumerated() {
            if scalar == "\n" {
                flush(at: offset)
                start = offset + 1
            } else {
                current.append(scalar)
            }
        }
        flush(at: text.unicodeScalars.count)
        return result
    }
}
