import Foundation
import UIKit

/// Safari JS 预处理结果（ios/Preprocess/src/preprocess.js 的返回值）。
struct PreprocessResult: Codable {
    var preprocessVersion: Int?
    var url: String?
    var title: String?
    var canonicalURL: String?
    var capturedAt: String?
    var lang: String?
    var extractor: String?
    var articleTitle: String?
    var byline: String?
    var siteName: String?
    var excerpt: String?
    var publishedTime: String?
    var textContent: String?
    var textLength: Int?
    var originalTextLength: Int?
    var truncated: Bool?
    var htmlLength: Int?
    var elapsedMs: Double?
    var error: String?

    init(dictionary d: [String: Any]) {
        func string(_ key: String) -> String? { d[key] as? String }
        func number(_ key: String) -> NSNumber? { d[key] as? NSNumber }
        preprocessVersion = number("preprocessVersion")?.intValue
        url = string("url")
        title = string("title")
        canonicalURL = string("canonicalURL")
        capturedAt = string("capturedAt")
        lang = string("lang")
        extractor = string("extractor")
        articleTitle = string("articleTitle")
        byline = string("byline")
        siteName = string("siteName")
        excerpt = string("excerpt")
        publishedTime = string("publishedTime")
        textContent = string("textContent")
        textLength = number("textLength")?.intValue
        originalTextLength = number("originalTextLength")?.intValue
        truncated = number("truncated")?.boolValue
        htmlLength = number("htmlLength")?.intValue
        elapsedMs = number("elapsedMs")?.doubleValue
        error = string("error")
    }
}

/// 分享扩展的一次探针记录，写入 App Group 供主 App 查看与导出。
struct ShareCaptureRecord: Codable, Identifiable {
    var id: String
    var createdAt: Date
    /// safari-page / url / image / text / unknown
    var kind: String
    var attachmentTypes: [String]
    var url: String?
    var title: String?
    var text: String?
    var preprocess: PreprocessResult?
    var ocr: OCRResult?
    var keychain: String
    var memory: MemorySnapshot?
    var elapsedMs: Double
    var errors: [String]
}

struct DeviceInfo: Codable {
    var model: String
    var systemName: String
    var systemVersion: String
    var bundleID: String
    var appVersion: String
    var appGroupID: String
    var keychainGroupSuffix: String
    var entitlementsVariant: String
    var capturedAt: Date

    @MainActor
    static func current() -> DeviceInfo {
        var system = utsname()
        uname(&system)
        let model = withUnsafePointer(to: &system.machine) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
        let bundle = Bundle.main
        let version = (bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "?"
        let build = (bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String) ?? "?"
        return DeviceInfo(
            model: model,
            systemName: UIDevice.current.systemName,
            systemVersion: UIDevice.current.systemVersion,
            bundleID: bundle.bundleIdentifier ?? "?",
            appVersion: "\(version) (\(build))",
            appGroupID: SharedConfig.appGroupID,
            keychainGroupSuffix: SharedConfig.keychainGroupSuffix,
            entitlementsVariant: SharedConfig.entitlementsVariant.isEmpty ? "完整" : SharedConfig.entitlementsVariant,
            capturedAt: Date()
        )
    }
}
