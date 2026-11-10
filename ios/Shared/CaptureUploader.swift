import Foundation

/// Sends one local submission: OCR if still needed, then the image, then the
/// entry. Progress is written back to the outbox so retries resume.
struct CaptureUploader {
    typealias Send = (_ path: String, _ method: String, _ body: (any Encodable)?, _ form: MultipartForm?) async throws -> Data

    let outbox: CaptureOutbox
    let send: Send

    func recognizeIfNeeded(_ draft: CaptureDraft) async -> CaptureDraft {
        guard draft.kind == .image, !draft.ocrAttempted, let url = outbox.imageURL(for: draft) else { return draft }
        var updated = draft
        updated.ocr = try? await TextRecognizer.recognize(imageAt: url)
        updated.ocrAttempted = true
        try? outbox.update(updated)
        return updated
    }

    func deliver(_ draft: CaptureDraft) async throws -> CaptureEntry {
        var draft = draft
        if draft.kind == .image, draft.attachmentId == nil {
            guard let image = draft.image, let url = outbox.imageURL(for: draft) else {
                throw APIError(status: 422, detail: "missing image")
            }
            var form = MultipartForm()
            form.add("client_submission_id", draft.id.uuidString.lowercased())
            form.add("sha256", image.sha256)
            form.addFile("file", fileName: image.fileName, contentType: image.contentType, data: try Data(contentsOf: url))
            let data = try await send("v1/attachments", "POST", nil, form.finished())
            let attachment = try APIClient.decode(AttachmentInfo.self, from: data)
            draft.attachmentId = attachment.id
            try? outbox.update(draft)
        }
        let data = try await send("v1/entries", "POST", draft.submission(attachmentId: draft.attachmentId), nil)
        let entry = try APIClient.decode(CaptureEntry.self, from: data)
        guard entry.clientSubmissionId == draft.id else {
            throw APIError(status: 409, detail: "submission mismatch")
        }
        return entry
    }
}
