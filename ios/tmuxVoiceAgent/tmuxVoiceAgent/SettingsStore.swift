import Foundation
import Observation

@Observable
final class SettingsStore {
    var url: String { didSet { Keychain.set(url, for: "url") } }
    var apiKey: String { didSet { Keychain.set(apiKey, for: "apiKey") } }
    var apiSecret: String { didSet { Keychain.set(apiSecret, for: "apiSecret") } }
    var room: String { didSet { Keychain.set(room, for: "room") } }
    var identity: String { didSet { Keychain.set(identity, for: "identity") } }
    /// Must match the agent's TMUX_AGENT_NAME (explicit dispatch).
    var agentName: String { didSet { Keychain.set(agentName, for: "agentName") } }
    /// Optional `agent/token_server.py` endpoint. When set, the app fetches
    /// tokens from it and the API key/secret fields are not used.
    var tokenServerURL: String { didSet { Keychain.set(tokenServerURL, for: "tokenServerURL") } }
    var tokenServerKey: String { didSet { Keychain.set(tokenServerKey, for: "tokenServerKey") } }

    init() {
        url = Keychain.get("url") ?? ""
        apiKey = Keychain.get("apiKey") ?? ""
        apiSecret = Keychain.get("apiSecret") ?? ""
        room = Keychain.get("room") ?? "tmux-agent"
        identity = Keychain.get("identity") ?? "mobile-user"
        agentName = Keychain.get("agentName") ?? "tmux-agent"
        tokenServerURL = Keychain.get("tokenServerURL") ?? ""
        tokenServerKey = Keychain.get("tokenServerKey") ?? ""
    }

    var usesTokenServer: Bool { !tokenServerURL.isEmpty }

    var isComplete: Bool {
        guard !room.isEmpty, !identity.isEmpty else { return false }
        if usesTokenServer {
            return !tokenServerKey.isEmpty
        }
        return !url.isEmpty && !apiKey.isEmpty && !apiSecret.isEmpty
    }
}
