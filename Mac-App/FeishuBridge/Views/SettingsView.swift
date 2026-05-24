import SwiftUI

/// Bridge settings window — hot-update items (via Control API) and cold-update
/// items (file edits requiring restart) displayed in separate sections.
struct SettingsView: View {

    let poller: StatusPoller
    let processManager: ProcessManager

    // Cold-update tracking
    @State private var needsRestart = false  // true only after a successful save
    @State private var credentialsDirty = false  // tracks unsaved edits

    // Credential fields (cold)
    @State private var appId = ""
    @State private var appSecret = ""
    @State private var savedAppId = ""  // snapshot at load time
    @State private var savedAppSecret = ""

    // Model override (hot)
    @State private var modelInput = ""


    var body: some View {
        VStack(spacing: 0) {
            Form {
                // ── Hot update section ───────────────────────────────
                Section {
                    if let status = poller.status {
                        LabeledContent("Provider") {
                            Picker("", selection: Binding(
                                get: { status.agent.provider },
                                set: { name in
                                    poller.performAction { c in _ = try await c.setProvider(name) }
                                }
                            )) {
                                ForEach(status.providers, id: \.self) { Text($0).tag($0) }
                            }
                            .labelsHidden()
                            .frame(width: 160)
                        }

                        LabeledContent("Agent") {
                            Picker("", selection: Binding(
                                get: { status.agent.type },
                                set: { type in
                                    poller.performAction { c in _ = try await c.setAgent(type) }
                                }
                            )) {
                                ForEach(status.agents, id: \.self) { Text($0).tag($0) }
                            }
                            .labelsHidden()
                            .frame(width: 160)
                        }

                        LabeledContent("Model") {
                            HStack {
                                TextField("model name or 'default'", text: $modelInput)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 160)
                                    .onSubmit { applyModel() }
                                Button("Apply") { applyModel() }
                                    .disabled(modelInput.isEmpty)
                                    .controlSize(.small)
                            }
                        }

                        if let override = status.agent.model_override {
                            LabeledContent("") {
                                HStack(spacing: 4) {
                                    Text("Override: \(override)")
                                        .font(.caption)
                                        .foregroundStyle(.orange)
                                    Button("Clear") {
                                        poller.performAction { c in _ = try await c.setModel("default") }
                                    }
                                    .controlSize(.mini)
                                }
                            }
                        }
                    } else {
                        Text("Bridge not connected")
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Label("热更新（无需重启）", systemImage: "bolt.fill")
                }

                // ── Cold update section ──────────────────────────────
                Section {
                    LabeledContent("App ID") {
                        TextField("", text: $appId)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 220)
                            .onChange(of: appId) { credentialsDirty = (appId != savedAppId || appSecret != savedAppSecret) }
                    }
                    LabeledContent("App Secret") {
                        SecureField("", text: $appSecret)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 220)
                            .onChange(of: appSecret) { credentialsDirty = (appId != savedAppId || appSecret != savedAppSecret) }
                    }

                    LabeledContent("") {
                        HStack(spacing: 8) {
                            Button("保存凭证") { saveCredentials() }
                                .controlSize(.small)
                                .disabled(!credentialsDirty || appId.isEmpty || appSecret.isEmpty)
                            if credentialsDirty && (appId.isEmpty || appSecret.isEmpty) {
                                Text("两个字段都必须填写")
                                    .font(.caption2)
                                    .foregroundStyle(.red)
                            }
                        }
                    }
                } header: {
                    Label("凭证（需重启）", systemImage: "key.fill")
                }


                // ── Advanced ─────────────────────────────────────────
                Section {
                    LabeledContent("配置文件") {
                        Button("在编辑器中打开") {
                            NSWorkspace.shared.open(ConfigManager.configPath)
                        }
                        .controlSize(.small)
                    }

                    LabeledContent("plist") {
                        Button("在编辑器中打开") {
                            NSWorkspace.shared.open(
                                LaunchctlHelper.plistPath(botName: poller.botName)
                            )
                        }
                        .controlSize(.small)
                    }
                } header: {
                    Label("高级", systemImage: "gearshape.2")
                }
            }
            .formStyle(.grouped)

            // ── Restart banner (only after successful save) ──────
            if needsRestart {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text("凭证已保存，需重启 Bridge 生效")
                        .font(.callout)
                    Spacer()
                    Button("重启") {
                        Task {
                            do {
                                try await processManager.restart()
                                needsRestart = false
                            } catch {
                                poller.performAction { _ in throw error }
                            }
                        }
                    }
                    .controlSize(.small)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(.bar)
            }

        }
        .frame(width: 480, height: 420)
        .onAppear { loadCurrentCredentials() }
    }

    // MARK: - Actions

    private func applyModel() {
        let value = modelInput.trimmingCharacters(in: .whitespaces)
        guard !value.isEmpty else { return }
        poller.performAction { c in _ = try await c.setModel(value) }
        modelInput = ""
    }

    private func loadCurrentCredentials() {
        let env = ConfigManager.loadEnv()
        appId = env["FEISHU_APP_ID"] ?? ""
        appSecret = env["FEISHU_APP_SECRET"] ?? ""
        savedAppId = appId
        savedAppSecret = appSecret
        credentialsDirty = false
        needsRestart = false
    }


    private func saveCredentials() {
        guard !appId.isEmpty, !appSecret.isEmpty else { return }
        do {
            try ConfigManager.writeEnv(appId: appId, appSecret: appSecret)
            savedAppId = appId
            savedAppSecret = appSecret
            credentialsDirty = false
            needsRestart = true
        } catch {
            poller.performAction { _ in throw error }
        }
    }
}
