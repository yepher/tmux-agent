import CryptoKit
import Foundation

/// Mints a LiveKit access token on-device (HS256 JWT signed with the API secret).
/// For a dev tool this is fine; for production, move JWT minting to a server
/// so the API secret never leaves your backend.
enum TokenBuilder {
    struct TokenError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static func build(
        apiKey: String,
        apiSecret: String,
        identity: String,
        room: String,
        ttlSeconds: TimeInterval = 3600
    ) throws -> String {
        guard !apiKey.isEmpty, !apiSecret.isEmpty, !identity.isEmpty, !room.isEmpty else {
            throw TokenError(message: "apiKey, apiSecret, identity, and room are required")
        }

        let now = Int(Date().timeIntervalSince1970)
        let exp = now + Int(ttlSeconds)

        let header: [String: String] = ["alg": "HS256", "typ": "JWT"]
        let video: [String: Any] = [
            "roomJoin": true,
            "room": room,
            "canPublish": true,
            "canPublishData": true,
            "canSubscribe": true,
        ]
        let payload: [String: Any] = [
            "iss": apiKey,
            "sub": identity,
            "name": identity,
            "nbf": now,
            "exp": exp,
            "video": video,
        ]

        let headerJSON = try JSONSerialization.data(
            withJSONObject: header, options: [.sortedKeys])
        let payloadJSON = try JSONSerialization.data(
            withJSONObject: payload, options: [.sortedKeys])

        let headerB64 = base64url(headerJSON)
        let payloadB64 = base64url(payloadJSON)
        let signingInput = "\(headerB64).\(payloadB64)"

        let key = SymmetricKey(data: Data(apiSecret.utf8))
        let sig = HMAC<SHA256>.authenticationCode(
            for: Data(signingInput.utf8), using: key)
        let sigB64 = base64url(Data(sig))

        return "\(signingInput).\(sigB64)"
    }

    private static func base64url(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
