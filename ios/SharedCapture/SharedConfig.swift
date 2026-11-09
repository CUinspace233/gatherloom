import Foundation

/// 构建时注入的配置（来自 Config/*.xcconfig → Info.plist）。主 App 与分享扩展各自读取自己的 Info.plist。
enum SharedConfig {
    static var appGroupID: String { string("AppGroupID") }
    static var keychainGroupSuffix: String { string("KeychainGroupSuffix") }

    /// 空字符串表示完整能力；"-KeychainOnly" / "-None" 见 Config/Base.xcconfig。
    static var entitlementsVariant: String { string("EntitlementsVariant") }

    static var appGroupEnabled: Bool { entitlementsVariant.isEmpty }
    static var keychainSharingEnabled: Bool { entitlementsVariant != "-None" }

    private static func string(_ key: String) -> String {
        (Bundle.main.object(forInfoDictionaryKey: key) as? String) ?? ""
    }
}

extension JSONEncoder {
    static var probe: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}

extension JSONDecoder {
    static var probe: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

extension Date {
    /// 用于文件名的紧凑时间戳，例如 20261109-153012
    var fileStamp: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: self)
    }
}
