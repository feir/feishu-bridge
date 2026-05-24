import SwiftUI

@main
struct FeishuBridgeApp: App {

    /// Resolve the active bot name: UserDefaults (set by onboarding) → hostname fallback.
    private static func resolveBotName() -> String {
        if let saved = UserDefaults.standard.string(forKey: "botName"), !saved.isEmpty {
            return saved
        }
        let host = ProcessInfo.processInfo.hostName
            .components(separatedBy: ".").first ?? "mac"
        return "feishu-bridge-\(host)"
    }

    @State private var poller = StatusPoller(botName: resolveBotName())
    @State private var processManager = ProcessManager(botName: resolveBotName())
    @State private var showOnboarding = !ConfigManager.configExists

    var body: some Scene {
        MenuBarExtra {
            if showOnboarding {
                OnboardingWizard(
                    isPresented: $showOnboarding,
                    botName: Self.resolveBotName()
                ) { configuredBotName in
                    UserDefaults.standard.set(configuredBotName, forKey: "botName")
                    poller.stop()
                    poller = StatusPoller(botName: configuredBotName)
                    poller.start()
                    processManager = ProcessManager(botName: configuredBotName)
                }
                .frame(width: 420, height: 380)
            } else {
                MenuBarView(poller: poller, processManager: processManager)
                    .frame(width: 320)
            }
        } label: {
            Label("Feishu Bridge", systemImage: poller.iconState.systemImage)
                .foregroundStyle(poller.iconState.color)
        }
        .menuBarExtraStyle(.window)

        Window("Settings", id: "settings") {
            SettingsView(poller: poller, processManager: processManager)
        }
        .defaultSize(width: 480, height: 450)

        Window("Logs", id: "logs") {
            LogViewer(poller: poller)
        }
        .defaultSize(width: 700, height: 500)
    }

    init() {
        DispatchQueue.main.async { [poller] in
            poller.start()
        }
    }
}
