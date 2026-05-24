import SwiftUI

/// Bridge log viewer window.
///
/// - **Bridge running** → fetches via ``logs`` RPC (2 s polling)
/// - **Bridge stopped** → reads stderr log file from disk
struct LogViewer: View {

    let poller: StatusPoller

    @State private var entries: [LogEntry] = []
    @State private var selectedLevel = "INFO"
    @State private var searchText = ""
    @State private var pollTask: Task<Void, Never>?
    @State private var fetchError: String?

    private static let levels = ["DEBUG", "INFO", "WARNING", "ERROR"]

    private static let stderrLogDir: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".feishu-bridge")
    }()

    var body: some View {
        VStack(spacing: 0) {
            // Toolbar
            HStack(spacing: 8) {
                Picker("Level", selection: $selectedLevel) {
                    ForEach(Self.levels, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.segmented)
                .frame(width: 280)

                TextField("Search", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 120)

                Spacer()

                Button {
                    Task { await refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh")

                if poller.bridgeRunning {
                    Label("Live", systemImage: "circle.fill")
                        .font(.caption)
                        .foregroundStyle(.green)
                } else {
                    Label("File", systemImage: "doc.text")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(8)

            Divider()

            if let error = fetchError {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text(error)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                    Spacer()
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
            }

            // Log entries
            ScrollViewReader { proxy in
                List(filteredEntries) { entry in
                    LogEntryRow(entry: entry)
                        .id(entry.ts)
                        .listRowSeparator(.hidden)
                }
                .listStyle(.plain)
                .font(.system(.caption, design: .monospaced))
                .onChange(of: entries.count) {
                    // Auto-scroll to bottom on new entries
                    if let last = filteredEntries.last {
                        proxy.scrollTo(last.ts, anchor: .bottom)
                    }
                }
            }
        }
        .frame(minWidth: 600, minHeight: 400)
        .onAppear { startPolling() }
        .onDisappear { stopPolling() }
        .onChange(of: selectedLevel) {
            Task { await refresh() }
        }
    }

    // MARK: - Filtering

    private var filteredEntries: [LogEntry] {
        guard !searchText.isEmpty else { return entries }
        let query = searchText.lowercased()
        return entries.filter { $0.msg.lowercased().contains(query) }
    }

    // MARK: - Data

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func refresh() async {
        if poller.bridgeRunning {
            await refreshFromRPC()
        } else {
            refreshFromFile()
        }
    }

    private func refreshFromRPC() async {
        guard let client = try? ControlClient(botName: poller.botName) else {
            fetchError = "Cannot connect to bridge"
            return
        }
        do {
            let response = try await client.logs(n: 500, level: selectedLevel)
            entries = response.entries
            fetchError = nil
        } catch {
            fetchError = "Log fetch failed: \(error.localizedDescription)"
            // Keep existing entries visible but marked stale
        }
    }

    private func refreshFromFile() {
        let logPath = Self.stderrLogDir
            .appendingPathComponent("bridge-\(poller.botName).stderr.log")

        // Tail-read: seek to last ~64KB instead of loading the entire file
        guard let fh = FileHandle(forReadingAtPath: logPath.path) else {
            entries = []
            fetchError = nil
            return
        }
        defer { try? fh.close() }

        let tailSize: UInt64 = 65_536
        let fileSize = fh.seekToEndOfFile()
        if fileSize > tailSize {
            fh.seek(toFileOffset: fileSize - tailSize)
        } else {
            fh.seek(toFileOffset: 0)
        }
        guard let data = try? fh.readToEnd(),
              let content = String(data: data, encoding: .utf8) else {
            entries = []
            return
        }

        let levelThreshold = Self.levelValue(selectedLevel)
        let lines = content.split(separator: "\n", omittingEmptySubsequences: true).suffix(500)

        entries = lines.compactMap { line -> LogEntry? in
            let str = String(line)
            for level in Self.levels {
                if str.contains(" \(level) ") {
                    guard Self.levelValue(level) >= levelThreshold else { return nil }
                    return LogEntry(ts: Date().timeIntervalSince1970, level: level, msg: str)
                }
            }
            if levelThreshold <= Self.levelValue("INFO") {
                return LogEntry(ts: Date().timeIntervalSince1970, level: "INFO", msg: str)
            }
            return nil
        }
        fetchError = nil
    }

    private static func levelValue(_ level: String) -> Int {
        switch level {
        case "DEBUG": 10
        case "INFO": 20
        case "WARNING": 30
        case "ERROR": 40
        default: 20
        }
    }
}

// MARK: - Log entry row

private struct LogEntryRow: View {
    let entry: LogEntry

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            Text(entry.level)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(levelColor)
                .frame(width: 55, alignment: .leading)
            Text(entry.msg)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
        }
    }

    private var levelColor: Color {
        switch entry.level {
        case "ERROR": .red
        case "WARNING": .orange
        case "DEBUG": .secondary
        default: .primary
        }
    }
}
