import Foundation
import LiveKit

/// Thin LiveKit-RPC wrapper around the agent's session-control API.
///
/// Wire format (must match agent/rtc_control.py):
///   sessions.list     → "" → {"sessions": [{"name": str, "current": bool}], "current": str}
///   sessions.switch   → {"name": str} → {"ok": bool, "current": str?, "error": str?}
actor SessionController {
    struct SessionInfo: Codable, Hashable, Identifiable, Sendable {
        let name: String
        let current: Bool
        var id: String { name }
    }

    struct SessionList: Sendable {
        let sessions: [SessionInfo]
        let current: String
    }

    enum Error: Swift.Error, LocalizedError {
        case agentNotInRoom
        case switchFailed(String)
        case badResponse(String)

        var errorDescription: String? {
            switch self {
            case .agentNotInRoom: return "Agent not in the room yet."
            case .switchFailed(let msg): return "Switch failed: \(msg)"
            case .badResponse(let msg): return "Bad response from agent: \(msg)"
            }
        }
    }

    nonisolated let room: Room

    init(room: Room) {
        self.room = room
    }

    func list() async throws -> SessionList {
        let resp = try await performRpc(method: "sessions.list", payload: "")
        let decoded: ListResponse
        do {
            decoded = try JSONDecoder().decode(
                ListResponse.self, from: Data(resp.utf8)
            )
        } catch {
            throw Error.badResponse(resp)
        }
        return SessionList(sessions: decoded.sessions, current: decoded.current)
    }

    @discardableResult
    func switchTo(name: String) async throws -> String {
        let payload = try JSONEncoder().encode(SwitchRequest(name: name))
        let resp = try await performRpc(
            method: "sessions.switch",
            payload: String(decoding: payload, as: UTF8.self)
        )
        let decoded: SwitchResponse
        do {
            decoded = try JSONDecoder().decode(
                SwitchResponse.self, from: Data(resp.utf8)
            )
        } catch {
            throw Error.badResponse(resp)
        }
        guard decoded.ok else {
            throw Error.switchFailed(decoded.error ?? "unknown error")
        }
        return decoded.current ?? name
    }

    // MARK: - Internals

    private func performRpc(method: String, payload: String) async throws -> String {
        guard let agentIdentity = resolveAgentIdentity() else {
            throw Error.agentNotInRoom
        }
        return try await room.localParticipant.performRpc(
            destinationIdentity: Participant.Identity(from: agentIdentity),
            method: method,
            payload: payload
        )
    }

    private func resolveAgentIdentity() -> String? {
        for p in room.remoteParticipants.values {
            let raw = p.identity?.stringValue ?? ""
            if raw.lowercased().hasPrefix("agent") {
                return raw
            }
        }
        return room.remoteParticipants.values.first?.identity?.stringValue
    }

    // MARK: - Wire types

    private struct ListResponse: Codable {
        let sessions: [SessionInfo]
        let current: String
    }
    private struct SwitchRequest: Codable { let name: String }
    private struct SwitchResponse: Codable {
        let ok: Bool
        let current: String?
        let error: String?
    }
}
