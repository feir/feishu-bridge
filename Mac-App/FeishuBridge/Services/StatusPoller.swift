import Foundation
import SwiftUI

/// Polls the bridge Control API and publishes state to SwiftUI views.
///
/// - Health check every 2 s (lightweight, just connectivity + ``{ok: true}``)
/// - Full status every 10 s (provider, model, sessions, quota, etc.)
/// - Write-then-refresh: after any mutation call, immediately re-fetches status.
@MainActor
@Observable
final class StatusPoller {

    // MARK: - Published state

    private(set) var bridgeRunning = false
    private(set) var status: BridgeStatusResponse?
    private(set) var lastError: String?
    private(set) var capabilities: Set<String> = []
    private(set) var apiVersion: Int = 0

    /// Error from the last user-initiated action (set_provider, etc.)
    /// Cleared on next successful action or after 5 seconds.
    private(set) var actionError: String?

    /// Icon state derived from bridge status + quota.
    var iconState: IconState {
        guard bridgeRunning, let s = status else { return .stopped }
        if let windows = s.quota.windows,
           let fiveHour = windows["five_hour"],
           fiveHour.utilization > 80 {
            return .warning
        }
        return .running
    }

    enum IconState {
        case running, stopped, warning

        var systemImage: String {
            switch self {
            case .running: "circle.fill"
            case .stopped: "circle"
            case .warning: "exclamationmark.triangle.fill"
            }
        }

        var color: Color {
            switch self {
            case .running: .green
            case .stopped: .secondary
            case .warning: .yellow
            }
        }
    }

    // MARK: - Internal

    let botName: String
    private var client: ControlClient?
    private var healthTask: Task<Void, Never>?
    private var statusTask: Task<Void, Never>?

    init(botName: String) {
        self.botName = botName
    }

    // MARK: - Lifecycle

    func start() {
        healthTask?.cancel()
        statusTask?.cancel()

        healthTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                await self.pollHealth()
                try? await Task.sleep(for: .seconds(2))
            }
        }

        statusTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                await self.pollStatus()
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    func stop() {
        healthTask?.cancel()
        statusTask?.cancel()
        healthTask = nil
        statusTask = nil
    }

    /// Call after a mutation (set_provider, set_model, etc.) to refresh immediately.
    func refreshNow() {
        Task { await pollStatus() }
    }

    /// Execute a user-initiated action with error surfacing and auto-refresh.
    func performAction(_ action: @escaping (ControlClient) async throws -> Void) {
        Task {
            guard let c = ensureClient() else {
                actionError = "Bridge not connected"
                return
            }
            do {
                try await action(c)
                actionError = nil
                await pollStatus()
            } catch {
                actionError = error.localizedDescription
                // Auto-clear after 5 seconds
                Task {
                    try? await Task.sleep(for: .seconds(5))
                    if actionError == error.localizedDescription {
                        actionError = nil
                    }
                }
            }
        }
    }

    // MARK: - Polling

    private func ensureClient() -> ControlClient? {
        if client != nil { return client }
        client = try? ControlClient(botName: botName)
        return client
    }

    private func pollHealth() async {
        let wasRunning = bridgeRunning
        guard let c = ensureClient() else {
            let reachable = await UnixSocketTransport(
                sockPath: FileManager.default.homeDirectoryForCurrentUser
                    .appendingPathComponent(".feishu-bridge/control-\(botName).sock").path
            ).probe()
            bridgeRunning = reachable
            if !reachable { client = nil }
            if reachable && !wasRunning { await pollStatus() }
            return
        }

        do {
            let h = try await c.health()
            bridgeRunning = h.ok
            lastError = nil
            // Transition offline→online: fetch status now instead of waiting
            // for the next 10 s statusTask tick, which otherwise leaves the
            // header showing "Running" while the body reads "not running".
            if h.ok && !wasRunning { await pollStatus() }
        } catch {
            bridgeRunning = false
            client = nil  // force reconnect next cycle
        }
    }

    private func pollStatus() async {
        guard bridgeRunning, let c = ensureClient() else { return }

        do {
            let s = try await c.status()
            status = s
            capabilities = Set(s.capabilities)
            apiVersion = s.api_version
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }
}
