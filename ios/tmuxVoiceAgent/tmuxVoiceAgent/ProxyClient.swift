import Foundation
import LiveKit

/// Tunnels HTTP requests from WKWebView through a LiveKit byte-stream pair
/// (`http.request` outbound, `http.response` inbound) to the agent, which
/// re-issues them against its local dev server (default `localhost:3000`).
///
/// Wire format (must match agent/rtc_proxy.py):
///   Request stream attributes: id, method, path, headers (JSON dict string)
///   Request body: the HTTP request body bytes
///   Response stream attributes: id, status, status_text, headers (JSON dict)
///   Response body: the HTTP response body bytes
///
/// Modeled as an actor so the pending-continuation map and the register flag
/// are safely mutated from both the public `send` entry and the byte-stream
/// handler callback.
actor ProxyClient {
    struct Response: Sendable {
        let status: Int
        let statusText: String
        let headers: [String: String]
        let body: Data
    }

    static let requestTopic = "http.request"
    static let responseTopic = "http.response"

    nonisolated let room: Room

    private var pending: [String: CheckedContinuation<Response, Error>] = [:]
    private var isHandlerRegistered = false

    init(room: Room) {
        self.room = room
    }

    /// Attach our response-stream handler to the room. Idempotent.
    func registerResponseHandler() async {
        guard !isHandlerRegistered else { return }
        isHandlerRegistered = true
        do {
            try await room.registerByteStreamHandler(
                for: Self.responseTopic
            ) { [weak self] reader, _ in
                await self?.handleResponse(reader: reader)
            }
        } catch {
            print("ProxyClient: registerByteStreamHandler failed: \(error)")
        }
    }

    /// Send one HTTP request through the tunnel; suspend until the agent's
    /// response arrives (or the outbound write errors).
    func send(
        method: String,
        path: String,
        headers: [String: String],
        body: Data?
    ) async throws -> Response {
        let id = UUID().uuidString
        guard let agentIdentity = resolveAgentIdentity() else {
            throw URLError(.cannotConnectToHost, userInfo: [
                NSLocalizedDescriptionKey: "Agent not in room yet",
            ])
        }

        let headersJSON = (try? String(
            data: JSONSerialization.data(
                withJSONObject: headers, options: [.sortedKeys]),
            encoding: .utf8
        )) ?? "{}"

        let options = StreamByteOptions(
            topic: Self.requestTopic,
            attributes: [
                "id": id,
                "method": method.uppercased(),
                "path": path,
                "headers": headersJSON,
            ],
            destinationIdentities: [Participant.Identity(from: agentIdentity)],
            mimeType: "application/octet-stream",
            name: "request-\(id)",
            totalSize: body?.count
        )

        return try await withCheckedThrowingContinuation { cont in
            pending[id] = cont
            Task { [weak self] in
                do {
                    guard let self else { return }
                    let writer = try await self.room.localParticipant
                        .streamBytes(options: options)
                    if let body, !body.isEmpty {
                        try await writer.write(body)
                    }
                    try await writer.close()
                } catch {
                    await self?.failPending(id: id, error: error)
                }
            }
        }
    }

    private func failPending(id: String, error: Error) {
        if let cont = pending.removeValue(forKey: id) {
            cont.resume(throwing: error)
        }
    }

    private func handleResponse(reader: ByteStreamReader) async {
        let attrs = reader.info.attributes
        let id = attrs["id"] ?? reader.info.id
        let status = Int(attrs["status"] ?? "") ?? 502
        let statusText = attrs["status_text"] ?? ""
        let headers: [String: String] = {
            let json = attrs["headers"] ?? "{}"
            let parsed = (try? JSONSerialization.jsonObject(
                with: Data(json.utf8)
            )) as? [String: String]
            return parsed ?? [:]
        }()

        var body = Data()
        do {
            for try await chunk in reader {
                body.append(chunk)
            }
        } catch {
            if let cont = pending.removeValue(forKey: id) {
                cont.resume(throwing: error)
            }
            return
        }

        let response = Response(
            status: status,
            statusText: statusText,
            headers: headers,
            body: body
        )
        if let cont = pending.removeValue(forKey: id) {
            cont.resume(returning: response)
        }
    }

    private func resolveAgentIdentity() -> String? {
        for participant in room.remoteParticipants.values {
            let raw = participant.identity?.stringValue ?? ""
            if raw.lowercased().hasPrefix("agent") {
                return raw
            }
        }
        return room.remoteParticipants.values
            .first?.identity?.stringValue
        
    }
}
