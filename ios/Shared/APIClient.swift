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
        case (404, "entry not found"), (404, "attachment not found"): "找不到这条记录。"
        case (404, "conversation not found"), (404, "run not found"): "找不到这个对话。"
        case (404, _): "找不到该项目或待解问题。"
        case (409, "username already exists"): "该用户名已被使用。"
        case (409, "entry changed since base_revision"): "记录已在别处修改，请刷新后再编辑。"
        case (409, "entry for this submission was deleted"): "这条记录已在服务器上删除。"
        case (409, "processing already finished"): "这条记录已经整理完成。"
        case (409, "run already finished"): "这个问题已经回答完成。"
        case (409, _): "与服务器上的内容冲突，本机记录已保留。"
        case (411, _), (413, _): "图片超过 10 MB，无法上传。"
        case (415, _): "只支持 JPEG、PNG、HEIC 和 WebP 图片。"
        case (422, "username"): "用户名须为 3–32 位字母、数字、下划线、点或连字符。"
        case (422, "password"): "密码须为 8–128 个字符。"
        case (422, "invite_code"): "请输入有效长度的邀请码。"
        case (422, "name"): "项目名称须为 1–120 个字符。"
        case (422, "goal"): "项目目标不能超过 4000 个字符。"
        case (422, "text"): "内容须为 1–2000 个字符。"
        case (422, _): "请检查输入内容和长度。"
        case (429, _): "操作太频繁，请稍后再试。"
        default: "请求失败（HTTP \(status)）。"
        }
    }
}

struct APIClient {
    let baseURL: URL

    private static let session = makeSession(timeout: 15)
    /// Image uploads may take longer than ordinary requests on slow networks.
    private static let uploadSession = makeSession(timeout: 120)

    private static func makeSession(timeout: TimeInterval) -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.waitsForConnectivity = true
        config.timeoutIntervalForResource = timeout
        return URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }

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
        form: MultipartForm? = nil,
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
            encoder.dateEncodingStrategy = .iso8601
            request.httpBody = try encoder.encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        } else if let form {
            request.httpBody = form.body
            request.setValue("multipart/form-data; boundary=\(form.boundary)", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await (form == nil ? Self.session : Self.uploadSession).data(for: request)
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
        decoder.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            for formatter in dateFormatters {
                if let date = formatter.date(from: text) { return date }
            }
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Invalid date \(text)"))
        }
        return try decoder.decode(type, from: data)
    }

    private nonisolated(unsafe) static let dateFormatters: [ISO8601DateFormatter] = {
        let precise = ISO8601DateFormatter()
        precise.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return [precise, ISO8601DateFormatter()]
    }()

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

struct MultipartForm {
    let boundary = "Gatherloom-\(UUID().uuidString)"
    private(set) var body = Data()

    mutating func add(_ name: String, _ value: String) {
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
    }

    mutating func addFile(_ name: String, fileName: String, contentType: String, data: Data) {
        body.append(Data(
            "--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"; filename=\"\(fileName)\"\r\nContent-Type: \(contentType)\r\n\r\n".utf8
        ))
        body.append(data)
        body.append(Data("\r\n".utf8))
    }

    func finished() -> MultipartForm {
        var form = self
        form.body.append(Data("--\(boundary)--\r\n".utf8))
        return form
    }
}
