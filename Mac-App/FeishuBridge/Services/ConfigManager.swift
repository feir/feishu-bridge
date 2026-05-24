import Foundation

/// Reads and writes bridge config files (config.json + .env).
///
/// File layout matches ``feishu_bridge.config._interactive_setup()``:
/// - ``~/.config/feishu-bridge/.env`` — ``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET``
/// - ``~/.config/feishu-bridge/config.json`` — bot + agent configuration
enum ConfigManager {

    // MARK: - Paths

    private static let configDir: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/feishu-bridge")
    }()

    static var configPath: URL { configDir.appendingPathComponent("config.json") }
    static var envPath: URL { configDir.appendingPathComponent(".env") }

    /// Whether a valid config already exists on disk.
    static var configExists: Bool {
        FileManager.default.fileExists(atPath: configPath.path)
    }

    // MARK: - Write (onboarding)

    /// Write .env file with App ID and Secret (0600 permissions).
    static func writeEnv(appId: String, appSecret: String) throws {
        try FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)

        let content = "FEISHU_APP_ID=\(appId)\nFEISHU_APP_SECRET=\(appSecret)\n"
        let path = envPath.path

        // Atomic write with 0600 permissions (matches bridge's _interactive_setup)
        let fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else {
            throw ConfigError.writeFailed(path, "open failed: \(errno)")
        }
        defer { close(fd) }

        guard let data = content.data(using: .utf8) else {
            throw ConfigError.writeFailed(path, "encoding failed")
        }
        let written = data.withUnsafeBytes { buf in
            write(fd, buf.baseAddress!, buf.count)
        }
        guard written == data.count else {
            throw ConfigError.writeFailed(path, "partial write")
        }
        // Enforce permissions even if file pre-existed
        chmod(path, 0o600)
    }

    /// Write config.json matching the bridge's expected structure.
    static func writeConfig(
        botName: String,
        workspace: String,
        agentType: String
    ) throws {
        try FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)

        let expandedWorkspace = (workspace as NSString).expandingTildeInPath

        let config: [String: Any] = [
            "bots": [
                [
                    "name": botName,
                    "app_id": "${FEISHU_APP_ID}",
                    "app_secret": "${FEISHU_APP_SECRET}",
                    "workspace": expandedWorkspace,
                    "allowed_users": ["*"],
                ]
            ],
            "agent": [
                "type": agentType,
                "command": agentType,  // "claude" or "codex"
                "timeout_seconds": 300,
            ],
        ]

        let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
        guard var jsonString = String(data: data, encoding: .utf8) else {
            throw ConfigError.writeFailed(configPath.path, "JSON encoding failed")
        }
        jsonString += "\n"
        try jsonString.write(to: configPath, atomically: true, encoding: .utf8)
    }

    // MARK: - Read (for Settings view, future use)

    /// Load the current config.json as raw dictionary.
    static func loadConfig() -> [String: Any]? {
        guard let data = try? Data(contentsOf: configPath),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return json
    }

    /// Load .env values as a dictionary.
    static func loadEnv() -> [String: String] {
        guard let content = try? String(contentsOf: envPath, encoding: .utf8) else {
            return [:]
        }
        var result: [String: String] = [:]
        for line in content.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1)
            if parts.count == 2 {
                result[String(parts[0])] = String(parts[1])
            }
        }
        return result
    }

    // MARK: - Agent CLI detection

    struct AgentCLIStatus {
        let claudePath: String?
        let codexPath: String?

        var hasAny: Bool { claudePath != nil || codexPath != nil }
        var hasClaude: Bool { claudePath != nil }
        var hasCodex: Bool { codexPath != nil }
    }

    /// Check which agent CLIs are available on PATH.
    static func detectAgentCLIs() -> AgentCLIStatus {
        AgentCLIStatus(
            claudePath: which("claude"),
            codexPath: which("codex")
        )
    }

    /// Detect the absolute path to the ``feishu-bridge`` command.
    static func detectBridgeCommand() -> String? {
        which("feishu-bridge")
    }

    private static func which(_ command: String) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        proc.arguments = [command]
        // Ensure common paths are on PATH for the subprocess
        var env = ProcessInfo.processInfo.environment
        let extra = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin",
        ]
        let current = env["PATH"] ?? "/usr/bin:/bin"
        env["PATH"] = (extra + [current]).joined(separator: ":")
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            return nil
        }
        guard proc.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (path?.isEmpty == false) ? path : nil
    }

    // MARK: - Errors

    enum ConfigError: LocalizedError {
        case writeFailed(String, String)

        var errorDescription: String? {
            switch self {
            case .writeFailed(let path, let reason):
                return "Failed to write \(path): \(reason)"
            }
        }
    }
}
