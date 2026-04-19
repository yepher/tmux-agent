import Foundation
import Observation

@Observable
final class SettingsStore {
    var url: String { didSet { Keychain.set(url, for: "url") } }
    var apiKey: String { didSet { Keychain.set(apiKey, for: "apiKey") } }
    var apiSecret: String { didSet { Keychain.set(apiSecret, for: "apiSecret") } }
    var room: String { didSet { Keychain.set(room, for: "room") } }
    var identity: String { didSet { Keychain.set(identity, for: "identity") } }

    init() {
        url = Keychain.get("url") ?? ""
        apiKey = Keychain.get("apiKey") ?? ""
        apiSecret = Keychain.get("apiSecret") ?? ""
        room = Keychain.get("room") ?? "tmux-agent"
        identity = Keychain.get("identity") ?? "mobile-user"
    }

    var isComplete: Bool {
        !url.isEmpty && !apiKey.isEmpty && !apiSecret.isEmpty
            && !room.isEmpty && !identity.isEmpty
    }
}
