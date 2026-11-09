import Foundation
import Security

enum SharingProbeStore {
    private static var sharedFile: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: "group.io.github.cuinspace233.gatherloom.probe")?
            .appendingPathComponent("probe.txt")
    }

    private static var keychainQuery: [String: Any] {
        let group = Bundle.main.object(forInfoDictionaryKey: "GatherloomProbeKeychainGroup") as? String ?? ""
        return [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccessGroup as String: group,
            kSecAttrService as String: "GatherloomSharingProbe",
            kSecAttrAccount as String: "sample",
        ]
    }

    static func writeAppGroup(_ code: String) -> String {
        guard let sharedFile else { return "App Group 容器不可用" }
        do {
            try Data(code.utf8).write(to: sharedFile, options: .atomic)
            return code
        } catch {
            return "写入失败。\(error.localizedDescription)"
        }
    }

    static func readAppGroup() -> String {
        guard let sharedFile else { return "App Group 容器不可用" }
        do {
            return try String(contentsOf: sharedFile, encoding: .utf8)
        } catch {
            return "读取失败。\(error.localizedDescription)"
        }
    }

    static func writeKeychain(_ code: String) -> String {
        var attributes: [String: Any] = [kSecValueData as String: Data(code.utf8)]
        var status = SecItemUpdate(keychainQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            attributes[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            let item = keychainQuery.merging(attributes) { _, value in value }
            status = SecItemAdd(item as CFDictionary, nil)
        }
        return status == errSecSuccess ? code : "写入失败（OSStatus \(status)）"
    }

    static func readKeychain() -> String {
        var query = keychainQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess else { return "读取失败（OSStatus \(status)）" }
        guard let data = result as? Data, let code = String(data: data, encoding: .utf8) else {
            return "读取到了无效测试码"
        }
        return code
    }
}
