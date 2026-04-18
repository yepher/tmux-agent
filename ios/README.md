# ios — TmuxAgent mobile client

Custom iOS app that joins the LiveKit room, subscribes to the tmux agent's
screen-share + audio, and tunnels HTTP requests from an in-app WebKit view
through the agent so you can view dev sites Claude is running on
`localhost:3000` inside the app.

## Plan

1. **Phase 1 — Connect.** SwiftUI app with a settings screen (LiveKit URL,
   API key, API secret, room name). Persist to Keychain. Connect to the room
   using a locally-minted JWT; render the agent's screen-share track and pipe
   mic audio back.
2. **Phase 2 — WebKit.** Embed a `WKWebView`. Hit `localhost:3000` over the
   tunnel (phase 3); until then just load `about:blank` and show scaffolding.
3. **Phase 3 — HTTP tunnel.** Register a `WKURLSchemeHandler` for a custom
   scheme (`tunnel://`). Every fetch made by the WKWebView gets serialized
   and sent to the agent over LiveKit; the agent proxies to `http://localhost:3000`
   and streams the response back. Ship raw bytes via LiveKit *byte streams*
   (not RPC) — JS/HTML payloads blow past the 15 KB RPC cap.

## Create the Xcode project

Put the Xcode project right under `ios/` so the folder ends up:

```
ios/
├── README.md          ← you are here
└── TmuxAgent/         ← the .xcodeproj and Swift sources
```

Steps:

1. Open Xcode → **File → New → Project…**
2. **iOS → App**, Next.
3. Product Name: `TmuxAgent`. Interface: **SwiftUI**. Language: **Swift**.
   Storage: **None**. Include Tests: your call.
4. Bundle ID: `com.chriswilson.tmuxagent` (or whatever).
5. Save into `tmux_agent/ios/` (don't create an extra enclosing folder —
   Xcode will add `TmuxAgent/` for you).
6. Minimum Deployment: **iOS 17.0**.

## Add LiveKit Swift SDK

In Xcode:

1. **File → Add Package Dependencies…**
2. URL: `https://github.com/livekit/client-sdk-swift`
3. Rule: **Up to Next Major** from the latest release (currently 2.x).
4. Add `LiveKit` to the `TmuxAgent` app target.

Info.plist additions the SDK needs:

- `NSMicrophoneUsageDescription` — "Talk to the tmux agent."
- `NSCameraUsageDescription` — optional; only if you later publish camera.
- `NSLocalNetworkUsageDescription` — "Connect to LiveKit."
- Background modes: **Audio, AirPlay, and Picture in Picture** (so audio
  keeps flowing when the screen is off).

## Settings storage

Put LiveKit creds in the Keychain (via `Security` framework or a small helper
like `KeychainAccess`). Never UserDefaults — API secret is sensitive.

Minimum fields to capture in `SettingsView`:

- `livekit_url` (e.g. `wss://chris-test-xxx.livekit.cloud`)
- `livekit_api_key`
- `livekit_api_secret`
- `room_name` (free text, e.g. `my-room`)
- `identity` (free text, e.g. `mobile-user`)

Mint the JWT on-device with the `api_key`/`api_secret` and join with that.
Keeps things simple for a dev tool; move to a token-server later if you
share this beyond yourself.

## HTTP tunnel — protocol sketch

This lives inside the LiveKit room; agent and app are both participants.

**Wire format** (both directions), CBOR or JSON — start with JSON for
debuggability, switch to CBOR later if payload size matters:

```
Request  (app → agent)       Response (agent → app)
{                            {
  "id": "<uuid>",              "id": "<uuid>",
  "method": "GET",             "status": 200,
  "path": "/foo?bar=1",        "headers": { ... },
  "headers": { ... },          "body_b64": "<base64>"
  "body_b64": "<base64>"     }
}
```

**Transport:** LiveKit byte streams (not RPC — RPC payloads cap at ~15 KB).

- App opens a byte stream with topic `http.request`, name = request id.
  Writes JSON header frame, then request body bytes.
- Agent's matching stream handler reads the request, fires `aiohttp` at
  `http://localhost:3000{path}`, reads the response, opens a byte stream
  back with topic `http.response`, name = request id, writes status/headers
  then body.
- App correlates by id, hands bytes to the `URLSchemeTask`.

## Phase breakdown — concrete next steps

**Phase 1 (after you've created the Xcode project, come back and I'll drop
in Swift sources for):**

- `TmuxAgentApp.swift` — @main.
- `SettingsStore.swift` — Keychain-backed `@Observable` store.
- `SettingsView.swift` — form UI.
- `RoomConnection.swift` — LiveKit room lifecycle + JWT minting.
- `ContentView.swift` — tabbed root: [Room view] [Settings].
- `RoomView.swift` — renders the agent's video track + mic toggle.

**Phase 2:**

- `BrowserView.swift` — SwiftUI wrapper around `WKWebView`.
- Tab 3: browser pointed at `tunnel://localhost/` (blank until Phase 3).

**Phase 3:**

- `TunnelScheme.swift` — `WKURLSchemeHandler` that serializes requests and
  writes to a LiveKit byte stream, then plumbs the response back.
- Agent side (`agent/tmux_agent.py`): register a byte-stream handler on the
  `http.request` topic that proxies to `http://localhost:3000`. Add `aiohttp`
  to `agent/requirements.txt`.

## Why a custom scheme (not `http://localhost:3000` directly)

`WKWebView` lets you intercept *custom* schemes with `WKURLSchemeHandler`,
but not `http`/`https`. So the webview loads `tunnel://localhost/` and all
relative requests stay inside `tunnel://`, where we can intercept them.
Absolute `http://localhost:3000/...` links in the page would break unless
we rewrite them; for Claude-built dev sites that's rarely an issue.
