import Foundation
import Security

struct KeychainError: LocalizedError {
    let status: OSStatus
    let operation: String

    var errorDescription: String? {
        let message = (SecCopyErrorMessageString(status, nil) as String?) ?? "未知错误"
        return "\(operation) 失败：\(status) \(message)"
    }
}

/// 共享访问组中的通用密码读写。S0 用来验证主 App 写入、分享扩展读取。
struct KeychainStore {
    static let probeService = "app.gatherloom.s0-probe"
    static let probeAccount = "probe-token"

    let accessGroup: String

    /// 完整访问组 = AppIdentifierPrefix（Team ID + "."）+ KeychainGroupSuffix。
    static func shared() throws -> KeychainStore {
        KeychainStore(accessGroup: try appIdentifierPrefix() + SharedConfig.keychainGroupSuffix)
    }

    /// 通过写入一个不指定访问组的条目，读回系统分配的默认访问组，从中取出 Team ID 前缀。
    static func appIdentifierPrefix() throws -> String {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "app.gatherloom.prefix-probe",
            kSecAttrAccount as String: "prefix",
        ]
        var query = base
        query[kSecReturnAttributes as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: CFTypeRef?
        var status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            var add = base
            add[kSecValueData as String] = Data("prefix".utf8)
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            add[kSecReturnAttributes as String] = true
            status = SecItemAdd(add as CFDictionary, &result)
        }
        guard status == errSecSuccess else { throw KeychainError(status: status, operation: "读取默认访问组") }
        guard
            let attributes = result as? [String: Any],
            let group = attributes[kSecAttrAccessGroup as String] as? String,
            let dot = group.firstIndex(of: ".")
        else {
            throw KeychainError(status: errSecInternalError, operation: "解析默认访问组")
        }
        return String(group[...dot])
    }

    func save(_ value: String, account: String = KeychainStore.probeAccount) throws {
        try? delete(account: account)
        let item: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.probeService,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
            kSecValueData as String: Data(value.utf8),
        ]
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError(status: status, operation: "写入") }
    }

    /// 返回 nil 表示条目不存在。
    func read(account: String = KeychainStore.probeAccount) throws -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.probeService,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainError(status: status, operation: "读取")
        }
        return String(decoding: data, as: UTF8.self)
    }

    func delete(account: String = KeychainStore.probeAccount) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.probeService,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String: accessGroup,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError(status: status, operation: "删除")
        }
    }

    /// 给探针记录使用的一行描述；不输出完整值。
    static func probeReadDescription() -> String {
        guard SharedConfig.keychainSharingEnabled else { return "未启用（ENTITLEMENTS_VARIANT=-None）" }
        do {
            let store = try KeychainStore.shared()
            guard let value = try store.read() else { return "未找到条目（主 App 尚未写入）· 组 \(store.accessGroup)" }
            return "读取成功：\(value.prefix(6))…（\(value.count) 字符）· 组 \(store.accessGroup)"
        } catch {
            return error.localizedDescription
        }
    }
}
