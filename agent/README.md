# agent — tmux voice agent (server)

Python LiveKit agent that attaches to a `tmux` session, publishes the active pane as
a screen-share video track, and accepts voice commands via OpenAI Realtime. The LLM
has tools to type text, send keys, run commands, read the screen, and switch
sessions/windows. Terminal rendering goes through `pyte` so ANSI colors and
attributes survive.

## Requirements

- macOS or Linux with `tmux` on `$PATH`.
- Python 3.10+.
- A LiveKit project (URL + API key/secret).
- An OpenAI API key (Realtime model).

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
OPENAI_API_KEY=...
```

## Run

With the venv activated:

```bash
python tmux_agent.py dev     # hot-reload, connects to LiveKit
python tmux_agent.py start   # production
python tmux_agent.py console # local audio, no LiveKit room
```

Dispatch a job from the LiveKit Cloud Agents playground or your own frontend, then
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

- **Prompt watcher** — polls the pane for the "Do you want to proceed? 1. Yes /
  2. Yes always / 3. No" permission prompt and speaks the question so the user
  knows to answer. Maps "yes" / "yes always" / "no" to `send_text("1"|"2"|"3",
  enter=True)`.
- **Completion watcher** — detects Claude Code busy→idle transitions (via the
  "esc to interrupt" / "Thinking…" cues) and nudges the agent to read the screen
  and summarize what Claude just did in one or two sentences.

## Voice tools the agent has

| Tool | What it does |
|------|--------------|
| `run_command(command)` | Types `command` in the pane and presses Enter. Shell use. |
| `send_text(text, press_enter=False)` | Types literal characters; Enter is opt-in. Use for interactive prompts / Claude Code. |
| `send_key(key)` | Sends a tmux-style key name: `Enter`, `Tab`, `Escape`, `Up`, `C-c`, `C-l`, `S-Tab`, `M-p`, … |
| `read_screen()` | Returns the visible pane text. The agent calls this to decide what to do. |
| `wait_for_output(seconds=2.0)` | Sleep (clamped to 0.5–10s), then return the visible pane. Use after launching slow starters (`claude`, `vim`, `npm install`, `ssh`, …) before concluding a command failed. |
| `list_sessions()` / `switch_session(name)` | Enumerate / switch tmux sessions; creates one if missing. |
| `list_windows()` / `switch_window(target)` | Enumerate / select windows by index or name in the current session. |

## HTTP tunnel

When the iOS client (see [`../ios/`](../ios/)) opens a `tunnel://` URL, it writes
a serialized `HTTPRequest` to the `http.request` byte-stream topic. The agent
forwards it to `PROXY_TARGET` (default `http://localhost:3000`) via `aiohttp` and
streams the response back on `http.response`. This lets the phone view a dev
site running on the agent's machine. Errors are returned as styled HTML so the
WKWebView can render them.

## Configuration (optional env vars)

Grid, font, frame rate:

| Var | Default | Notes |
|-----|---------|-------|
| `TMUX_SESSION_NAME` | `agent` | tmux session attached to / created. |
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
| `TMUX_COMPAT_I420` | `0` | RGBA → I420 conversion before publish (compat mode). |
| `TMUX_OUT_WIDTH` / `TMUX_OUT_HEIGHT` | `1280` / `720` | Publish size when `TMUX_COMPAT_VIDEO=0`. |
| `TMUX_VIDEO_CODEC` | `h264` | Non-compat path only. One of `h264`, `h265`/`hevc`, `vp8`, `vp9`, `av1`. |
| `TMUX_TRACK_SOURCE` | `screenshare` | Non-compat track source (`screenshare` or `camera`). |
| `TMUX_USE_I420` | `0` | Non-compat: RGB24 → I420 conversion before publish. |
| `TMUX_SCREENCAST` | `0` | Non-compat: set `is_screencast=True` on the `VideoSource` (enables LiveKit screencast heuristics). |
| `TMUX_STREAM_STATIC_PNG` | `0` | Publish `res/static_share.png` instead of tmux — pipeline diagnostic. |
| `TMUX_STATIC_IMAGE` | `res/static_share.png` | Path to the PNG used when `TMUX_STREAM_STATIC_PNG=1`. |
| `TMUX_TEST_MODE` | `0` | Publish a colored test card instead of tmux. |
| `TMUX_DEBUG_RAW` | `0` | Dump first frame to `/tmp/tmux_debug_first.rgba` for `ffplay` verification. |

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
- **First-frame diagnostic** — set `TMUX_DEBUG_RAW=1` to dump the raw RGBA of
  the first frame; verify it with
  `ffplay -f rawvideo -pixel_format rgba -video_size <W>x<H> /tmp/tmux_debug_first.rgba`.
  If ffplay shows it correctly but the browser doesn't, the renderer is fine
  and the bug is downstream.
