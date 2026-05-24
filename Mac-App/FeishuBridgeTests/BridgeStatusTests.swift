import Foundation
import Testing

@testable import FeishuBridge

@Suite("BridgeStatus Decoding")
struct BridgeStatusTests {

    // MARK: - RPCResponse envelope

    @Test func rpcResponseWithResult() throws {
        let json = """
        {"result":{"ok":true},"id":1,"api_version":1,"capabilities":["logs","sessions"]}
        """.data(using: .utf8)!
        let resp = try JSONDecoder().decode(RPCResponse.self, from: json)
        #expect(resp.id == 1)
        #expect(resp.api_version == 1)
        #expect(resp.capabilities == ["logs", "sessions"])
        #expect(resp.error == nil)
    }

    @Test func rpcResponseWithError() throws {
        let json = """
        {"error":{"code":401,"message":"invalid token"},"id":1}
        """.data(using: .utf8)!
        let resp = try JSONDecoder().decode(RPCResponse.self, from: json)
        #expect(resp.error?.code == 401)
        #expect(resp.error?.message == "invalid token")
        #expect(resp.result == nil)
    }

    // MARK: - HealthResponse

    @Test func healthResponse() throws {
        let json = """
        {"ok":true}
        """.data(using: .utf8)!
        let h = try JSONDecoder().decode(HealthResponse.self, from: json)
        #expect(h.ok == true)
    }

    // MARK: - Full status decode

    @Test func statusResponseDecode() throws {
        let json = """
        {
          "api_version": 1,
          "capabilities": ["logs","quota","sessions","provider","model","agent","stop","tasks"],
          "version": "2026.05.24.6",
          "uptime_seconds": 3600.0,
          "agent": {
            "type": "claude",
            "provider": "default",
            "model": "claude-sonnet-4-20250514",
            "model_override": null,
            "command": "/Users/feir/.local/bin/claude"
          },
          "bot": {"name": "test-bot", "workspace": "/Users/feir/.claude"},
          "sessions": {"active_count": 2, "keys": ["bot:chat1:", "bot:chat2:t1"]},
          "queue": {"pending_total": 0, "active_sessions": 1},
          "quota": {
            "available": true,
            "stale": false,
            "windows": {
              "five_hour": {"utilization": 12.5, "resets_at": "2026-05-24T18:00:00Z"},
              "seven_day": {"utilization": 45.2, "resets_at": "2026-05-28T00:00:00Z"}
            },
            "extra_usage_enabled": true
          },
          "providers": ["default", "omlx"],
          "agents": ["claude", "codex"]
        }
        """.data(using: .utf8)!

        let status = try JSONDecoder().decode(BridgeStatusResponse.self, from: json)
        #expect(status.version == "2026.05.24.6")
        #expect(status.uptime_seconds == 3600.0)
        #expect(status.agent.type == "claude")
        #expect(status.agent.model_override == nil)
        #expect(status.sessions.active_count == 2)
        #expect(status.sessions.keys.count == 2)
        #expect(status.queue.pending_total == 0)
        #expect(status.quota.available == true)
        #expect(status.quota.windows?["five_hour"]?.utilization == 12.5)
        #expect(status.providers == ["default", "omlx"])
        #expect(status.agents == ["claude", "codex"])
    }

    // MARK: - Quota with unavailable data

    @Test func quotaUnavailable() throws {
        let json = """
        {"available":false,"stale":true}
        """.data(using: .utf8)!
        let q = try JSONDecoder().decode(QuotaInfo.self, from: json)
        #expect(q.available == false)
        #expect(q.stale == true)
        #expect(q.windows == nil)
    }

    // MARK: - SessionEntry identifiable

    @Test func sessionEntryId() throws {
        let json = """
        {"session_key":"bot:chat:thread","session_id":"sess-123"}
        """.data(using: .utf8)!
        let entry = try JSONDecoder().decode(SessionEntry.self, from: json)
        #expect(entry.id == "bot:chat:thread")
        #expect(entry.session_id == "sess-123")
    }

    // MARK: - SetModelResponse

    @Test func setModelResponse() throws {
        let json = """
        {"ok":true,"model":"claude-opus-4-6","cleared":false}
        """.data(using: .utf8)!
        let r = try JSONDecoder().decode(SetModelResponse.self, from: json)
        #expect(r.ok == true)
        #expect(r.model == "claude-opus-4-6")
        #expect(r.cleared == false)
    }
}
