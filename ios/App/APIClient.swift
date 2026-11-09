import Foundation

struct Account: Codable {
    let id: UUID
    let username: String
    let timezone: String
}

struct Credentials: Codable {
    let account: Account
    let accessToken: String
    let refreshToken: String
}

struct APIError: LocalizedError {
    let status: Int
    let detail: String?

    var errorDescription: String? {
        switch (status, detail) {
        case (400, "invalid or expired invitation"): "邀请码无效、已使用或已过期。"
        case (401, "invalid username or password"): "用户名或密码错误。"
        case (401, _): "登录已过期，请重新登录。"
        case (404, _): "找不到该项目或问题。"
        case (409, _): "该用户名已被使用。"
        case (422, "username"): "用户名须为 3–32 位字母、数字、下划线、点或连字符。"
        case (422, "password"): "密码须为 8–128 个字符。"
        case (422, "invite_code"): "请输入有效长度的邀请码。"
        case (422, "name"): "项目名称须为 1–120 个字符。"
        case (422, "goal"): "项目目标不能超过 4000 个字符。"
        case (422, "text"): "问题须为 1–2000 个字符。"
        case (422, _): "请检查输入内容和长度。"
        case (429, _): "操作太频繁，请稍后再试。"
        default: "请求失败（HTTP \(status)）。"
        }
    }
}

struct APIClient {
    let baseURL: URL

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.waitsForConnectivity = true
        config.timeoutIntervalForResource = 15
        return URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }()

    init(address: String) throws {
        guard var components = URLComponents(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = components.scheme?.lowercased(),
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil else {
            throw URLError(.badURL)
        }
        #if DEBUG
        guard scheme == "http" || scheme == "https" else { throw URLError(.unsupportedURL) }
        #else
        guard scheme == "https" else { throw URLError(.unsupportedURL) }
        #endif
        components.scheme = scheme
        components.host = host.lowercased()
        while components.percentEncodedPath.hasSuffix("/") {
            components.percentEncodedPath.removeLast()
        }
        guard let url = components.url else { throw URLError(.badURL) }
        baseURL = url
    }

    func send(
        _ path: String,
        method: String = "GET",
        query: [URLQueryItem] = [],
        body: (any Encodable)? = nil,
        accessToken: String? = nil
    ) async throws -> Data {
        let endpoint = baseURL.appending(path: path)
        guard var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false) else {
            throw URLError(.badURL)
        }
        if !query.isEmpty { components.queryItems = query }
        guard let url = components.url else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let accessToken {
            request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            request.httpBody = try encoder.encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await Self.session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(response.statusCode) else {
            let detail = response.statusCode == 422
                ? (try? Self.decode(ValidationErrors.self, from: data))?.detail.first?.loc.last
                : (try? Self.decode(ErrorDetail.self, from: data))?.detail
            throw APIError(status: response.statusCode, detail: detail)
        }
        return data
    }

    static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: data)
    }

    private struct ErrorDetail: Decodable {
        let detail: String
    }

    private struct ValidationErrors: Decodable {
        struct Issue: Decodable {
            let loc: [String]
        }

        let detail: [Issue]
    }

    private final class NoRedirects: NSObject, URLSessionTaskDelegate {
        func urlSession(
            _ session: URLSession,
            task: URLSessionTask,
            willPerformHTTPRedirection response: HTTPURLResponse,
            newRequest request: URLRequest,
            completionHandler: @escaping (URLRequest?) -> Void
        ) {
            completionHandler(nil)
        }
    }
}
