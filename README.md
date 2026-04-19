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

## Is this what you want? (vs. Claude Code Remote Control)

Anthropic ships [Claude Code Remote Control](https://code.claude.com/docs/remote-control), which turns claude.ai/code and the Claude mobile app into a remote window onto your local Claude Code session. It's official, zero-infra, and has real push notifications. For a lot of "drive Claude from my phone" use cases it's the right answer.

**Use Remote Control when** you want a polished, subscription-gated, text-chat remote for Claude Code specifically. Push wakes your phone; you keep typing the way you already type.

**Use this project when** you need one (or more) of:

- **Voice as the primary interface.** You talk to an OpenAI Realtime agent; it drives the terminal. No typing on your phone. Remote Control is still a text chat.
- **Driving things other than Claude Code.** The agent here sits in front of *any* tmux program — a shell, `vim`, `htop`, a dev server, a long-running build — and Claude Code is just one of the things it might be driving. Remote Control only exposes Claude Code.
- **Actually seeing the terminal.** The iOS app receives the live tmux pane as a video track — colors, cursor, spinners, the whole TUI. Remote Control shows the chat transcript, not the terminal.
- **Browsing dev servers from your phone.** The iOS client has an in-app `WKWebView` that tunnels HTTP through LiveKit to the agent machine, so `tunnel://localhost/` on the phone reaches your `localhost:3000` dev server. Remote Control doesn't do this.
- **Self-hosted infra on your own API keys.** Bring your own LiveKit and OpenAI credentials; nothing is gated by a Claude subscription tier.


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
