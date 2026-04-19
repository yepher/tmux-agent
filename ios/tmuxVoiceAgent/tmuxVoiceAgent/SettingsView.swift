import SwiftUI

struct SettingsView: View {
    @Bindable var settings: SettingsStore

    var body: some View {
        NavigationStack {
            Form {
                Section("LiveKit server") {
                    TextField("wss://your-host.livekit.cloud", text: $settings.url)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                Section("Credentials") {
                    TextField("API key", text: $settings.apiKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API secret", text: $settings.apiSecret)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                Section("Room") {
                    TextField("Room name", text: $settings.room)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Your identity", text: $settings.identity)
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
