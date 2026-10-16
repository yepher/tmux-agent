import Foundation

/// Fetches a LiveKit token from `agent/token_server.py` so the API secret
/// never has to be stored on the phone.
///
/// Wire format (must match agent/token_server.py):
///   POST <server>/token   Authorization: Bearer <key>
///   {"identity": str, "room": str}
///   → {"url": str, "token": str, "room": str}
enum TokenServerClient {
    struct Credentials: Decodable, Sendable {
        let url: String
        let token: String
        let room: String
    }

    struct ServerError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static func fetch(
        serverURL: String,
        key: String,
        identity: String,
        room: String
    ) async throws -> Credentials {
        guard var url = URL(string: serverURL.trimmingCharacters(in: .whitespaces)),
              url.scheme != nil
        else {
            throw ServerError(message: "Invalid token server URL")
        }
        if url.path.isEmpty || url.path == "/" {
            url = url.appendingPathComponent("token")
        }

        var request = URLRequest(url: url, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONEncoder().encode(
            ["identity": identity, "room": room]
        )

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw ServerError(message: "No HTTP response from token server")
        }
        guard http.statusCode == 200 else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["error"]
                ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
            throw ServerError(message: "Token server \(http.statusCode): \(detail)")
        }
        return try JSONDecoder().decode(Credentials.self, from: data)
    }
}
