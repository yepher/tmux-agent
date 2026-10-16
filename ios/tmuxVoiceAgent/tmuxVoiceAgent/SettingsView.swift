import SwiftUI

struct SettingsView: View {
    @Bindable var settings: SettingsStore

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://your-mac.tailnet.ts.net:8787", text: $settings.tokenServerURL)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Token server key", text: $settings.tokenServerKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Token server (recommended)")
                } footer: {
                    Text("Run agent/token_server.py and enter its URL (HTTPS) and TOKEN_SERVER_KEY. When set, the API key and secret below are not used or needed.")
                }
                Section("LiveKit server") {
                    TextField("wss://your-host.livekit.cloud", text: $settings.url)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                Section {
                    TextField("API key", text: $settings.apiKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API secret", text: $settings.apiSecret)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Credentials (fallback)")
                } footer: {
                    Text("Only used when no token server is set. The API secret grants admin over the whole LiveKit project.")
                }
                .disabled(settings.usesTokenServer)
                Section("Room") {
                    TextField("Room name", text: $settings.room)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Your identity", text: $settings.identity)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Agent name", text: $settings.agentName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                Section {
                    Label(
                        settings.isComplete ? "Ready to connect" : "Missing fields",
                        systemImage: settings.isComplete
                            ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
                    )
                    .foregroundStyle(settings.isComplete ? .green : .orange)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
