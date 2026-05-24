import Foundation

/// Shell-out wrapper for ``launchctl`` and plist management.
enum LaunchctlHelper {

    static func label(botName: String) -> String {
        "com.feishu-bridge.\(botName)"
    }

    static func plistPath(botName: String) -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.feishu-bridge.\(botName).plist")
    }

    // MARK: - Query

    /// Check if the launch agent is loaded (listed by launchctl).
    static func isLoaded(botName: String) -> Bool {
        let (_, out) = run("/bin/launchctl", args: ["list"])
        return out.contains(label(botName: botName))
    }

    // MARK: - Control

    static func load(botName: String) throws {
        let path = plistPath(botName: botName).path
        let (status, output) = run("/bin/launchctl", args: ["load", "-w", path])
        if status != 0 {
            throw LaunchctlError.commandFailed("load", output)
        }
    }

    static func unload(botName: String) throws {
        let path = plistPath(botName: botName).path
        let (status, output) = run("/bin/launchctl", args: ["unload", path])
        if status != 0 {
            throw LaunchctlError.commandFailed("unload", output)
        }
    }

    // MARK: - Plist generation

    /// Generate and write a LaunchAgent plist for the given bot.
    static func installPlist(
        botName: String,
        bridgeCommand: String = "feishu-bridge",
        workspace: String = "~/.claude"
    ) throws {
        let home = FileManager.default.homeDirectoryForCurrentUser.path

        // NOTE: Do NOT set FEISHU_BRIDGE_BG_HOME here. The bridge defaults to
        // ~/.feishu-bridge/ which is where the app looks for control-<bot>.sock
        // and control-<bot>.token. Setting a custom bg_home would break the
        // control-path contract. Multi-bot isolation requires the user to set
        // FEISHU_BRIDGE_BG_HOME themselves and update the app accordingly.
        let plist: [String: Any] = [
            "Label": label(botName: botName),
            "ProgramArguments": [bridgeCommand, "--bot", botName],
            "KeepAlive": true,
            "RunAtLoad": true,
            "StandardErrorPath": "\(home)/.feishu-bridge/bridge-\(botName).stderr.log",
            "StandardOutPath": "/dev/null",
            "EnvironmentVariables": [
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:\(home)/.local/bin",
            ],
            "WorkingDirectory": (workspace as NSString).expandingTildeInPath,
        ]

        let url = plistPath(botName: botName)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let data = try PropertyListSerialization.data(
            fromPropertyList: plist, format: .xml, options: 0
        )
        try data.write(to: url, options: .atomic)
    }

    // MARK: - Private

    private static func run(_ cmd: String, args: [String]) -> (Int32, String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: cmd)
        proc.arguments = args
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            return (-1, error.localizedDescription)
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return (proc.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    enum LaunchctlError: LocalizedError {
        case commandFailed(String, String)

        var errorDescription: String? {
            switch self {
            case .commandFailed(let cmd, let output):
                return "launchctl \(cmd) failed: \(output)"
            }
        }
    }
}
