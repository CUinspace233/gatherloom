import Foundation
import Security

enum CredentialStore {
    private static func query(accountID: UUID, server: URL) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "io.github.cuinspace233.gatherloom.session",
            kSecAttrAccount as String: "\(server.absoluteString)|\(accountID.uuidString)",
        ]
    }

    static func load(accountID: UUID, server: URL) throws -> Credentials? {
        var query = query(accountID: accountID, server: server)
        query[kSecReturnData as String] = true
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        try check(status)
        guard let data = item as? Data else { throw URLError(.cannotDecodeContentData) }
        return try JSONDecoder().decode(Credentials.self, from: data)
    }

    static func save(_ credentials: Credentials, server: URL) throws {
        let query = query(accountID: credentials.account.id, server: server)
        let data = try JSONEncoder().encode(credentials)
        let changes = [kSecValueData as String: data]
        let status = SecItemUpdate(query as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            try check(SecItemAdd(item as CFDictionary, nil))
        } else {
            try check(status)
        }
    }

    static func delete(accountID: UUID, server: URL) throws {
        let status = SecItemDelete(query(accountID: accountID, server: server) as CFDictionary)
        if status != errSecItemNotFound { try check(status) }
    }

    private static func check(_ status: OSStatus) throws {
        if status != errSecSuccess {
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(status),
                userInfo: [NSLocalizedDescriptionKey: String(localized: "无法访问登录凭证（Keychain \(status)）。")]
            )
        }
    }
}
