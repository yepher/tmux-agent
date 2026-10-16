# agent — tmux voice agent (server)

Python LiveKit agent that attaches to a `tmux` session, publishes the active pane as
a screen-share video track, and accepts voice commands through a realtime model
served by [LiveKit Inference](https://docs.livekit.io/agents/models/). The LLM
has tools to type text, send keys, run commands, read the screen, and switch
sessions/windows. Terminal rendering goes through `pyte` so ANSI colors and
attributes survive.

## Requirements

- macOS or Linux with `tmux` on `$PATH`.
- Python 3.10+.
- A LiveKit Cloud project (URL + API key/secret). The realtime model runs on
  LiveKit Inference with the same credentials, so you don't need an OpenAI key.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in creds
```

Required in `.env`:

```
LIVEKIT_URL=wss://...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
TMUX_AGENT_NAME=tmux-agent            # explicit dispatch (see Security)
TMUX_ALLOWED_IDENTITIES=mobile-user   # who may drive the terminal
```

## Security

This agent gives voice control of a real shell on your machine, so lock it down:

- **Explicit dispatch.** The worker registers as `TMUX_AGENT_NAME` (default
  `tmux-agent`) and only joins rooms that request it. The iOS app and
  `token_server.py` put the dispatch in the token (`roomConfig.agents`). For a
  browser client, dispatch it yourself:
  `lk dispatch create --agent-name tmux-agent --room <room>`. Setting
  `TMUX_AGENT_NAME=` (empty) turns automatic dispatch back on, which sends the
  agent into **every** new room in the project.
- **Identity allowlist.** `TMUX_ALLOWED_IDENTITIES` (comma-separated) limits
  who the voice session listens to and who can call the session RPCs, use the
  HTTP tunnel, or get a token from `token_server.py`. If it's empty, the agent
  logs a warning and anyone in the room can drive the terminal.
- **Destructive-command confirmation.** `run_command` and
  `send_text(..., press_enter=True)` refuse things like `rm -rf`, `sudo`,
  `git reset --hard`, force-push, `curl | sh`, `dd` and `mkfs`. The model has
  to read the command back and get a spoken "yes" first. This is a speed bump,
  not a sandbox.
- **Untrusted screen text.** The system prompt and the watcher nudges tell
  the model that terminal output is data, not instructions. Prompt injection
  through what's on screen is still possible, so don't point it at content
  you don't trust.
- **Token server.** `python token_server.py` mints tokens for the iOS app,
  so the LiveKit API secret doesn't have to be stored on the phone. Set
  `TOKEN_SERVER_KEY` and expose it over HTTPS (Tailscale, cloudflared).

## Run

With the venv activated:

```bash
python tmux_agent.py dev     # hot-reload, connects to LiveKit
python tmux_agent.py start   # production
python tmux_agent.py console # local audio, no LiveKit room
python token_server.py       # optional: tokens for the iOS app (needs TOKEN_SERVER_KEY)
```

The agent only joins rooms that dispatch `TMUX_AGENT_NAME` (see Security). The
iOS app does this through its token. For other clients run
`lk dispatch create --agent-name tmux-agent --room <room>`, then
join the room from a client (the Meet example at <https://meet.livekit.io>, or the
custom iOS client in [`../ios/`](../ios/)). The agent creates a tmux session named
`agent` if one doesn't exist and attaches to it; it sizes the window to `TMUX_COLS`
× `TMUX_ROWS`.

## Claude Code integration

The system prompt teaches the agent to distinguish a plain shell from Claude Code
and to use the correct input mode (`send_text` + Enter inside Claude Code,
`run_command` at a shell). It also knows Claude Code's slash commands (`/init`,
`/branch`, …), `!`/`@`/`&` prefixes, and keybindings (`Shift-Tab`, `M-p`, `C-o`,
etc.).

Two background watchers keep the user in the loop:

- **Prompt watcher** — polls the pane for any question followed by
  consecutively numbered options (Claude Code's permission prompts have 2, 3
  or more) and tells the agent the exact options. The agent sends the
  matching number. For "no" it uses the option labelled "No…", or `Escape`
  if there isn't one. It waits until neither you nor the agent is talking.
- **Completion watcher** — detects Claude Code busy→idle transitions (via the
  "esc to interrupt" / "Thinking…" cues in the bottom few lines only) and
  nudges the agent to summarize what Claude did. It holds the summary until
  nobody is talking and skips it when a prompt is on screen.

## Voice tools the agent has

| Tool | What it does |
|------|--------------|
| `run_command(command, confirmed=False)` | Types `command` in the pane and presses Enter. Shell use. Destructive commands need `confirmed=true` after a spoken yes. |
| `send_text(text, press_enter=False, confirmed=False)` | Types literal characters; Enter is opt-in. Use for interactive prompts / Claude Code. Same confirmation rule when submitting. |
| `send_key(key)` | Sends a tmux-style key name: `Enter`, `Tab`, `Escape`, `Up`, `C-c`, `C-l`, `S-Tab`, `M-p`, … |
| `read_screen()` | Returns the visible pane text. The agent calls this to decide what to do. |
| `read_scrollback(lines=200)` | Returns the visible pane plus `lines` rows of tmux scrollback (clamped to 50–2000). Used when the user asks about earlier output that's already scrolled off. |
| `wait_for_output(seconds=2.0)` | Sleep (clamped to 0.5–10s), then return the visible pane. Use after launching slow starters (`claude`, `vim`, `npm install`, `ssh`, …) before concluding a command failed. |
| `list_sessions()` / `switch_session(name)` | Enumerate / switch tmux sessions (exact name match); creates one if missing. Publishes the `tmux.session` participant attribute so clients see the change. |
| `list_windows()` / `switch_window(target)` | Enumerate / select windows by index or name in the current session. |

## HTTP tunnel

When the iOS client (see [`../ios/`](../ios/)) opens a `tunnel://` URL, it
writes a serialized HTTP request to the `http.request` byte-stream topic.
The agent forwards it to `PROXY_TARGET` (default `http://localhost:3000`)
via `aiohttp` and streams the response back on `http.response`. Errors
are returned as styled HTML so the WKWebView shows a readable page.

- Bodies are decompressed by aiohttp, so `Content-Encoding` is dropped from
  the response.
- Absolute redirects to the dev server (`Location: http://localhost:3000/x`)
  are rewritten to `tunnel://localhost/x` so they stay in the tunnel.
- Repeated response headers (e.g. several `Set-Cookie`) are joined with `, `.
- Requests from identities outside `TMUX_ALLOWED_IDENTITIES` get a 403.
- Not supported: WebSockets (dev-server HMR), and absolute
  `http://localhost:3000/...` URLs embedded in pages.

> We would prefer a transparent per-app VPN so the webview could load
> `http://localhost:3000` directly, but on iOS 17
> `WKWebsiteDataStore.proxyConfigurations` is silently ignored for
> non-browser apps (it requires Apple's browser-engine entitlement). The
> custom `tunnel://` scheme is the workaround; see `ios/README.md` for
> details.

## Configuration (optional env vars)

Grid, font, frame rate:

| Var | Default | Notes |
|-----|---------|-------|
| `TMUX_AGENT_NAME` | `tmux-agent` | Explicit-dispatch name. Empty = automatic dispatch into every room. |
| `TMUX_ALLOWED_IDENTITIES` | *(empty)* | Comma-separated identities allowed to drive the agent. |
| `TMUX_LLM_MODEL` | `openai/gpt-realtime` | LiveKit Inference realtime model. |
| `TMUX_SESSION_NAME` | `agent` | tmux session attached to / created. |
| `TMUX_LOCK_SIZE` | `1` | Set `window-size manual` so other attached clients can't resize the window. |
| `TMUX_PROXY_TARGET` | `http://localhost:3000` | Where tunnel requests go. |
| `TMUX_PROXY_TIMEOUT` | `30` | Upstream timeout (seconds). |
| `TMUX_COLS` / `TMUX_ROWS` | `100` / `30` | Terminal grid size. |
| `TMUX_FONT_SIZE` | `20` | Cell size scales with font. |
| `TMUX_FONT_PATH` | auto-detect | Path to a monospace `.ttf`/`.ttc`. |
| `TMUX_FPS` | `10` | Capture / publish frame rate. |
| `TMUX_MAX_BITRATE` | `8_000_000` | Advisory; codec may choose lower. |

Publish pipeline (rarely need to touch — defaults match the verified-working path):

| Var | Default | Notes |
|-----|---------|-------|
| `TMUX_COMPAT_VIDEO` | `1` | Use the "compat" publish path (RGBA, minimal `TrackPublishOptions`, default 1280×720 16:9). Set `0` to use the advanced path with codec/bitrate/framerate control. |
| `TMUX_COMPAT_WIDTH` / `TMUX_COMPAT_HEIGHT` | `1280` / `720` | Letterboxed publish size in compat mode. 16:9 matches modern widescreen displays; bump or drop for sharpness/perf. |
| `TMUX_COMPAT_TRACK_SOURCE` | `screenshare` | Or `camera`. Compat-mode track source. |
| `TMUX_OUT_WIDTH` / `TMUX_OUT_HEIGHT` | `1280` / `720` | Publish size when `TMUX_COMPAT_VIDEO=0`. |
| `TMUX_VIDEO_CODEC` | `h264` | Non-compat path only. One of `h264`, `h265`/`hevc`, `vp8`, `vp9`, `av1`. |
| `TMUX_TRACK_SOURCE` | `screenshare` | Non-compat track source (`screenshare` or `camera`). |
| `TMUX_SCREENCAST` | `0` | Non-compat: set `is_screencast=True` on the `VideoSource` (enables LiveKit screencast heuristics). |
| `TMUX_STREAM_STATIC_PNG` | `0` | Publish `res/static_share.png` instead of tmux — pipeline diagnostic. |
| `TMUX_STATIC_IMAGE` | `res/static_share.png` | Path to the PNG used when `TMUX_STREAM_STATIC_PNG=1`. |

## Architecture notes

The terminal-to-video pipeline has three pieces:

1. **Capture**: `tmux capture-pane -p -e -t <session>` → ANSI-escaped text.
2. **Render**: `pyte.Screen` parses the ANSI into a cell grid; PIL draws each cell
   to an RGBA `Image`. Glyph colors resolved from a named xterm palette.
3. **Publish**: the RGBA pixels are written **in place** into a persistent
   `bytearray` (via `np.frombuffer` → `np.copyto`), and every frame wraps that
   same bytearray in a fresh `rtc.VideoFrame`. The LiveKit FFI stores the
   buffer pointer for asynchronous read by the Rust encoder; reusing the same
   buffer keeps the pointer stable. The pattern is lifted from
   [`python-sdks/examples/publish_hue.py`](../../python-sdks/examples/publish_hue.py).

Capture and render run in a worker thread (`asyncio.to_thread`) so tmux
subprocess calls and PIL drawing don't stall the realtime audio on the event
loop. The renderer returns the cached image when the pane text hasn't changed,
and the publisher skips the pad/copy for an unchanged frame. If the loop falls
behind, it resyncs its clock instead of sending a burst of catch-up frames.

## Troubleshooting

- **Green / rainbow striping in the browser** — you're handing the FFI a fresh
  short-lived `bytes` object per frame and the Rust encoder reads garbage when
  Python GCs it. Use one persistent `bytearray` and mutate it in place.
- **Video works for one frame then nothing** — the entrypoint is exiting after
  `await session.start(...)` returns. Keep the entrypoint alive with
  `await asyncio.Event().wait()` so your background video task isn't cancelled.
- **Opus experimental error from `recorder_io`** — we disable audio recording
  via `session.start(..., record={"audio": False})`. Ffmpeg's Opus encoder is
  flagged experimental and PyAV's `strict=False` doesn't grant that flag.
- **Agent replies in the wrong language** — the `TmuxAgent.on_enter` calls
  `generate_reply` with an explicit English greeting; the system prompt also
  pins English.
- **Text looks blurry in the browser** — match `TMUX_COMPAT_WIDTH` /
  `TMUX_COMPAT_HEIGHT` to the pane's native render size so there's no
  downscale.
- **Pipeline diagnostic** — set `TMUX_STREAM_STATIC_PNG=1` to publish
  `res/static_share.png` instead of the terminal. If that renders correctly,
  the publish path is fine and the problem is in capture/render.
- **Agent never joins the room** — with explicit dispatch, something has to
  request `TMUX_AGENT_NAME`: the iOS app/token server do it via the token; for
  other clients use `lk dispatch create`. Also check the log for "ignoring
  participant" (identity not in `TMUX_ALLOWED_IDENTITIES`).
- **"CONFIRMATION_REQUIRED" in the logs** — the destructive-command guard
  held a command. Say "yes" when the agent reads it back.
