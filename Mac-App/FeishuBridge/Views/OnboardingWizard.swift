import SwiftUI

/// Four-step onboarding wizard for first-time bridge configuration.
///
/// Step 1: Environment check (agent CLI detection)
/// Step 2: Feishu credentials (App ID / Secret + API validation)
/// Step 3: Bot configuration (name, workspace, agent type)
/// Step 4: Completion (write files, install LaunchAgent, start bridge)
struct OnboardingWizard: View {

    @Binding var isPresented: Bool
    let botName: String
    var onComplete: (String) -> Void = { _ in }

    @State private var step = 1

    // Step 1
    @State private var cliStatus: ConfigManager.AgentCLIStatus?
    @State private var checking = false

    // Step 2
    @State private var appId = ""
    @State private var appSecret = ""
    @State private var validating = false
    @State private var credentialError: String?

    // Step 3
    @State private var editBotName: String = ""
    @State private var workspace = "~/.claude"
    @State private var agentType = "claude"

    // Step 4
    @State private var setupError: String?
    @State private var setupDone = false

    var body: some View {
        VStack(spacing: 0) {
            // Progress indicator
            HStack(spacing: 4) {
                ForEach(1...4, id: \.self) { s in
                    Circle()
                        .fill(s <= step ? Color.accentColor : Color.secondary.opacity(0.3))
                        .frame(width: 8, height: 8)
                }
            }
            .padding(.top, 16)
            .padding(.bottom, 12)

            Divider()

            // Step content
            Group {
                switch step {
                case 1: step1View
                case 2: step2View
                case 3: step3View
                case 4: step4View
                default: EmptyView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(20)
        }
        .frame(width: 420, height: 380)
        .onAppear {
            editBotName = botName
            checkEnvironment()
        }
    }

    // MARK: - Step 1: Environment Check

    private var step1View: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("环境检查")
                .font(.title3.bold())

            if checking {
                ProgressView("检测 Agent CLI...")
            } else if let cli = cliStatus {
                VStack(alignment: .leading, spacing: 8) {
                    checkRow("macOS \(ProcessInfo.processInfo.operatingSystemVersionString)", ok: true)
                    checkRow("claude CLI", ok: cli.hasClaude, detail: cli.claudePath)
                    checkRow("codex CLI", ok: cli.hasCodex, detail: cli.codexPath, optional: true)
                }

                if !cli.hasAny {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("需要至少安装一个 Agent CLI：")
                            .font(.callout)
                            .foregroundStyle(.red)
                        Link("安装 Claude Code →", destination: URL(string: "https://docs.anthropic.com/en/docs/claude-code/overview")!)
                            .font(.callout)
                        Link("安装 Codex →", destination: URL(string: "https://github.com/openai/codex")!)
                            .font(.callout)
                    }
                    .padding(.top, 4)
                }
            }

            Spacer()

            HStack {
                Spacer()
                Button("继续 →") { step = 2 }
                    .disabled(cliStatus?.hasAny != true)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    // MARK: - Step 2: Feishu Credentials

    private var step2View: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("飞书机器人凭证")
                .font(.title3.bold())

            Text("在飞书开放平台创建机器人后获取：")
                .font(.callout)
                .foregroundStyle(.secondary)

            Link("打开飞书开放平台 →",
                 destination: URL(string: "https://open.feishu.cn/page/openclaw?form=multiAgent")!)
                .font(.callout)

            TextField("App ID", text: $appId)
                .textFieldStyle(.roundedBorder)
            SecureField("App Secret", text: $appSecret)
                .textFieldStyle(.roundedBorder)

            if let error = credentialError {
                Label(error, systemImage: "xmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Spacer()

            HStack {
                Button("← 上一步") { step = 1 }
                Spacer()
                if validating {
                    ProgressView()
                        .controlSize(.small)
                }
                Button("验证凭证 →") { validateCredentials() }
                    .disabled(appId.isEmpty || appSecret.isEmpty || validating)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    // MARK: - Step 3: Configuration

    private var step3View: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("配置")
                .font(.title3.bold())

            HStack {
                Text("Bot 名称")
                    .frame(width: 80, alignment: .leading)
                TextField("", text: $editBotName)
                    .textFieldStyle(.roundedBorder)
            }

            HStack {
                Text("工作目录")
                    .frame(width: 80, alignment: .leading)
                TextField("", text: $workspace)
                    .textFieldStyle(.roundedBorder)
            }

            HStack {
                Text("Agent 类型")
                    .frame(width: 80, alignment: .leading)
                Picker("", selection: $agentType) {
                    if cliStatus?.hasClaude == true { Text("Claude Code").tag("claude") }
                    if cliStatus?.hasCodex == true { Text("Codex").tag("codex") }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
            }

            Spacer()

            HStack {
                Button("← 上一步") { step = 2 }
                Spacer()
                Button("完成配置 →") { performSetup() }
                    .disabled(editBotName.isEmpty || workspace.isEmpty)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    // MARK: - Step 4: Completion

    private var step4View: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("配置完成")
                .font(.title3.bold())

            if let error = setupError {
                Label(error, systemImage: "xmark.circle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
            } else if setupDone {
                VStack(alignment: .leading, spacing: 8) {
                    checkRow("凭证已写入 \(ConfigManager.envPath.path)", ok: true)
                    checkRow("配置已写入 \(ConfigManager.configPath.path)", ok: true)
                    checkRow("launchd 服务已注册", ok: true)
                }

                Text("Control token 由 bridge 首次启动时自动生成。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
            } else {
                ProgressView("正在写入配置...")
            }

            Spacer()

            if setupDone {
                HStack {
                    Spacer()
                    Button("启动 Bridge") {
                        isPresented = false
                        onComplete(editBotName)
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
        }
    }

    // MARK: - Helpers

    private func checkRow(_ text: String, ok: Bool, detail: String? = nil, optional: Bool = false) -> some View {
        HStack(spacing: 6) {
            Image(systemName: ok ? "checkmark.circle.fill" : (optional ? "minus.circle" : "xmark.circle.fill"))
                .foregroundStyle(ok ? .green : (optional ? .secondary : .red))
                .font(.body)
            VStack(alignment: .leading, spacing: 1) {
                Text(text)
                    .font(.callout)
                if let detail {
                    Text(detail)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Actions

    private func checkEnvironment() {
        checking = true
        DispatchQueue.global(qos: .userInitiated).async {
            let result = ConfigManager.detectAgentCLIs()
            DispatchQueue.main.async {
                cliStatus = result
                checking = false
                // Default agent type based on what's available
                if result.hasClaude {
                    agentType = "claude"
                } else if result.hasCodex {
                    agentType = "codex"
                }
            }
        }
    }

    private func validateCredentials() {
        validating = true
        credentialError = nil
        Task {
            let result = await CredentialValidator.validate(appId: appId, appSecret: appSecret)
            validating = false
            if result.valid {
                step = 3
            } else {
                credentialError = result.error ?? "验证失败"
            }
        }
    }

    private func performSetup() {
        step = 4
        setupError = nil
        setupDone = false

        Task {
            do {
                // 1. Write .env
                try ConfigManager.writeEnv(appId: appId, appSecret: appSecret)

                // 2. Write config.json
                try ConfigManager.writeConfig(
                    botName: editBotName,
                    workspace: workspace,
                    agentType: agentType
                )

                // 3. Detect bridge command path
                let bridgeCmd = ConfigManager.detectBridgeCommand() ?? "feishu-bridge"

                // 4. Install LaunchAgent plist
                try LaunchctlHelper.installPlist(
                    botName: editBotName,
                    bridgeCommand: bridgeCmd,
                    workspace: workspace
                )

                // 5. Load the LaunchAgent to actually start the bridge
                try LaunchctlHelper.load(botName: editBotName)

                setupDone = true
            } catch {
                setupError = error.localizedDescription
            }
        }
    }
}
