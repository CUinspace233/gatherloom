import Foundation
import Observation

@MainActor
@Observable
final class AccountSession {
    private static let defaults = AccountSession.migratedDefaults()
    private var client: APIClient?
    private var credentials: Credentials?
    private var sessionID = UUID()
    private var refreshTask: Task<Credentials, Error>?

    private(set) var isRestoring = true
    private(set) var isSigningOut = false
    private(set) var notice: String?
    var serverAddress = AccountSession.defaults.string(forKey: AppGroup.serverKey) ?? ""
    var account: Account? { credentials?.account }
    var captureScope: CaptureScope? {
        guard let client, let account else { return nil }
        return CaptureScope(server: client.baseURL, accountID: account.id)
    }

    func restore() {
        guard isRestoring else { return }
        defer { isRestoring = false }
        guard let saved = Self.defaults.string(forKey: AppGroup.accountKey),
              let accountID = UUID(uuidString: saved) else { return }
        do {
            let client = try APIClient(address: serverAddress)
            if let saved = try CredentialStore.load(accountID: accountID, server: client.baseURL) {
                self.client = client
                credentials = saved
            }
        } catch {
            notice = error.localizedDescription
        }
    }

    func signIn(username: String, password: String, invitation: String?) async throws {
        let client = try APIClient(address: serverAddress)
        let payload = LoginBody(username: username, password: password, inviteCode: invitation)
        let path = invitation == nil ? "v1/auth/login" : "v1/auth/register"
        let data = try await client.send(path, method: "POST", body: payload)
        let credentials = try APIClient.decode(Credentials.self, from: data)
        try CredentialStore.save(credentials, server: client.baseURL)
        serverAddress = client.baseURL.absoluteString
        Self.defaults.set(serverAddress, forKey: AppGroup.serverKey)
        Self.defaults.set(credentials.account.id.uuidString, forKey: AppGroup.accountKey)
        self.client = client
        self.credentials = credentials
        sessionID = UUID()
        notice = nil
    }

    func signOut() async throws {
        guard !isSigningOut, let client else { return }
        isSigningOut = true
        defer { isSigningOut = false }
        if let refreshTask { _ = try await refreshTask.value }
        guard let credentials else { return }
        _ = try await client.send(
            "v1/auth/logout", method: "POST", body: RefreshBody(refreshToken: credentials.refreshToken)
        )
        clearSession()
    }

    func request<T: Decodable>(
        _ type: T.Type,
        _ path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: (any Encodable)? = nil,
        form: MultipartForm? = nil,
        scope: CaptureScope? = nil
    ) async throws -> T {
        let data = try await send(path, method: method, query: query, body: body, form: form, scope: scope)
        return try APIClient.decode(type, from: data)
    }

    /// Sends an authenticated request, renewing the access token once if it expired.
    func send(
        _ path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: (any Encodable)? = nil,
        form: MultipartForm? = nil,
        scope: CaptureScope? = nil
    ) async throws -> Data {
        try Task.checkCancellation()
        if let scope, captureScope != scope { throw CancellationError() }
        guard !isSigningOut, let client, let credentials else { throw signedOut }
        let id = sessionID
        let data: Data
        do {
            data = try await client.send(
                path, method: method, query: query, body: body, form: form, accessToken: credentials.accessToken
            )
        } catch let error as APIError where error.status == 401 {
            let renewed = try await refresh(client: client, rejectedToken: credentials.accessToken, id: id)
            try Task.checkCancellation()
            guard id == sessionID, !isSigningOut else { throw CancellationError() }
            data = try await client.send(
                path, method: method, query: query, body: body, form: form, accessToken: renewed.accessToken
            )
        }
        try Task.checkCancellation()
        guard id == sessionID, !isSigningOut else { throw CancellationError() }
        return data
    }

    /// Moves the active account from the app's own defaults into the App
    /// Group so the share extension saves to the same account.
    private static func migratedDefaults() -> UserDefaults {
        let shared = AppGroup.defaults
        for key in [AppGroup.serverKey, AppGroup.accountKey] {
            if shared.object(forKey: key) == nil, let value = UserDefaults.standard.string(forKey: key) {
                shared.set(value, forKey: key)
            }
            UserDefaults.standard.removeObject(forKey: key)
        }
        return shared
    }

    private func refresh(client: APIClient, rejectedToken: String, id: UUID) async throws -> Credentials {
        guard id == sessionID, !isSigningOut, let credentials else { throw signedOut }
        if credentials.accessToken != rejectedToken { return credentials }
        if let refreshTask { return try await refreshTask.value }

        let task = Task {
            let data: Data
            do {
                data = try await client.send(
                    "v1/auth/refresh", method: "POST", body: RefreshBody(refreshToken: credentials.refreshToken)
                )
            } catch let error as APIError where error.status == 401 {
                if id == sessionID { clearSession(message: error.localizedDescription) }
                throw error
            }
            guard id == sessionID else { throw CancellationError() }
            let renewed = try APIClient.decode(Credentials.self, from: data)
            self.credentials = renewed
            try CredentialStore.save(renewed, server: client.baseURL)
            return renewed
        }
        refreshTask = task
        defer { if id == sessionID { refreshTask = nil } }
        return try await task.value
    }

    private var signedOut: APIError { APIError(status: 401, detail: nil) }

    private func clearSession(message: String? = nil) {
        if let account, let client {
            do {
                try CredentialStore.delete(accountID: account.id, server: client.baseURL)
            } catch {
                notice = error.localizedDescription
            }
        }
        credentials = nil
        client = nil
        sessionID = UUID()
        refreshTask = nil
        Self.defaults.removeObject(forKey: AppGroup.accountKey)
        if let message { notice = message }
    }

    private struct LoginBody: Encodable {
        let username: String
        let password: String
        let inviteCode: String?
    }

    private struct RefreshBody: Encodable {
        let refreshToken: String
    }
}
