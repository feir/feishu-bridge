import Foundation

// MARK: - JSON-RPC envelope

/// Generic JSON-RPC response wrapper.
struct RPCResponse: Decodable {
    let result: AnyCodable?
    let error: RPCError?
    let id: Int?
    let api_version: Int?
    let capabilities: [String]?
}

struct RPCError: Decodable {
    let code: Int
    let message: String
}

// MARK: - Status response

struct BridgeStatusResponse: Decodable {
    let api_version: Int
    let capabilities: [String]
    let version: String
    let uptime_seconds: Double
    let agent: AgentInfo
    let bot: BotInfo
    let sessions: SessionsInfo
    let queue: QueueInfo
    let quota: QuotaInfo
    let providers: [String]
    let agents: [String]
}

struct AgentInfo: Decodable {
    let type: String
    let provider: String
    let model: String
    let model_override: String?
    let command: String?
}

struct BotInfo: Decodable {
    let name: String
    let workspace: String
}

struct SessionsInfo: Decodable {
    let active_count: Int
    let keys: [String]
}

struct QueueInfo: Decodable {
    let pending_total: Int
    let active_sessions: Int
}

struct QuotaInfo: Decodable {
    let available: Bool
    let stale: Bool
    let windows: [String: QuotaWindowInfo]?
    let extra_usage_enabled: Bool?
}

struct QuotaWindowInfo: Decodable {
    let utilization: Double
    let resets_at: String
}

// MARK: - Health response

struct HealthResponse: Decodable {
    let ok: Bool
}

// MARK: - Sessions response

struct SessionsResponse: Decodable {
    let sessions: [SessionEntry]
}

struct SessionEntry: Decodable, Identifiable {
    let session_key: String
    let session_id: String
    var id: String { session_key }
}

// MARK: - Log response

struct LogsResponse: Decodable {
    let entries: [LogEntry]
}

struct LogEntry: Decodable, Identifiable {
    let ts: Double
    let level: String
    let msg: String
    var id: Double { ts }
}

// MARK: - Write responses

struct OkResponse: Decodable {
    let ok: Bool
    let message: String?
}

struct SetModelResponse: Decodable {
    let ok: Bool
    let model: String
    let cleared: Bool
}

// MARK: - AnyCodable for flexible decoding

/// Minimal type-erased Codable for heterogeneous JSON-RPC `result` payloads.
struct AnyCodable: Decodable {
    let value: Any

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let dict = try? container.decode([String: AnyCodable].self) {
            value = dict.mapValues(\.value)
        } else if let arr = try? container.decode([AnyCodable].self) {
            value = arr.map(\.value)
        } else if let s = try? container.decode(String.self) {
            value = s
        } else if let d = try? container.decode(Double.self) {
            value = d
        } else if let b = try? container.decode(Bool.self) {
            value = b
        } else if container.decodeNil() {
            value = NSNull()
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "unsupported JSON type")
        }
    }
}
