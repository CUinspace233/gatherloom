import CryptoKit
import ImageIO
import UIKit
import UniformTypeIdentifiers

/// Normalizes a picked or shared image for upload: keeps supported formats up
/// to 10 MB as-is and re-encodes everything else as JPEG.
enum ImagePayload {
    static let maxBytes = 10 * 1024 * 1024

    struct Prepared {
        let data: Data
        let file: CaptureDraft.ImageFile
    }

    static func prepare(_ data: Data) -> Prepared? {
        if data.count <= maxBytes, let (type, ext) = sniff(data) {
            return Prepared(data: data, file: file(data, type: type, ext: ext))
        }
        for maxPixels in [4096, 3072, 2048] {
            guard let jpeg = downsampled(data, maxPixels: maxPixels)?.jpegData(compressionQuality: 0.85) else { return nil }
            if jpeg.count <= maxBytes {
                return Prepared(data: jpeg, file: file(jpeg, type: "image/jpeg", ext: "jpg"))
            }
        }
        return nil
    }

    static func downsampled(_ data: Data, maxPixels: Int) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixels,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return UIImage(cgImage: image)
    }

    private static func file(_ data: Data, type: String, ext: String) -> CaptureDraft.ImageFile {
        let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        return CaptureDraft.ImageFile(fileName: "image.\(ext)", sha256: digest, size: data.count, contentType: type)
    }

    private static func sniff(_ data: Data) -> (String, String)? {
        let head = [UInt8](data.prefix(16))
        guard head.count >= 12 else { return nil }
        if head.starts(with: [0xFF, 0xD8, 0xFF]) { return ("image/jpeg", "jpg") }
        if head.starts(with: [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) { return ("image/png", "png") }
        let brand = String(decoding: head[4..<12], as: UTF8.self)
        if ["ftypheic", "ftypheix", "ftypmif1"].contains(brand) { return ("image/heic", "heic") }
        if String(decoding: head[0..<4], as: UTF8.self) == "RIFF", String(decoding: head[8..<12], as: UTF8.self) == "WEBP" {
            return ("image/webp", "webp")
        }
        return nil
    }
}
