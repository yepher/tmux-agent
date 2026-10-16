import Foundation
import LiveKit
import Observation

/// Thin @Observable wrapper around LiveKit's Room. Tracks connection state and
/// the agent's screen-share video track so SwiftUI can render it.
@Observable
@MainActor
final class RoomConnection {
    enum State: Equatable {
        case disconnected
        case connecting
        case connected
        case failed(String)
    }

    private(set) var state: State = .disconnected
    private(set) var screenShareTrack: VideoTrack?
    private(set) var micEnabled: Bool = false
    private(set) var micError: String?
    private(set) var proxy: ProxyClient?
    private(set) var sessions: [SessionController.SessionInfo] = []
    private(set) var currentSession: String?

    let room: Room

    private var delegateAdapter: RoomDelegateAdapter?
    private var sessionController: SessionController?

    init() {
        room = Room()
        let adapter = RoomDelegateAdapter()
        adapter.owner = self
        delegateAdapter = adapter
        room.add(delegate: adapter)
        let p = ProxyClient(room: room)
        self.proxy = p
        self.sessionController = SessionController(room: room)
        Task { await p.registerResponseHandler() }
    }

    func connect(settings: SettingsStore) async {
        guard settings.isComplete else {
            state = .failed("Fill in every settings field first.")
            return
        }
        state = .connecting
        do {
            let serverURL: String
            let token: String
            if settings.usesTokenServer {
                // Preferred: the token server mints the token (with agent
                // dispatch) so the API secret stays off the phone.
                let creds = try await TokenServerClient.fetch(
                    serverURL: settings.tokenServerURL,
                    key: settings.tokenServerKey,
                    identity: settings.identity,
                    room: settings.room
                )
                serverURL = creds.url.isEmpty ? settings.url : creds.url
                token = creds.token
            } else {
                // Append the current epoch-seconds so every connect creates a
                // fresh room. LiveKit auto-creates rooms on first join, so no
                // server-side pre-provisioning needed.
                let uniqueRoom = "\(settings.room)-\(Int(Date().timeIntervalSince1970))"
                serverURL = settings.url
                token = try TokenBuilder.build(
                    apiKey: settings.apiKey,
                    apiSecret: settings.apiSecret,
                    identity: settings.identity,
                    room: uniqueRoom,
                    agentName: settings.agentName
                )
            }
            try await room.connect(url: serverURL, token: token)
            state = .connected
            // Rescan any tracks that may already be subscribed.
            refreshScreenShareTrack()
            // Fetch the initial session list so the menu is populated.
            await refreshSessions()
        } catch {
            state = .failed("\(error)")
        }
    }

    func disconnect() async {
        // Fail in-flight tunnel requests so webview loads don't hang.
        await proxy?.failAll(URLError(.networkConnectionLost))
        await room.disconnect()
        state = .disconnected
        screenShareTrack = nil
        micEnabled = false
        micError = nil
        sessions = []
        currentSession = nil
    }

    /// Re-fetch the tmux session list from the agent.
    func refreshSessions() async {
        guard state == .connected, let sc = sessionController else { return }
        do {
            let list = try await sc.list()
            sessions = list.sessions
            currentSession = list.current
        } catch {
            print("RoomConnection: refreshSessions failed: \(error)")
        }
    }

    /// Switch the agent's active tmux session (creating it if missing).
    /// Silently no-ops if called while disconnected.
    func switchSession(name: String) async {
        guard let sc = sessionController else { return }
        do {
            let now = try await sc.switchTo(name: name)
            currentSession = now
            await refreshSessions()
        } catch {
            print("RoomConnection: switchSession(\(name)) failed: \(error)")
        }
    }

    func setMic(_ enabled: Bool) async {
        // Mic publish failure must NOT tear the room down — it just means the
        // audio track couldn't be published (common in simulator). Surface it
        // via `micError` so the UI can show a warning, keep the connection up.
        do {
            try await room.localParticipant.setMicrophone(enabled: enabled)
            micEnabled = enabled
            micError = nil
        } catch {
            micError = "Mic toggle failed: \(error.localizedDescription)"
            print("RoomConnection: \(micError!)")
            // Reset the toggle to reflect reality.
            micEnabled = false
        }
    }

    /// Prefer the agent's screen-share track; fall back to any subscribed
    /// video track (e.g. `TMUX_COMPAT_TRACK_SOURCE=camera` on the agent).
    fileprivate func refreshScreenShareTrack() {
        var fallback: VideoTrack?
        for participant in room.remoteParticipants.values {
            for pub in participant.videoTracks {
                guard let t = pub.track as? VideoTrack else { continue }
                if pub.source == .screenShareVideo {
                    screenShareTrack = t
                    return
                }
                if fallback == nil { fallback = t }
            }
        }
        screenShareTrack = fallback
    }

    /// The agent left: anything waiting on the tunnel will never be answered.
    fileprivate func participantLeft(identity: String) {
        refreshScreenShareTrack()
        if identity.lowercased().hasPrefix("agent") {
            Task { await proxy?.failAll(URLError(.networkConnectionLost)) }
        }
    }

    /// The agent publishes `tmux.session` whenever the active session
    /// changes — including switches made by voice — so keep the menu in sync.
    fileprivate func attributesChanged(_ attributes: [String: String]) {
        guard let name = attributes["tmux.session"] else { return }
        currentSession = name
        Task { await refreshSessions() }
    }
}

/// RoomDelegate is class-bound; we keep it separate from @Observable to avoid
/// forcing the whole model into a nonisolated context. Delegate methods may be
/// called off the main actor, so we hop back to MainActor before touching the
/// observable model.
private final class RoomDelegateAdapter: NSObject, RoomDelegate, @unchecked Sendable {
    nonisolated(unsafe) weak var owner: RoomConnection?

    func room(_ room: Room,
              participant: RemoteParticipant,
              didSubscribeTrack publication: RemoteTrackPublication) {
        Task { @MainActor in owner?.refreshScreenShareTrack() }
    }

    func room(_ room: Room,
              participant: RemoteParticipant,
              didUnsubscribeTrack publication: RemoteTrackPublication) {
        Task { @MainActor in owner?.refreshScreenShareTrack() }
    }

    func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        let identity = participant.identity?.stringValue ?? ""
        Task { @MainActor in owner?.participantLeft(identity: identity) }
    }

    func room(_ room: Room,
              participant: Participant,
              didUpdateAttributes attributes: [String: String]) {
        Task { @MainActor in owner?.attributesChanged(attributes) }
    }
}
