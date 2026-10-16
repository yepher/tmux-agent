# ios — TmuxAgent mobile client

SwiftUI app that joins the LiveKit room, subscribes to the tmux agent's
screen-share + audio, and tunnels HTTP requests from an in-app `WKWebView`
through the agent so you can browse a dev server running on the agent
machine (e.g. `http://localhost:3000`) from your phone.

## Layout

```
ios/
├── README.md                 ← you are here
├── client-sdk-swift/         ← local clone of livekit/client-sdk-swift (SPM path dep)
└── tmuxVoiceAgent/
    ├── tmuxVoiceAgent.xcodeproj
    └── tmuxVoiceAgent/       ← Swift sources
```

Key sources:

- `tmuxVoiceAgentApp.swift` — `@main`, installs the orientation-lock app delegate.
- `ContentView.swift` — tab root: Agent / Browser / Settings.
- `SettingsStore.swift` + `SettingsView.swift` — Keychain-backed creds form.
- `Keychain.swift` — Security-framework wrapper.
- `TokenServerClient.swift` — fetches tokens from `agent/token_server.py`
  (preferred: the API secret stays off the phone).
- `TokenBuilder.swift` — fallback HS256 JWT minting via CryptoKit.
- `RoomConnection.swift` — `@Observable` LiveKit room + delegate adapter.
- `RoomView.swift` — renders the agent's video track, mic toggle, hangup.
- `LiveKitVideoView.swift` — `UIViewRepresentable` wrapping `LiveKit.VideoView`.
- `BrowserView.swift` — `WKWebView` + address bar + reload button.
- `ProxyClient.swift` — `actor` that ships `HTTPRequest`/`HTTPResponse`
  over LiveKit byte streams (`http.request` / `http.response`).
- `TunnelSchemeHandler.swift` — `WKURLSchemeHandler` for the `tunnel://`
  scheme.
- `OrientationLock.swift` — per-view `UIInterfaceOrientationMask` helper.

## Xcode setup

1. Open `tmuxVoiceAgent/tmuxVoiceAgent.xcodeproj`.
2. Bundle ID defaults to `com.chriswilson.tmuxagent` — change in Signing &
   Capabilities.
3. Minimum deployment target: iOS 17.0.
4. Signing: pick your team. For on-device runs you need a dev profile.

### LiveKit SDK dependency

The project links `LiveKit` via a **local path** dependency at
`ios/client-sdk-swift/`. That clone's own dependencies are also local, in
`ios/vendor/`, so Xcode never fetches anything from GitHub. Xcode's built-in
git and artifact downloads kept failing with GitHub auth errors in the
author's environment, even for public repos. Both folders are gitignored. To
recreate them:

```bash
cd ios
git clone --branch 2.13.0 --depth 1 https://github.com/livekit/client-sdk-swift
mkdir -p vendor && cd vendor
git clone --branch 144.7559.03 --depth 1 https://github.com/livekit/webrtc-xcframework
git clone --branch 0.0.5 --depth 1 https://github.com/livekit/livekit-uniffi-xcframework
git clone --branch 1.36.1 --depth 1 --recurse-submodules --shallow-submodules https://github.com/apple/swift-protobuf
curl -L -o webrtc-xcframework/LiveKitWebRTC.xcframework.zip \
  https://github.com/livekit/webrtc-xcframework/releases/download/144.7559.03/LiveKitWebRTC.xcframework.zip
curl -L -o livekit-uniffi-xcframework/RustLiveKitUniFFI.xcframework.zip \
  https://github.com/livekit/livekit-uniffi-xcframework/releases/download/0.0.5/RustLiveKitUniFFI.xcframework.zip
shasum -a 256 */*.zip
# 892898ba…d4fd  LiveKitWebRTC.xcframework.zip
# 9a21e046…2600  RustLiveKitUniFFI.xcframework.zip
```

Then patch the manifests:

- In `vendor/webrtc-xcframework/Package.swift` and
  `vendor/livekit-uniffi-xcframework/Package*.swift`, replace each binary
  target's `url:` + `checksum:` with `path: "<the .zip name>"`.
- In `client-sdk-swift/Package*.swift`, point the three dependencies at
  `.package(path: "../vendor/<name>")` and delete the `swift-docc-plugin`
  line (it's only used for docs).

If GitHub works from Xcode for you, you can skip all of this: **File → Add
Package Dependencies… → `https://github.com/livekit/client-sdk-swift`**
(2.13.0) and remove the local reference.

### Info.plist keys

- `NSMicrophoneUsageDescription` — "Talk to the tmux agent."
- `NSLocalNetworkUsageDescription` — "Connect to LiveKit."
- Background modes: **Audio, AirPlay, and Picture in Picture** so audio keeps
  flowing when the screen is off.

## Settings

Settings live in the Keychain (`Keychain.swift`), not UserDefaults.

**Token server (recommended).** Run `agent/token_server.py` on your machine
and enter its URL and `TOKEN_SERVER_KEY`. The app POSTs your identity and
room name to it and gets back a LiveKit URL and token. The LiveKit API secret
never touches the phone. The token server only mints tokens for
`TMUX_ALLOWED_IDENTITIES` and adds explicit agent dispatch for you. Expose it
over HTTPS (Tailscale, cloudflared, ngrok). App Transport Security blocks
plain `http://` unless you add an ATS exception.

**On-device minting (fallback).** If the token server URL is empty, the app
mints tokens itself with:

- `livekit_url` (e.g. `wss://chris-test-xxx.livekit.cloud`)
- `livekit_api_key`
- `livekit_api_secret`. This secret grants admin over the whole project,
  so only use this mode with a project dedicated to this tool.

Both modes use:

- `room_name` (free text, e.g. `my-room`)
- `identity` (free text, e.g. `mobile-user`). Must be in the agent's
  `TMUX_ALLOWED_IDENTITIES` if that's set.
- `agent_name` (default `tmux-agent`). Must match the agent's
  `TMUX_AGENT_NAME`. The token carries `roomConfig.agents` so LiveKit
  dispatches the agent when the room is created.

On connect the room name is suffixed with the current epoch seconds
(`my-room-1713461234`) so every reconnect lands in a fresh room and doesn't
inherit stale participants.

## Agent tab — video display

LiveKit publishes the tmux pane as a 16:9 track. The phone stays in portrait;
the video is displayed sideways (landscape-in-portrait) via the standard
"swap the frame, rotate -90°, swap back" pattern in `RoomView.swift`. See the
comment block at the top of that file for the three-step breakdown.

Controls:

- **Hangup / Connect** — nav bar top-right. Red `phone.down.fill` with a
  confirmation dialog when connected; green `phone.fill` when disconnected.
- **Mic** — floating circle in lower-right, only visible when connected.
- **Mic errors** (e.g. simulator mic -4010) surface as a banner above the mic
  button without tearing the call down.

Run on a **physical iPhone**. The iOS Simulator's CoreAudio stack is broken on
Xcode 16 / macOS 15 (error `-4010`) and cannot publish mic audio.

## Browser tab — HTTP tunnel over a custom scheme

The in-app `WKWebView` registers a handler for a custom `tunnel://` scheme.
Every request the webview issues flows through this chain:

1. `TunnelSchemeHandler` serializes the `URLRequest` into an `HTTPRequest`
   struct (method / path / query / headers / body).
2. `ProxyClient` opens a LiveKit byte stream with topic `http.request`,
   writes the serialized request, and awaits a matching `HTTPResponse` on
   `http.response` (correlated by UUID).
3. The agent proxies to `PROXY_TARGET` (default `http://localhost:3000`)
   via `aiohttp` and streams the response back.

**Caching.** `WKWebsiteDataStore.nonPersistent()` + every load uses
`URLRequest(cachePolicy: .reloadIgnoringLocalAndRemoteCacheData)` so
reloads always re-fetch through the tunnel.

### Why a custom `tunnel://` scheme (and not a real proxy)

We'd prefer that the webview just load `http://localhost:3000` directly
and have traffic transparently routed through LiveKit. Two approaches we
tried or considered:

- **`WKWebsiteDataStore.proxyConfigurations` (iOS 17+).** The API is
  public but silently ignored for non-browser apps — it's gated behind
  Apple's browser-engine entitlement (`com.apple.developer.web-browser-
  engine.networking`, etc.) which is only granted to EU DMA browser
  engines. Both `socksv5Proxy` and `httpCONNECTProxy` variants were
  tested; WKWebView never connected to the local listener.
- **`NetworkExtension` packet-tunnel provider.** A real per-app VPN.
  Works, but requires the `com.apple.developer.networking.networkextension`
  entitlement (Apple-approved), a separate extension target with a ~15
  MB memory cap, and IPC back to the main app that owns the LiveKit
  `Room`. Overkill for a personal dev tool.

So the custom scheme is the path that actually works on iOS without
Apple-side paperwork. `WKURLSchemeHandler` can intercept any scheme
*except* `http`/`https`, which is why the webview loads
`tunnel://localhost/...`. Relative URLs stay inside `tunnel://`; the
agent maps the path onto `PROXY_TARGET` (default `http://localhost:3000`)
on its own machine.

**Why byte streams, not RPC.** LiveKit RPC caps payloads at ~15 KB — way
below a typical JS bundle. Byte streams have no meaningful cap; we've
moved 900 KB+ chunks through them without issue.

## Troubleshooting

- **`No such module 'LiveKit'` in Xcode's editor only** — that's SourceKit's
  stale indexer. Build (`⌘B`) to force a rebuild of the module; the error
  clears once the real compile succeeds.
- **Mic toggle fails with `-4010`** — you're on the simulator. Use a physical
  device.
- **Browser tab shows a stale page after you edit the dev server** — hit the
  reload button (circular arrow) next to Go. It forces a re-fetch through the
  tunnel.
- **Browser tab shows "not connected"** — connect on the Agent tab first.
  Tunnel requests time out after 30s and fail right away if the agent leaves
  the room.
- **Connected but the agent never joins** — the agent uses explicit dispatch.
  Check that Settings → Agent name matches `TMUX_AGENT_NAME` on the agent, and
  that your identity is in `TMUX_ALLOWED_IDENTITIES` (the agent log says
  "ignoring participant" when it isn't).
- **Video shows nothing** — the app prefers the agent's screen-share track
  and falls back to any video track, so check the agent's log for
  "video track published".
