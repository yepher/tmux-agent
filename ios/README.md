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
- `TokenBuilder.swift` — HS256 JWT minting via CryptoKit.
- `RoomConnection.swift` — `@Observable` LiveKit room + delegate adapter.
- `RoomView.swift` — renders the agent's video track, mic toggle, hangup.
- `LiveKitVideoView.swift` — `UIViewRepresentable` wrapping `LiveKit.VideoView`.
- `BrowserView.swift` — `WKWebView` + address bar + reload button.
- `ProxyClient.swift` — `actor` that ships `HTTPRequest`/`HTTPResponse` over
  LiveKit byte streams.
- `TunnelSchemeHandler.swift` — `WKURLSchemeHandler` for the `tunnel://` scheme.
- `OrientationLock.swift` — per-view `UIInterfaceOrientationMask` helper.

## Xcode setup

1. Open `tmuxVoiceAgent/tmuxVoiceAgent.xcodeproj`.
2. Bundle ID defaults to `com.chriswilson.tmuxagent` — change in Signing &
   Capabilities.
3. Minimum deployment target: iOS 17.0.
4. Signing: pick your team. For on-device runs you need a dev profile.

### LiveKit SDK dependency

The project links `LiveKit` via a **local path** dependency at
`ios/client-sdk-swift/`. If you pulled this repo fresh, clone the SDK alongside
the app:

```bash
cd ios
git clone https://github.com/livekit/client-sdk-swift
# git checkout a tagged release if you prefer reproducibility
```

If you'd rather use the remote SPM version: **File → Add Package Dependencies…
→ `https://github.com/livekit/client-sdk-swift`** and remove the local
reference. The local path was adopted because Xcode's GitHub auth kept failing
in the author's environment.

### Info.plist keys

- `NSMicrophoneUsageDescription` — "Talk to the tmux agent."
- `NSLocalNetworkUsageDescription` — "Connect to LiveKit."
- Background modes: **Audio, AirPlay, and Picture in Picture** so audio keeps
  flowing when the screen is off.

## Settings

LiveKit creds live in the Keychain (`Keychain.swift`), not UserDefaults — the
API secret is sensitive. Fields the settings form captures:

- `livekit_url` (e.g. `wss://chris-test-xxx.livekit.cloud`)
- `livekit_api_key`
- `livekit_api_secret`
- `room_name` (free text, e.g. `my-room`)
- `identity` (free text, e.g. `mobile-user`)

Tokens are minted on-device via `TokenBuilder`. For a dev tool this is fine —
move to a token server if you distribute the app.

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

## Browser tab — HTTP tunnel

The in-app `WKWebView` registers a handler for a custom `tunnel://` scheme.
Every request the webview issues flows through this chain:

1. `TunnelSchemeHandler` serializes the `URLRequest` into an `HTTPRequest`
   struct (method / path / query / headers / body).
2. `ProxyClient` opens a LiveKit byte stream with topic `http.request`, writes
   the serialized request, and awaits a matching `HTTPResponse` on the
   `http.response` topic (correlated by UUID).
3. The agent proxies to `PROXY_TARGET` (default `http://localhost:3000`) via
   `aiohttp` and streams the response back.

**Why byte streams, not RPC?** LiveKit RPC caps payloads at ~15 KB — way below a
typical JS bundle. Byte streams have no effective cap; we've moved 900 KB+ chunks
through them without issue.

**Why a custom `tunnel://` scheme?** `WKURLSchemeHandler` can only intercept
custom schemes, not `http`/`https`. The webview loads `tunnel://localhost/` and
all relative fetches stay inside `tunnel://` where we can intercept them.
Absolute `http://localhost:3000/…` links in the page would bypass the handler;
for Claude-built dev sites that rarely matters.

**Caching.** The webview uses `WKWebsiteDataStore.nonPersistent()` and every
load uses `URLRequest(cachePolicy: .reloadIgnoringLocalAndRemoteCacheData)` so
reload always re-fetches through the tunnel.

## Troubleshooting

- **`No such module 'LiveKit'` in Xcode's editor only** — that's SourceKit's
  stale indexer. Build (`⌘B`) to force a rebuild of the module; the error
  clears once the real compile succeeds.
- **Mic toggle fails with `-4010`** — you're on the simulator. Use a physical
  device.
- **Browser tab shows a stale page after you edit the dev server** — hit the
  reload button (circular arrow) next to Go. It forces a re-fetch through the
  tunnel.
- **Connect spinner hangs** — check the Agent tab is dispatching. The
  mobile app only joins the room; something has to tell LiveKit to start the
  agent job (Cloud Agents playground, or your own dispatch frontend).
