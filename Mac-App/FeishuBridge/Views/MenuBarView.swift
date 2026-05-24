import SwiftUI

struct MenuBarView: View {

    let poller: StatusPoller
    let processManager: ProcessManager

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            headerSection
            Divider()

            if let errorMsg = poller.actionError {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                    Text(errorMsg)
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 4)
                Divider()
            }

            if poller.bridgeRunning, let status = poller.status {
                if poller.capabilities.contains("provider") ||
                   poller.capabilities.contains("model") ||
                   poller.capabilities.contains("agent") {
                    controlSection(status)
                    Divider()
                }
                if poller.capabilities.contains("sessions") {
                    sessionSection(status)
                    Divider()
                }
                if poller.capabilities.contains("quota") {
                    quotaSection(status)
                    Divider()
                }
            } else {
                offlineSection
                Divider()
            }

            footerSection
        }
        .padding(.vertical, 8)
    }

    // MARK: - Header

    private var headerSection: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text("Feishu Bridge")
                        .font(.headline)
                    Image(systemName: poller.iconState.systemImage)
                        .foregroundStyle(poller.iconState.color)
                        .font(.caption)
                    Text(poller.bridgeRunning ? "Running" : "Stopped")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let status = poller.status {
                    Text("v\(status.version) · up \(formatUptime(status.uptime_seconds))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 8)
    }

    // MARK: - Controls

    private func controlSection(_ status: BridgeStatusResponse) -> some View {
        VStack(spacing: 6) {
            pickerRow("Provider", selection: status.agent.provider, options: status.providers) { name in
                poller.performAction { client in _ = try await client.setProvider(name) }
            }

            pickerRow("Agent", selection: status.agent.type, options: status.agents) { name in
                poller.performAction { client in _ = try await client.setAgent(name) }
            }

            HStack {
                Text("Model")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 60, alignment: .leading)
                Text(status.agent.model)
                    .font(.caption)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if status.agent.model_override != nil {
                    Text("(override)")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
                Spacer()
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
    }

    private func pickerRow(
        _ label: String,
        selection: String,
        options: [String],
        onChange: @escaping (String) -> Void
    ) -> some View {
        HStack {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 60, alignment: .leading)
            Picker("", selection: Binding(
                get: { selection },
                set: { onChange($0) }
            )) {
                ForEach(options, id: \.self) { Text($0).tag($0) }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .controlSize(.small)
        }
    }

    // MARK: - Sessions

    private func sessionSection(_ status: BridgeStatusResponse) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label("\(status.sessions.active_count) active", systemImage: "bubble.left.and.bubble.right")
                    .font(.caption)
                Spacer()
                Label("\(status.queue.pending_total) pending", systemImage: "tray")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
    }

    // MARK: - Quota

    private func quotaSection(_ status: BridgeStatusResponse) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if status.quota.available, let windows = status.quota.windows {
                ForEach(Array(windows.sorted(by: { $0.key < $1.key })), id: \.key) { key, window in
                    HStack(spacing: 8) {
                        Text(quotaLabel(key))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(width: 50, alignment: .leading)
                        ProgressView(value: min(window.utilization / 100.0, 1.0))
                            .tint(window.utilization > 80 ? .orange : .accentColor)
                        Text("\(Int(window.utilization))%")
                            .font(.caption)
                            .monospacedDigit()
                            .frame(width: 35, alignment: .trailing)
                    }
                }
            } else if status.quota.stale {
                Label("Quota data stale", systemImage: "clock.badge.questionmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Label("Quota unavailable", systemImage: "xmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
    }

    // MARK: - Offline

    private var offlineSection: some View {
        VStack(spacing: 10) {
            Image(systemName: "power.circle")
                .font(.title2)
                .foregroundStyle(.secondary)
            Text("Bridge is not running")
                .font(.callout)
                .foregroundStyle(.secondary)

            if processManager.isLoaded {
                Button("Restart") {
                    Task { try? await processManager.restart() }
                }
                .controlSize(.small)
            } else {
                Button("Start") {
                    try? processManager.start()
                }
                .controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
    }

    // MARK: - Footer

    @Environment(\.openWindow) private var openWindow

    private var footerSection: some View {
        HStack(spacing: 10) {
            Button {
                openWindow(id: "logs")
            } label: {
                Label("Logs", systemImage: "doc.text")
            }
            .buttonStyle(.borderless)

            Button {
                openWindow(id: "settings")
            } label: {
                Label("Settings", systemImage: "gearshape")
            }
            .buttonStyle(.borderless)

            Spacer()

            Button {
                poller.refreshNow()
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh")

            Button("Quit") {
                NSApplication.shared.terminate(nil)
            }
            .buttonStyle(.borderless)
            .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
    }

    // MARK: - Helpers

    private func formatUptime(_ seconds: Double) -> String {
        let h = Int(seconds) / 3600
        let m = (Int(seconds) % 3600) / 60
        if h > 0 { return "\(h)h\(m)m" }
        return "\(m)m"
    }

    private func quotaLabel(_ key: String) -> String {
        switch key {
        case "five_hour": "5h"
        case "seven_day": "7d"
        default: key
        }
    }
}
