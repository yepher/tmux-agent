# tmux_agent

A LiveKit voice agent that shares a `tmux` session as a screen-share video
track and lets you drive the terminal by voice. When you connect to the room
you see the tmux pane live; when you talk, the agent runs commands, sends
keys, switches sessions or windows, and narrates what it's doing.

Built on `livekit-agents` + OpenAI Realtime. Works from any LiveKit client —
a browser (e.g. <https://meet.livekit.io>), or the custom iOS app bundled
here, which also tunnels HTTP requests to a dev server running on the agent
machine so you can view it from your phone.

**Demo**
[![Demo](https://img.youtube.com/vi/TNtxj9letrg/maxresdefault.jpg)](https://youtu.be/TNtxj9letrg)

**Browser example**
[![Browser demo](https://img.youtube.com/vi/yQYDYB9REl0/maxresdefault.jpg)](https://youtu.be/yQYDYB9REl0)

**Mobile example**
[![Mobile demo](https://img.youtube.com/vi/0i5sbigrLJ8/maxresdefault.jpg)](https://youtu.be/0i5sbigrLJ8)

## Layout

```
tmux_agent/
├── README.md          ← you are here
├── agent/             ← Python voice agent (server)
│   └── README.md
└── ios/               ← SwiftUI client (WKWebView + HTTP tunnel over LiveKit)
    └── README.md
```

- **[`agent/`](agent/README.md)** — setup, run commands, voice tools, config
  env vars, architecture, troubleshooting.
- **[`ios/`](ios/README.md)** — Xcode setup, LiveKit SDK dependency, Info.plist
  keys, in-app HTTP tunnel protocol, troubleshooting.

## Quick start

1. Set up and run the agent — see [`agent/README.md`](agent/README.md).
2. Join the room from any LiveKit client. The browser demo above uses
   <https://meet.livekit.io>. For the mobile experience, build and run the
   iOS app — see [`ios/README.md`](ios/README.md).
