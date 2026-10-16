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
/// Every pending request ends exactly once: with the agent's response, a
/// write error, a timeout, task cancellation, or `failAll` (disconnect /
/// agent left). Modeled as an actor so the pending maps are only touched
/// from one place.
actor ProxyClient {
    struct Response: Sendable {
        let status: Int
        let statusText: String
        let headers: [String: String]
        let body: Data
    }

    static let requestTopic = "http.request"
    static let responseTopic = "http.response"
    static let defaultTimeout: TimeInterval = 30

    nonisolated let room: Room

    private var pending: [String: CheckedContinuation<Response, Error>] = [:]
    private var timeouts: [String: Task<Void, Never>] = [:]
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
    /// response arrives, the write fails, `timeout` elapses, or the calling
    /// task is cancelled.
    func send(
        method: String,
        path: String,
        headers: [String: String],
        body: Data?,
        timeout: TimeInterval = ProxyClient.defaultTimeout
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

        return try await withTaskCancellationHandler {
            try await self.awaitResponse(
                id: id, options: options, body: body, timeout: timeout
            )
        } onCancel: {
            Task { await self.complete(id: id, with: .failure(CancellationError())) }
        }
    }

    /// Register the continuation for `id`, arm its timeout, and write the
    /// request stream. Runs on the actor so `pending` is mutated safely.
    private func awaitResponse(
        id: String,
        options: StreamByteOptions,
        body: Data?,
        timeout: TimeInterval
    ) async throws -> Response {
        try await withCheckedThrowingContinuation { cont in
            // Cancelled before we registered: onCancel already ran and
            // found nothing to fail, so finish here.
            if Task.isCancelled {
                cont.resume(throwing: CancellationError())
                return
            }
            pending[id] = cont
            timeouts[id] = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                guard !Task.isCancelled else { return }
                await self?.complete(id: id, with: .failure(URLError(.timedOut)))
            }
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
                    await self?.complete(id: id, with: .failure(error))
                }
            }
        }
    }

    /// Fail every in-flight request (room disconnected / agent left).
    func failAll(_ error: Error = URLError(.networkConnectionLost)) {
        let conts = pending
        pending.removeAll()
        timeouts.values.forEach { $0.cancel() }
        timeouts.removeAll()
        for cont in conts.values {
            cont.resume(throwing: error)
        }
    }

    /// Resolve request `id` exactly once; later calls are no-ops.
    private func complete(id: String, with result: Result<Response, Error>) {
        timeouts.removeValue(forKey: id)?.cancel()
        guard let cont = pending.removeValue(forKey: id) else { return }
        cont.resume(with: result)
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
            complete(id: id, with: .failure(error))
            return
        }

        complete(id: id, with: .success(Response(
            status: status,
            statusText: statusText,
            headers: headers,
            body: body
        )))
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
