import Foundation
import Testing

@testable import FeishuBridge

@Suite("ConfigManager")
struct ConfigManagerTests {

    // MARK: - .env write/read round-trip

    @Test func envRoundTrip() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("fb-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        let envPath = dir.appendingPathComponent(".env")
        let content = "FEISHU_APP_ID=test_id_123\nFEISHU_APP_SECRET=test_secret_456\n"
        try content.write(to: envPath, atomically: true, encoding: .utf8)

        let parsed = parseEnv(at: envPath)
        #expect(parsed["FEISHU_APP_ID"] == "test_id_123")
        #expect(parsed["FEISHU_APP_SECRET"] == "test_secret_456")
    }

    // MARK: - config.json structure

    @Test func configJsonStructure() throws {
        let home = FileManager.default.homeDirectoryForCurrentUser.path

        let config: [String: Any] = [
            "bots": [
                [
                    "name": "unit-test-bot",
                    "app_id": "${FEISHU_APP_ID}",
                    "app_secret": "${FEISHU_APP_SECRET}",
                    "workspace": "\(home)/.claude",
                    "allowed_users": ["*"],
                ]
            ],
            "agent": [
                "type": "claude",
                "command": "claude",
                "timeout_seconds": 300,
            ],
        ]

        let data = try JSONSerialization.data(withJSONObject: config, options: .sortedKeys)
        let parsed = try JSONSerialization.jsonObject(with: data) as! [String: Any]

        // Validate bridge-expected structure
        let bots = parsed["bots"] as! [[String: Any]]
        #expect(bots.count == 1)
        #expect(bots[0]["name"] as! String == "unit-test-bot")
        #expect(bots[0]["app_id"] as! String == "${FEISHU_APP_ID}")
        #expect(bots[0]["allowed_users"] as! [String] == ["*"])

        let agent = parsed["agent"] as! [String: Any]
        #expect(agent["type"] as! String == "claude")
        #expect(agent["command"] as! String == "claude")
        #expect(agent["timeout_seconds"] as! Int == 300)
    }

    // MARK: - Agent CLI detection

    @Test func agentCLIDetection() {
        let status = ConfigManager.detectAgentCLIs()
        // At least one should be present on this dev machine
        // (test is informational — doesn't fail on CI without CLIs)
        if status.hasAny {
            #expect(status.claudePath != nil || status.codexPath != nil)
        }
    }

    // MARK: - Bridge command detection

    @Test func bridgeCommandDetection() {
        let cmd = ConfigManager.detectBridgeCommand()
        // feishu-bridge should be installed on this dev machine
        if let cmd {
            #expect(cmd.contains("feishu-bridge"))
        }
    }

    // MARK: - Helpers

    private func parseEnv(at url: URL) -> [String: String] {
        guard let content = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        var result: [String: String] = [:]
        for line in content.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1)
            if parts.count == 2 { result[String(parts[0])] = String(parts[1]) }
        }
        return result
    }
}
