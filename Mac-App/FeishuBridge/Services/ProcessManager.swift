import Foundation

/// Manages the bridge process lifecycle via ``launchctl``.
///
/// Uses ``LaunchctlHelper`` for plist generation and launchctl commands.
/// Uses ``ControlClient.shutdown()`` for graceful stop when the bridge is running.
@MainActor
@Observable
final class ProcessManager {

    let botName: String
    private(set) var isLoaded: Bool = false

    init(botName: String) {
        self.botName = botName
        refresh()
    }

    // MARK: - State

    func refresh() {
        isLoaded = LaunchctlHelper.isLoaded(botName: botName)
    }

    // MARK: - Actions

    /// Start the bridge via launchctl load.
    /// Requires the plist to already exist (created by onboarding wizard or ``installLaunchAgent``).
    func start() throws {
        try LaunchctlHelper.load(botName: botName)
        refresh()
    }

    /// Graceful stop: ask bridge to shut down via Control API, then unload the launch agent.
    func stop() async throws {
        // Try graceful shutdown first
        if let client = try? ControlClient(botName: botName) {
            _ = try? await client.shutdown()
            // Give the process a moment to exit
            try? await Task.sleep(for: .milliseconds(500))
        }
        // Unload prevents KeepAlive from restarting
        try LaunchctlHelper.unload(botName: botName)
        refresh()
    }

    /// Graceful restart: shutdown via Control API → launchd KeepAlive restarts automatically.
    /// If the bridge doesn't exit within 5 s, force unload + load.
    func restart() async throws {
        if let client = try? ControlClient(botName: botName) {
            _ = try? await client.shutdown()

            // Poll for the bridge to actually stop (up to 5 s)
            let sockPath = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".feishu-bridge/control-\(botName).sock").path
            let transport = UnixSocketTransport(sockPath: sockPath)
            var stopped = false
            for _ in 0..<10 {
                try? await Task.sleep(for: .milliseconds(500))
                if !(await transport.probe()) {
                    stopped = true
                    break
                }
            }
            if !stopped {
                // Force restart
                try? LaunchctlHelper.unload(botName: botName)
                try? await Task.sleep(for: .milliseconds(500))
                try LaunchctlHelper.load(botName: botName)
            }
            // Otherwise KeepAlive=true will have restarted automatically
        } else {
            // Fallback: unload + load
            try? LaunchctlHelper.unload(botName: botName)
            try? await Task.sleep(for: .milliseconds(500))
            try LaunchctlHelper.load(botName: botName)
        }
        refresh()
    }

    /// Install a LaunchAgent plist and load it.
    func installAndStart(bridgeCommand: String = "feishu-bridge", workspace: String = "~/.claude") throws {
        try LaunchctlHelper.installPlist(
            botName: botName,
            bridgeCommand: bridgeCommand,
            workspace: workspace
        )
        try LaunchctlHelper.load(botName: botName)
        refresh()
    }
}
