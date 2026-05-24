import Foundation

/// JSON-RPC client for the bridge Control API over Unix socket.
///
/// Thread-safe: all methods are ``async`` and the transport is ``Sendable``.
/// Token is read from disk once and cached.
final class ControlClient: Sendable {

    let botName: String

    private let transport: UnixSocketTransport
    private let token: String

    private static let bridgeHome: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".feishu-bridge")
    }()

    init(botName: String) throws {
        self.botName = botName
        let base = Self.bridgeHome
        let sockPath = base.appendingPathComponent("control-\(botName).sock").path
        self.transport = UnixSocketTransport(sockPath: sockPath)

        let tokenPath = base.appendingPathComponent("control-\(botName).token")
        guard let raw = try? String(contentsOf: tokenPath, encoding: .utf8) else {
            throw ClientError.tokenNotFound(tokenPath.path)
        }
        self.token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !self.token.isEmpty else {
            throw ClientError.tokenNotFound(tokenPath.path)
        }
    }

    // MARK: - Public API

    /// Raw JSON-RPC call.  Returns the decoded ``result`` dictionary.
    func call<T: Decodable>(
        _ method: String,
        params: [String: Any] = [:],
        as type: T.Type
    ) async throws -> T {
        let id = Int.random(in: 1...999_999)
        let request: [String: Any] = [
            "method": method,
            "params": params,
            "id": id,
            "token": token,
        ]

        let requestData = try JSONSerialization.data(withJSONObject: request)
        var payload = requestData
        payload.append(0x0A) // newline delimiter

        let responseData = try await transport.send(payload)

        // Parse the RPC envelope
        let envelope = try JSONDecoder().decode(RPCResponse.self, from: responseData)

        if let rpcError = envelope.error {
            throw ClientError.rpcError(rpcError.code, rpcError.message)
        }

        // Re-serialize `result` and decode into the target type
        guard let resultValue = envelope.result?.value else {
            throw ClientError.noResult
        }
        let resultData = try JSONSerialization.data(withJSONObject: resultValue)
        return try JSONDecoder().decode(T.self, from: resultData)
    }

    // MARK: - Typed helpers

    func health() async throws -> HealthResponse {
        try await call("health", as: HealthResponse.self)
    }

    func status() async throws -> BridgeStatusResponse {
        try await call("status", as: BridgeStatusResponse.self)
    }

    func sessions() async throws -> SessionsResponse {
        try await call("sessions", as: SessionsResponse.self)
    }

    func quota() async throws -> QuotaInfo {
        try await call("quota", as: QuotaInfo.self)
    }

    func logs(n: Int = 200, level: String = "INFO") async throws -> LogsResponse {
        try await call("logs", params: ["n": n, "level": level], as: LogsResponse.self)
    }

    func setProvider(_ name: String) async throws -> OkResponse {
        try await call("set_provider", params: ["name": name], as: OkResponse.self)
    }

    func setModel(_ name: String) async throws -> SetModelResponse {
        try await call("set_model", params: ["name": name], as: SetModelResponse.self)
    }

    func setAgent(_ type: String) async throws -> OkResponse {
        try await call("set_agent", params: ["type": type], as: OkResponse.self)
    }

    func stopSession(_ sessionKey: String) async throws -> OkResponse {
        try await call("stop_session", params: ["session_key": sessionKey], as: OkResponse.self)
    }

    func shutdown() async throws -> OkResponse {
        try await call("shutdown", as: OkResponse.self)
    }

    /// Quick connectivity check — does the socket accept connections?
    func probe() async -> Bool {
        await transport.probe()
    }

    // MARK: - Errors

    enum ClientError: LocalizedError {
        case tokenNotFound(String)
        case rpcError(Int, String)
        case noResult

        var errorDescription: String? {
            switch self {
            case .tokenNotFound(let path):
                return "Control token not found at \(path). Is bridge running?"
            case .rpcError(let code, let msg):
                return "RPC error \(code): \(msg)"
            case .noResult:
                return "RPC response missing result"
            }
        }
    }
}
