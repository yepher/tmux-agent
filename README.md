# tmux_agent

A LiveKit voice agent that shares a `tmux` session as a screen-share video track and
lets you drive the terminal by voice. When you connect to the room you see the tmux
pane live; when you talk, the agent runs commands, sends keys, switches sessions or
windows, and narrates what it's doing.

Built on `livekit-agents` + OpenAI Realtime. Terminal rendering goes through `pyte`
so ANSI colors and attributes survive.

[![Watch the demo](https://img.youtube.com/vi/yQYDYB9REl0/maxresdefault.jpg)](https://youtu.be/yQYDYB9REl0)

## What it does

- **Screen share** — captures the active tmux pane every frame, renders it to an
  RGBA image (monospace font, color-preserving via `pyte`), publishes it as a
  `SOURCE_SCREENSHARE` video track at 10 FPS.
- **Voice control** — voice in/out via OpenAI Realtime. The LLM has tools to type
  text, send keys, run commands, read the screen, and switch sessions/windows.
- **Context awareness** — the system prompt teaches the agent to distinguish a
  plain shell from Claude Code and to use the correct input mode (`send_text`
  plus Enter inside Claude Code, `run_command` at a shell).

## Requirements

- macOS or Linux with `tmux` on `$PATH`.
- Python 3.10+.
- A LiveKit project (URL + API key/secret).
- An OpenAI API key (Realtime model).

## Setup

```bash
cd tmux_agent
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

```bash
python tmux_agent.py dev     # hot-reload, connects to LiveKit
python tmux_agent.py start   # production
python tmux_agent.py console # local audio, no LiveKit room
```

Dispatch a job from the LiveKit Cloud Agents playground or your own frontend, then
join the room from a client (e.g. the Meet example at <https://meet.livekit.io>).
The agent creates a tmux session named `agent` if one doesn't exist and attaches
to it; it sizes the window to `TMUX_COLS` × `TMUX_ROWS`.

## Voice tools the agent has

| Tool | What it does |
|------|--------------|
| `run_command(command)` | Types `command` in the pane and presses Enter. Shell use. |
| `send_text(text, press_enter=False)` | Types literal characters; Enter is opt-in. Use for interactive prompts / Claude Code. |
| `send_key(key)` | Sends a tmux-style key name: `Enter`, `Tab`, `Escape`, `Up`, `C-c`, `C-l`, `S-Tab`, `M-p`, … |
| `read_screen()` | Returns the visible pane text. The agent calls this to decide what to do. |
| `list_sessions()` / `switch_session(name)` | Enumerate / switch tmux sessions; creates one if missing. |
| `list_windows()` / `switch_window(target)` | Enumerate / select windows by index or name in the current session. |

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
| `TMUX_COMPAT_WIDTH` / `TMUX_COMPAT_HEIGHT` | `640` / `480` | Letterboxed publish size. Bump to `1280 × 720` if text is too small. |
| `TMUX_COMPAT_TRACK_SOURCE` | `screenshare` | Or `camera`. |
| `TMUX_COMPAT_I420` | `0` | RGBA → I420 conversion before publish. |
| `TMUX_STREAM_STATIC_PNG` | `0` | Publish `res/static_share.png` instead of tmux — pipeline diagnostic. |
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
   [`python-sdks/examples/publish_hue.py`](../python-sdks/examples/publish_hue.py).

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
- **Text looks blurry in the browser** — the pane renders at 1200×720 but
  letterboxes to 640×480 by default. Run with
  `TMUX_COMPAT_WIDTH=1280 TMUX_COMPAT_HEIGHT=720` to keep it near 1:1.
- **First-frame diagnostic** — set `TMUX_DEBUG_RAW=1` to dump the raw RGBA of
  the first frame; verify it with
  `ffplay -f rawvideo -pixel_format rgba -video_size <W>x<H> /tmp/tmux_debug_first.rgba`.
  If ffplay shows it correctly but the browser doesn't, the renderer is fine
  and the bug is downstream.
