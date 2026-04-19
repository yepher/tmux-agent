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
            // Append the current epoch-seconds so every connect creates a
            // fresh room. LiveKit auto-creates rooms on first join, so no
            // server-side pre-provisioning needed.
            let uniqueRoom = "\(settings.room)-\(Int(Date().timeIntervalSince1970))"
            let token = try TokenBuilder.build(
                apiKey: settings.apiKey,
                apiSecret: settings.apiSecret,
                identity: settings.identity,
                room: uniqueRoom
            )
            try await room.connect(url: settings.url, token: token)
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

    fileprivate func refreshScreenShareTrack() {
        for participant in room.remoteParticipants.values {
            for pub in participant.videoTracks {
                if pub.source == .screenShareVideo, let t = pub.track as? VideoTrack {
                    screenShareTrack = t
                    return
                }
            }
        }
        screenShareTrack = nil
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
        Task { @MainActor in owner?.refreshScreenShareTrack() }
    }
}
