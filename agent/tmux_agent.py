"""LiveKit agent that shares a tmux session as a screen-share video track and
accepts voice commands (via OpenAI Realtime) to interact with it.

Uses `pyte` as a virtual terminal to render colors and attributes captured from
tmux via `capture-pane -e`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
import numpy as np
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.agents.llm import function_tool
from livekit.plugins import openai
from PIL import Image, ImageDraw, ImageFont, ImageOps

from tmux_helper import DEFAULT_BG, TmuxHelper, find_font

load_dotenv()

logger = logging.getLogger("tmux-agent")
logger.setLevel(logging.INFO)

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_STATIC_PNG = _SCRIPT_DIR / "res" / "static_share.png"
# Absolute or relative path to a PNG to stream (isolates WebRTC from tmux rendering).
TMUX_STATIC_IMAGE = os.getenv("TMUX_STATIC_IMAGE", str(_DEFAULT_STATIC_PNG))
# 1 = stream only the PNG (padded to OUT_WIDTH×OUT_HEIGHT). 0 = live tmux. Empty = auto
# (use bundled res/static_share.png when that file exists).
# Default OFF — set TMUX_STREAM_STATIC_PNG=1 to publish the bundled PNG
# instead of live tmux. Used only as a publish-pipeline diagnostic.
STREAM_STATIC_PNG = os.getenv("TMUX_STREAM_STATIC_PNG", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

# HTTP proxy: the iOS app tunnels WKWebView requests to us over a LiveKit
# byte stream; we re-issue them against this base URL on the agent's machine
# and stream the response back. Typical use: Claude runs a dev server on
# localhost:3000 and the phone views it through the tunnel.
#
# We can't use a transparent per-app VPN on iOS because
# `WKWebsiteDataStore.proxyConfigurations` is silently ignored for
# non-browser apps (requires Apple's browser-engine entitlement). See
# `ios/README.md` for details.
PROXY_TARGET = os.getenv("TMUX_PROXY_TARGET", "http://localhost:3000").rstrip("/")
PROXY_REQUEST_TOPIC = os.getenv("TMUX_PROXY_REQ_TOPIC", "http.request")
PROXY_RESPONSE_TOPIC = os.getenv("TMUX_PROXY_RES_TOPIC", "http.response")
PROXY_TIMEOUT = float(os.getenv("TMUX_PROXY_TIMEOUT", "30"))

TMUX_SESSION = os.getenv("TMUX_SESSION_NAME", "agent")
COLS = int(os.getenv("TMUX_COLS", "100"))
ROWS = int(os.getenv("TMUX_ROWS", "30"))
FONT_SIZE = int(os.getenv("TMUX_FONT_SIZE", "20"))
FPS = int(os.getenv("TMUX_FPS", "10"))
MAX_BITRATE = int(os.getenv("TMUX_MAX_BITRATE", "8000000"))
# When set, publish a synthetic test card instead of the tmux pane —
# isolates the webrtc publish pipeline from tmux + pyte + text rendering.
TEST_MODE = os.getenv("TMUX_TEST_MODE", "").lower() in ("1", "true", "yes")
# Optional: RGB24 → I420 before capture_frame. Default off — publish RGB24 (3 bpp),
# which matches LiveKit Python guidance for synthetic/camera frames (see python-sdks
# issues re meet.livekit.io). RGBA→I420 was still producing green garbage for some users.
USE_I420 = os.getenv("TMUX_USE_I420", "0").lower() in ("1", "true", "yes")
# Match browsing_agent's browser screenshare (VideoSource default). True enables
# LiveKit screencast heuristics; try False if you see encoder issues.
SCREENCAST_SOURCE = os.getenv("TMUX_SCREENCAST", "0").lower() in ("1", "true", "yes")
# Encode/publish at a standard HD size. Odd sizes (e.g. 1200-wide) can confuse
# simulcast/SFU layers and produce green/corrupt video; we letterbox the terminal.
OUT_WIDTH = int(os.getenv("TMUX_OUT_WIDTH", "1280"))
OUT_HEIGHT = int(os.getenv("TMUX_OUT_HEIGHT", "720"))
_VIDEO_CODEC_STR = os.getenv("TMUX_VIDEO_CODEC", "h264").strip().lower()
# "screenshare" | "camera" — use camera if your client mishandles screenshare tracks.
_TRACK_SRC_STR = os.getenv("TMUX_TRACK_SOURCE", "screenshare").strip().lower()
# Simple pipeline: RGBA frames + minimal TrackPublishOptions (no forced codec).
# Defaults match browsing_agent browser_manager screenshare: 640×480, SOURCE_SCREENSHARE.
COMPAT_VIDEO = os.getenv("TMUX_COMPAT_VIDEO", "1").lower() in ("1", "true", "yes")
COMPAT_WIDTH = int(os.getenv("TMUX_COMPAT_WIDTH", "1280"))
COMPAT_HEIGHT = int(os.getenv("TMUX_COMPAT_HEIGHT", "720"))
# Match browsing_agent/browser_manager exactly: screenshare source, no I420 conversion.
_COMPAT_TRACK_SRC = os.getenv("TMUX_COMPAT_TRACK_SOURCE", "screenshare").strip().lower()
# RGBA → I420 before capture_frame. Default OFF — the browsing_agent reference
# passes RGBA straight through, and the I420 conversion path was producing
# green/striped output in this setup.
COMPAT_I420 = os.getenv("TMUX_COMPAT_I420", "0").lower() in ("1", "true", "yes")
# Write /tmp/tmux_debug_first.rgba on first frame; verify with ffplay before blaming WebRTC.
DEBUG_RAW_FRAME = os.getenv("TMUX_DEBUG_RAW", "").lower() in ("1", "true", "yes")

def _error_html(status: int, title: str, message: str) -> bytes:
    """Render a minimal mobile-friendly HTML error page for the proxy so that
    WKWebView actually shows a readable error instead of blank/old content."""
    import html as _html
    safe_title = _html.escape(title)
    safe_msg = _html.escape(message).replace("\n", "<br>")
    body = (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{status} {safe_title}</title>"
        f"<style>"
        f"body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        f"background:#1b1b1f;color:#eee;margin:0;padding:2em;"
        f"line-height:1.5;-webkit-text-size-adjust:none}}"
        f".card{{max-width:600px;margin:0 auto;"
        f"background:#2a2a30;border-radius:12px;padding:1.5em;"
        f"border:1px solid #5c2222}}"
        f".badge{{display:inline-block;padding:0.2em 0.6em;"
        f"border-radius:6px;background:#b0413e;color:#fff;"
        f"font-size:0.9em;font-weight:600;margin-bottom:0.8em}}"
        f".title{{font-size:1.3em;margin:0 0 0.8em}}"
        f".msg{{color:#ccc;white-space:pre-wrap;"
        f"font-family:ui-monospace,Menlo,monospace;font-size:0.95em}}"
        f".foot{{margin-top:1.2em;color:#888;font-size:0.82em}}"
        f"</style></head><body><div class='card'>"
        f"<span class='badge'>{status}</span>"
        f"<h1 class='title'>{safe_title}</h1>"
        f"<div class='msg'>{safe_msg}</div>"
        f"<div class='foot'>tmux-agent proxy → {PROXY_TARGET}</div>"
        f"</div></body></html>"
    )
    return body.encode("utf-8")


async def _run_http_proxy_request(
    room: rtc.Room,
    reader: rtc.ByteStreamReader,
    remote_identity: str,
    http: aiohttp.ClientSession,
) -> None:
    """Handle a single proxied HTTP request from the mobile app.

    Wire format (see ios/README.md):
      Request stream attributes:
        id, method, path, headers (json dict string)
        Stream body: request body bytes (possibly empty).
      Response stream attributes:
        id, status, status_text, headers (json dict string)
        Stream body: response body bytes.
    """
    attrs = dict(reader.info.attributes or {})
    req_id = attrs.get("id", reader.info.stream_id)
    method = attrs.get("method", "GET").upper()
    path = attrs.get("path", "/")
    try:
        headers = json.loads(attrs.get("headers", "{}") or "{}")
    except json.JSONDecodeError:
        headers = {}

    body_chunks: list[bytes] = []
    async for chunk in reader:
        body_chunks.append(chunk)
    body = b"".join(body_chunks) if body_chunks else None

    url = f"{PROXY_TARGET}{path if path.startswith('/') else '/' + path}"
    logger.info(
        "proxy %s %s (id=%s, %d bytes in)", method, url, req_id, len(body or b"")
    )

    hop = {"host", "connection", "content-length", "transfer-encoding"}
    fwd_headers = {k: v for k, v in headers.items() if k.lower() not in hop}

    status = 502
    status_text = "Bad Gateway"
    res_headers: dict[str, str] = {}
    res_body = b""
    try:
        async with http.request(
            method, url,
            headers=fwd_headers, data=body,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=PROXY_TIMEOUT),
        ) as resp:
            status = resp.status
            status_text = resp.reason or ""
            res_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in hop
            }
            res_body = await resp.read()
    except asyncio.TimeoutError:
        status, status_text = 504, "Gateway Timeout"
        res_body = _error_html(
            status, "Gateway Timeout",
            f"The agent's upstream at {PROXY_TARGET} didn't respond within "
            f"{PROXY_TIMEOUT:.0f}s.\n\nCheck that your dev server (e.g. "
            f"npm run dev) is still running.",
        )
        res_headers = {"Content-Type": "text/html; charset=utf-8"}
    except aiohttp.ClientError as e:
        status, status_text = 502, "Bad Gateway"
        res_body = _error_html(
            status, "Bad Gateway",
            f"The agent could not reach {PROXY_TARGET}.\n\n{e}\n\n"
            f"Start something on that port (e.g. npm run dev, python -m "
            f"http.server 3000) and refresh.",
        )
        res_headers = {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        logger.exception("proxy request failed")
        status, status_text = 500, "Internal Server Error"
        res_body = _error_html(
            status, "Proxy Error", f"Unexpected error in the agent proxy: {e}"
        )
        res_headers = {"Content-Type": "text/html; charset=utf-8"}

    logger.info(
        "proxy %s %s -> %d (id=%s, %d bytes out)",
        method, url, status, req_id, len(res_body),
    )

    writer = await room.local_participant.stream_bytes(
        name=f"response-{req_id}",
        topic=PROXY_RESPONSE_TOPIC,
        destination_identities=[remote_identity],
        attributes={
            "id": req_id,
            "status": str(status),
            "status_text": status_text,
            "headers": json.dumps(res_headers),
        },
        total_size=len(res_body),
        mime_type=res_headers.get(
            "Content-Type", "application/octet-stream"
        ).split(";")[0].strip(),
    )
    try:
        if res_body:
            await writer.write(res_body)
    finally:
        await writer.aclose()


def _load_static_share_image(out_w: int, out_h: int) -> Image.Image:
    """Load TMUX_STATIC_IMAGE (PNG), RGBA, letterboxed to out_w×out_h."""
    path = Path(TMUX_STATIC_IMAGE).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"Static image not found: {path}. Set TMUX_STATIC_IMAGE or add res/static_share.png"
        )
    img = Image.open(path).convert("RGBA")
    return _pad_terminal_image(img, out_w, out_h)


def _video_frame_like_video_play(img: Image.Image) -> rtc.VideoFrame:
    """Build a VideoFrame matching browsing_agent/browser_manager exactly:
    PIL RGBA → img.tobytes() → rtc.VideoFrame(..., RGBA, ...).
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    rgba_vf = rtc.VideoFrame(w, h, rtc.VideoBufferType.RGBA, img.tobytes())
    if COMPAT_I420:
        return rgba_vf.convert(rtc.VideoBufferType.I420)
    return rgba_vf


def _compat_track_source_enum() -> int:
    if _COMPAT_TRACK_SRC in ("camera", "cam"):
        return rtc.TrackSource.SOURCE_CAMERA
    return rtc.TrackSource.SOURCE_SCREENSHARE


def _build_publish_options_compat() -> rtc.TrackPublishOptions:
    """Match browsing_agent/browser_manager: minimal options, only the track source."""
    return rtc.TrackPublishOptions(source=_compat_track_source_enum())


def _video_frame_from_pil_rgba(img: Image.Image) -> rtc.VideoFrame:
    """PIL RGBA → rtc VideoFrame. Uses RGB24 (not RGBA), per LiveKit Python examples."""
    w, h = img.size
    rgb = img.convert("RGB")
    data = rgb.tobytes()
    rgb_frame = rtc.VideoFrame(w, h, rtc.VideoBufferType.RGB24, data)
    if USE_I420:
        return rgb_frame.convert(rtc.VideoBufferType.I420)
    return rgb_frame


def _pad_terminal_image(img: Image.Image, out_w: int, out_h: int) -> Image.Image:
    """Letterbox terminal render to a standard output size (default 1280x720)."""
    if img.size == (out_w, out_h):
        return img
    return ImageOps.pad(
        img,
        (out_w, out_h),
        method=Image.Resampling.LANCZOS,
        color=DEFAULT_BG,
    )


def _video_codec_from_env() -> int:
    m = {
        "vp8": rtc.VideoCodec.VP8,
        "h264": rtc.VideoCodec.H264,
        "h265": rtc.VideoCodec.H265,
        "hevc": rtc.VideoCodec.H265,
        "vp9": rtc.VideoCodec.VP9,
        "av1": rtc.VideoCodec.AV1,
    }
    return m.get(_VIDEO_CODEC_STR, rtc.VideoCodec.H264)


def _track_source_from_env() -> int:
    if _TRACK_SRC_STR in ("camera", "cam"):
        return rtc.TrackSource.SOURCE_CAMERA
    return rtc.TrackSource.SOURCE_SCREENSHARE


def _build_publish_options() -> rtc.TrackPublishOptions:
    opts = rtc.TrackPublishOptions()
    opts.source = _track_source_from_env()
    opts.video_codec = _video_codec_from_env()
    ve = rtc.VideoEncoding()
    ve.max_bitrate = MAX_BITRATE
    ve.max_framerate = max(FPS, 1)
    opts.video_encoding.CopyFrom(ve)
    return opts


INSTRUCTIONS = """\
You are a voice-controlled assistant that drives a shared tmux terminal the user can see.
Always speak and respond in English, regardless of what language the user speaks first.

## Tool choice
- `run_command` for normal shell commands (`ls`, `cd`, `git status`, etc.). Types the
  command and presses Enter.
- `send_text` for typing characters without pressing Enter, or into interactive prompts.
  Set `press_enter=True` to also submit.
- `send_key` for named keys: `Enter`, `Tab`, `Escape`, `Up`/`Down`/`Left`/`Right`,
  `C-c` (Ctrl+C), `C-d`, `C-l` (clear), `M-p` (Alt+p), etc.
- `read_screen` to read what's currently visible before deciding what to do.
- `wait_for_output(seconds)` after launching something that takes time to render
  (`claude`, `vim`, `less`, `htop`, `nano`, `npm install`, `ssh`, `docker build`, …).
  Default 2s; bump to 4-6s for heavier startups. **Do not conclude a command failed
  from a single quick read_screen** — prefer `wait_for_output` first.

## Context awareness — shell vs Claude Code
Before acting, check the screen. You are in one of two modes:

**Plain shell** — prompt ends in `$`, `%`, `#`, or `>`. Use `run_command` freely
and answer the user's question yourself from the terminal output.

**Claude Code** (an interactive coding assistant running in the pane) — telltale
signs: prompt starts with `>`; hint line like `? for shortcuts`; slash-command menu
visible; mentions of `/init`, `/agents`, `/branch`, etc.

When in Claude Code mode your role is a **voice-to-Claude-Code proxy**, not an
assistant that answers on your own:

- Forward the user's request to Claude Code by typing it with
  `send_text(..., press_enter=True)`. Use the user's own wording — you may clean
  up obvious speech-to-text artifacts ("um", stutters), but do NOT rephrase,
  expand, or add preamble like "Sure, let me look at…". Keep it terse, like the
  user typed it.
- Do NOT answer the request yourself from `read_screen` output. Claude Code is
  the one answering. You only carry the message and, after it runs, describe
  briefly in one sentence what Claude Code did or is waiting on.
- Do NOT use `run_command` in this mode — it would send a shell command line,
  but you're not at a shell; the text would become a Claude Code prompt.
- Meta-requests about the terminal itself — "exit Claude", "clear the screen",
  "switch sessions", "what shortcut is that" — you handle directly (tools or
  answering). Don't forward those to Claude Code.
  - "exit Claude" / "quit Claude" / "close Claude" → `send_text("exit", press_enter=True)`.
    Do NOT use `send_key("C-c")` — Ctrl+C interrupts the current response but
    leaves Claude running; typing `exit` is the clean way out.
- If the user's request is ambiguous (task vs meta), ask one short clarifying
  question before typing anything.

### Claude Code input conventions (use `send_text`, then Enter)
- `!<cmd>` — run shell in Claude Code's bash mode (e.g. `!ls -la`)
- `/<command>` — slash command (e.g. `/init`, `/branch`, `/agents`, `/add-dir`,
  `/advisor`, `/autofix-pr`, `/btw`, `/keybindings`)
- `@<path>` — reference a file path
- `&<task>` — run as a background task

### Claude Code keybindings (use `send_key`)
- `Escape` twice (back-to-back `send_key("Escape")` calls) — clear current input
- `Shift-Tab` — toggle auto-accept edits (`send_key("S-Tab")`)
- `C-o` — toggle verbose output
- `C-t` — toggle task list
- `C-z` — suspend
- `C-s` — stash prompt
- `C-g` — edit in $EDITOR
- `M-p` — switch model
- `S-Enter` — newline within prompt (`send_key("S-Enter")`)

## Answering Claude Code confirmation prompts
When Claude Code asks a permission question, the screen shows a line like
"Do you want to proceed?" followed by:
  1. Yes
  2. Yes, and don't ask again …
  3. No, tell Claude what to do differently

A background watcher notifies the user automatically when it sees such a prompt.
When the user responds, answer with `send_text` of the option number and Enter:
- user says "yes" → `send_text("1", press_enter=True)`
- user says "yes always" / "don't ask again" / "always" → `send_text("2", press_enter=True)`
- user says "no" / "cancel" / "stop" → `send_text("3", press_enter=True)`
If the user gives guidance to pass to Claude instead of a numeric choice, choose
option 3 and then send their guidance via `send_text(..., press_enter=True)`.

## Style
Narrate briefly what you're about to do (one short sentence), then run it. After it
completes, summarize in one or two sentences. Keep responses concise and terminal-aware.
Do not read long file contents aloud unless asked.
"""


class TmuxAgent(Agent):
    def __init__(self, tools):
        super().__init__(instructions=INSTRUCTIONS, tools=tools)

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions=(
                "Greet the user briefly in English and ask what they'd like "
                "to do in the terminal."
            )
        )


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    # Register the HTTP-proxy byte-stream handler FIRST, before video setup,
    # so a client that fires a request the instant the agent joins doesn't
    # get "ignoring byte stream ... no callback attached". One aiohttp
    # ClientSession is shared across requests for connection reuse.
    http_session = aiohttp.ClientSession()
    proxy_tasks: set[asyncio.Task[None]] = set()

    def _on_proxy_request(
        reader: rtc.ByteStreamReader, remote_identity: str
    ) -> None:
        task = asyncio.create_task(
            _run_http_proxy_request(
                ctx.room, reader, remote_identity, http_session
            )
        )
        proxy_tasks.add(task)
        task.add_done_callback(proxy_tasks.discard)

    try:
        ctx.room.unregister_byte_stream_handler(PROXY_REQUEST_TOPIC)
    except Exception:
        pass
    ctx.room.register_byte_stream_handler(PROXY_REQUEST_TOPIC, _on_proxy_request)
    logger.info(
        "http proxy ready: topic=%s target=%s",
        PROXY_REQUEST_TOPIC, PROXY_TARGET,
    )

    tmux = TmuxHelper(TMUX_SESSION, COLS, ROWS, FONT_SIZE)
    tmux.ensure()

    if COMPAT_VIDEO:
        vid_w, vid_h = COMPAT_WIDTH, COMPAT_HEIGHT
        publish_options = _build_publish_options_compat()
    else:
        vid_w, vid_h = OUT_WIDTH, OUT_HEIGHT
        publish_options = _build_publish_options()

    static_frame: Image.Image | None = None
    if STREAM_STATIC_PNG:
        try:
            static_frame = _load_static_share_image(vid_w, vid_h)
            # PIL (width, height) is the single source of truth for stride = width * 4.
            vid_w, vid_h = static_frame.size
            logger.info(
                "streaming static PNG -> %dx%d (from %s)",
                vid_w,
                vid_h,
                Path(TMUX_STATIC_IMAGE).expanduser(),
            )
        except OSError as e:
            logger.error("static PNG mode failed (%s); falling back to tmux video", e)
            static_frame = None

    logger.info(
        "render %dx%d -> publish %dx%d compat_video=%s compat_track=%s compat_i420=%s (cell %dx%d, grid %dx%d) "
        "codec=%s track_src=%s rgb24_i420=%s screencast_src=%s static_png=%s",
        tmux.render_width,
        tmux.render_height,
        vid_w,
        vid_h,
        COMPAT_VIDEO,
        _COMPAT_TRACK_SRC if COMPAT_VIDEO else "n/a",
        COMPAT_I420 if COMPAT_VIDEO else False,
        tmux.cell_size.w,
        tmux.cell_size.h,
        COLS,
        ROWS,
        _VIDEO_CODEC_STR,
        _TRACK_SRC_STR,
        USE_I420,
        SCREENCAST_SOURCE,
        static_frame is not None,
    )

    # video_play.py uses plain VideoSource(w,h) — is_screencast defaults False.
    source = rtc.VideoSource(
        vid_w,
        vid_h,
        is_screencast=False if COMPAT_VIDEO else SCREENCAST_SOURCE,
    )
    track = rtc.LocalVideoTrack.create_video_track("tmux-screen", source)
    pub = await ctx.room.local_participant.publish_track(track, publish_options)
    logger.info("published video track sid=%s", pub.sid)

    ow, oh = vid_w, vid_h
    test_img = Image.new("RGBA", (ow, oh), (0, 0, 0, 255))
    td = ImageDraw.Draw(test_img)
    td.rectangle([0, 0, ow // 2, oh // 2], fill=(200, 40, 40, 255))
    td.rectangle([ow // 2, 0, ow, oh // 2], fill=(40, 160, 40, 255))
    td.rectangle([0, oh // 2, ow // 2, oh], fill=(40, 60, 200, 255))
    td.rectangle([ow // 2, oh // 2, ow, oh], fill=(220, 200, 40, 255))
    big_font = ImageFont.truetype(find_font(), min(120, oh // 4))
    td.text((ow // 2 - 140, oh // 2 - 70), "TMUX", font=big_font, fill=(255, 255, 255, 255))
    if TEST_MODE:
        test_img.save("/tmp/tmux_agent_testcard.png")
        logger.info("TEST MODE: publishing static test card (%dx%d)", ow, oh)

    # frame_delta_us = 1_000_000 // max(FPS, 1)

    # ONE persistent bytearray + numpy view over it — matches
    # python-sdks/examples/publish_hue.py. The FFI reads the pixel pointer
    # asynchronously after capture_frame returns; if we hand it a fresh short-lived
    # bytes object each iteration, Python GC can invalidate the pointer before the
    # Rust encoder reads it (→ green/rainbow stripes). A buffer that lives for the
    # lifetime of the source keeps the pointer stable forever.
    frame_buffer = bytearray(vid_w * vid_h * 4)
    frame_view = np.frombuffer(frame_buffer, dtype=np.uint8).reshape(vid_h, vid_w, 4)

    # Follows python-sdks/examples/publish_hue.py: persistent bytearray +
    # numpy view + rtc.VideoFrame(..., frame_buffer) every iteration so the FFI
    # pointer stays stable. Pixel content comes from tmux/PIL; the publish
    # path is otherwise identical to the verified-working hue diagnostic.
    from time import perf_counter

    async def _stream_video() -> None:
        logger.info(
            "stream task start %dx%d FPS=%d buf_len=%d",
            vid_w, vid_h, max(FPS, 1), len(frame_buffer),
        )
        framerate = 1.0 / max(FPS, 1)
        next_frame_time = perf_counter()
        frames = 0
        try:
            while True:
                if TEST_MODE:
                    pub_img = test_img
                elif static_frame is not None:
                    pub_img = static_frame
                else:
                    pub_img = tmux.render_frame()
                if pub_img.size != (vid_w, vid_h):
                    pub_img = _pad_terminal_image(pub_img, vid_w, vid_h)
                if pub_img.mode != "RGBA":
                    pub_img = pub_img.convert("RGBA")
                np.copyto(frame_view, np.asarray(pub_img, dtype=np.uint8))
                frame = rtc.VideoFrame(
                    vid_w, vid_h, rtc.VideoBufferType.RGBA, frame_buffer
                )
                source.capture_frame(frame)
                frames += 1
                if frames <= 5 or frames % 100 == 0:
                    logger.info("pushed frame %d", frames)
                next_frame_time += framerate
                await asyncio.sleep(next_frame_time - perf_counter())
        except asyncio.CancelledError:
            logger.info("stream cancelled at frame %d", frames)
            raise
        except Exception:
            logger.exception("stream died at frame %d", frames)
            raise

    video_task = asyncio.create_task(_stream_video())

    def _tail() -> str:
        return "\n".join(tmux.capture_lines()[-20:])

    @function_tool
    async def run_command(command: str) -> str:
        """Run a shell command in the tmux session by typing it and pressing Enter.

        Use for normal shell commands (e.g. 'ls -la', 'cd ~/code', 'git status').

        Args:
            command: The full shell command to run.
        """
        logger.info("run_command: %s", command)
        tmux.send_text(command, press_enter=True)
        await asyncio.sleep(0.4)
        return _tail()

    @function_tool
    async def send_text(text: str, press_enter: bool = False) -> str:
        """Type literal text into the pane without interpreting it as a shell command.

        Useful for interactive prompts (e.g. answering a y/n prompt, entering a value).

        Args:
            text: The literal characters to type.
            press_enter: If true, press Enter after typing.
        """
        logger.info("send_text: %r (enter=%s)", text, press_enter)
        tmux.send_text(text, press_enter=press_enter)
        await asyncio.sleep(0.25)
        return _tail()

    @function_tool
    async def send_key(key: str) -> str:
        """Send a special key or key combo using tmux key names.

        Examples: 'Enter', 'Tab', 'Escape', 'Up', 'Down', 'Left', 'Right',
        'C-c' (Ctrl+C), 'C-d' (Ctrl+D), 'C-l' (clear screen), 'M-x' (Meta+x).

        Args:
            key: A tmux-style key name.
        """
        logger.info("send_key: %s", key)
        tmux.send_key(key)
        await asyncio.sleep(0.25)
        return _tail()

    @function_tool
    async def read_screen() -> str:
        """Return the current visible contents of the tmux pane (plain text)."""
        return tmux.capture_text()

    @function_tool
    async def wait_for_output(seconds: float = 2.0) -> str:
        """Wait, then return the visible pane contents.

        Use this after launching a program that takes a moment to start or
        render (claude, vim, less, htop, top, nano, npm/pip install, ssh,
        docker build, etc.) so the UI has time to appear before you decide
        whether it worked. Do NOT immediately conclude a command failed if
        the first `read_screen` looks empty — wait and check again.

        Args:
            seconds: How long to wait, clamped to [0.5, 10].
        """
        s = max(0.5, min(seconds, 10.0))
        logger.info("wait_for_output: sleeping %.2fs", s)
        await asyncio.sleep(s)
        return tmux.capture_text()

    @function_tool
    async def list_sessions() -> str:
        """List all tmux sessions on the host. The current session is marked with '*'."""
        sessions = tmux.list_sessions()
        if not sessions:
            return "No tmux sessions found."
        current = tmux.session_name
        return "\n".join(f"{'*' if s == current else ' '} {s}" for s in sessions)

    @function_tool
    async def switch_session(name: str) -> str:
        """Switch the shared video stream to a different tmux session.
        Creates the session if it does not exist.

        Args:
            name: Name of the tmux session to attach to (or create).
        """
        logger.info("switch_session: %s", name)
        tmux.switch_session(name)
        await asyncio.sleep(0.2)
        return f"Now streaming session '{name}'."

    @function_tool
    async def list_windows() -> str:
        """List windows in the current tmux session, marking the active one with '*'."""
        windows = tmux.list_windows()
        if not windows:
            return "No windows found."
        return "\n".join(
            f"{'*' if active else ' '} {idx}: {name}" for idx, name, active in windows
        )

    @function_tool
    async def switch_window(target: str) -> str:
        """Select a window in the current tmux session by index (e.g. '0') or name.

        Args:
            target: Window index or name.
        """
        logger.info("switch_window: %s", target)
        tmux.select_window(target)
        await asyncio.sleep(0.2)
        return f"Selected window '{target}'."

    agent = TmuxAgent(
        tools=[
            run_command,
            send_text,
            send_key,
            read_screen,
            wait_for_output,
            list_sessions,
            switch_session,
            list_windows,
            switch_window,
        ],
    )

    session = AgentSession(llm=openai.realtime.RealtimeModel())

    async def _watch_claude_prompts() -> None:
        """Poll the pane for Claude Code confirmation prompts and speak up.

        Matches a "Do you want …" / "Would you like …" question followed by
        a "1. Yes" / "3. No" option list — the standard Claude Code permission
        prompt shape. Announces each new prompt exactly once via session.say,
        so the user knows to respond. The agent's own prompt instructions tell
        it how to turn the user's reply into send_text("1"|"2"|"3", enter=True).
        """
        last_seen: str | None = None
        while True:
            await asyncio.sleep(0.8)
            try:
                lines = tmux.capture_lines()
                prompt = TmuxHelper.detect_claude_prompt(lines)
                if prompt and prompt != last_seen:
                    last_seen = prompt
                    logger.info("claude-code prompt detected: %s", prompt)
                    question = prompt.splitlines()[0].strip()
                    # Realtime session doesn't support say(); use generate_reply
                    # with explicit instructions so the LLM voices the prompt.
                    session.generate_reply(
                        instructions=(
                            "Claude Code is waiting for a yes/no decision from "
                            f"the user. The question on screen is: '{question}'. "
                            "Briefly tell the user what Claude is asking and ask "
                            "whether to answer yes, yes always, or no. Do not "
                            "call any tools yet — wait for the user's reply, "
                            "then use send_text('1'|'2'|'3', press_enter=True) "
                            "to answer."
                        )
                    )
                elif not prompt:
                    last_seen = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("prompt watcher error")

    async def _watch_claude_completion() -> None:
        """Detect Claude Code busy→idle transitions and nudge the agent to
        summarize. Only fires after Claude was busy for ≥1.5s so brief internal
        idles between tool calls don't spam. 3s cooldown between fires.
        """
        busy_since: float | None = None
        last_fired: float = 0.0
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(1.0)
            try:
                screen = tmux.capture_text()
                now = loop.time()
                if TmuxHelper.is_claude_busy(screen):
                    if busy_since is None:
                        busy_since = now
                elif busy_since is not None:
                    duration = now - busy_since
                    busy_since = None
                    if duration >= 1.5 and now - last_fired >= 3.0:
                        last_fired = now
                        logger.info(
                            "claude-code finished after %.1fs busy", duration
                        )
                        session.generate_reply(
                            instructions=(
                                "Claude Code just finished responding in the "
                                "terminal the user can see. Call read_screen, "
                                "then briefly tell the user (1-2 sentences) what "
                                "Claude did or concluded. If Claude is asking a "
                                "question, relay the question and ask the user "
                                "how they'd like to answer."
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("completion watcher error")

    # End the job when the last remote participant leaves. Without this, the
    # entrypoint would block on `asyncio.Event().wait()` forever and keep
    # pushing video frames into an empty room.
    exit_event = asyncio.Event()

    def _on_participant_disconnected(_: rtc.RemoteParticipant) -> None:
        if len(ctx.room.remote_participants) == 0:
            logger.info("last participant left — ending job")
            exit_event.set()

    ctx.room.on("participant_disconnected", _on_participant_disconnected)

    try:
        # record={"audio": False}: skip audio encoding (Opus is marked experimental in
        # the bundled ffmpeg and breaks recorder_io); keep traces/logs/transcripts.
        await session.start(agent=agent, room=ctx.room, record={"audio": False})
        prompt_task = asyncio.create_task(_watch_claude_prompts())
        completion_task = asyncio.create_task(_watch_claude_completion())
        # session.start returns after setup; the session runs on its own tasks.
        # We must keep the entrypoint coroutine alive (matches
        # livekit_info/examples/browsing_agent/main.py pattern) or the framework
        # cancels any tasks we created — including the video publisher.
        try:
            await exit_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            prompt_task.cancel()
            completion_task.cancel()
    finally:
        try:
            ctx.room.unregister_byte_stream_handler(PROXY_REQUEST_TOPIC)
        except Exception:
            pass
        for t in list(proxy_tasks):
            t.cancel()
        await http_session.close()
        video_task.cancel()


if __name__ == "__main__":
    cli.run_app(server)
