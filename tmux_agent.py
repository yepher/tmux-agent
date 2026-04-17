"""LiveKit agent that shares a tmux session as a screen-share video track and
accepts voice commands (via OpenAI Realtime) to interact with it.

Uses `pyte` as a virtual terminal to render colors and attributes captured from
tmux via `capture-pane -e`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyte
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.agents.llm import function_tool
from livekit.plugins import openai
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
COMPAT_WIDTH = int(os.getenv("TMUX_COMPAT_WIDTH", "640"))
COMPAT_HEIGHT = int(os.getenv("TMUX_COMPAT_HEIGHT", "480"))
# Match browsing_agent/browser_manager exactly: screenshare source, no I420 conversion.
_COMPAT_TRACK_SRC = os.getenv("TMUX_COMPAT_TRACK_SOURCE", "screenshare").strip().lower()
# RGBA → I420 before capture_frame. Default OFF — the browsing_agent reference
# passes RGBA straight through, and the I420 conversion path was producing
# green/striped output in this setup.
COMPAT_I420 = os.getenv("TMUX_COMPAT_I420", "0").lower() in ("1", "true", "yes")
# Write /tmp/tmux_debug_first.rgba on first frame; verify with ffplay before blaming WebRTC.
DEBUG_RAW_FRAME = os.getenv("TMUX_DEBUG_RAW", "").lower() in ("1", "true", "yes")

DEFAULT_BG = (20, 20, 24, 255)
DEFAULT_FG = (220, 220, 220, 255)

FONT_CANDIDATES = [
    os.getenv("TMUX_FONT_PATH", ""),
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Courier.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]

# Xterm-like palette used to resolve pyte's named colors.
NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "red": (205, 0, 0),
    "green": (0, 205, 0),
    "yellow": (205, 205, 0),
    "brown": (205, 205, 0),
    "blue": (0, 0, 238),
    "magenta": (205, 0, 205),
    "cyan": (0, 205, 205),
    "white": (229, 229, 229),
    "bright_black": (127, 127, 127),
    "bright_red": (255, 0, 0),
    "bright_green": (0, 255, 0),
    "bright_yellow": (255, 255, 0),
    "bright_blue": (92, 92, 255),
    "bright_magenta": (255, 0, 255),
    "bright_cyan": (0, 255, 255),
    "bright_white": (255, 255, 255),
}


def _find_font() -> str:
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            return path
    raise RuntimeError(
        "No monospace font found. Set TMUX_FONT_PATH to a .ttf/.ttc path."
    )


def _resolve_color(
    name: str, default: tuple[int, int, int, int], bright: bool = False
) -> tuple[int, int, int, int]:
    """Resolve a pyte color token (named, 'default', or 6-char hex) to RGBA."""
    if name == "default":
        return default
    key = f"bright_{name}" if bright and f"bright_{name}" in NAMED_COLORS else name
    if key in NAMED_COLORS:
        r, g, b = NAMED_COLORS[key]
        return (r, g, b, 255)
    if len(name) == 6:
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16), 255)
        except ValueError:
            pass
    return default


@dataclass
class CellSize:
    w: int
    h: int


class TmuxSession:
    def __init__(self, name: str, cols: int, rows: int) -> None:
        self.name = name
        self.cols = cols
        self.rows = rows

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=check
        )

    def ensure(self) -> None:
        if not shutil.which("tmux"):
            raise RuntimeError("tmux is not installed or not in PATH")
        exists = self._run("has-session", "-t", self.name, check=False).returncode == 0
        if not exists:
            logger.info("creating tmux session %s (%dx%d)", self.name, self.cols, self.rows)
            self._run(
                "new-session",
                "-d",
                "-s",
                self.name,
                "-x",
                str(self.cols),
                "-y",
                str(self.rows),
            )
        else:
            logger.info("attaching to existing tmux session %s", self.name)
            self._run(
                "resize-window",
                "-t",
                self.name,
                "-x",
                str(self.cols),
                "-y",
                str(self.rows),
                check=False,
            )

    def capture_ansi(self) -> str:
        """Capture the active pane with SGR escapes preserved."""
        result = self._run("capture-pane", "-p", "-e", "-t", self.name)
        return result.stdout

    def capture_plain(self) -> list[str]:
        result = self._run("capture-pane", "-p", "-t", self.name)
        lines = result.stdout.splitlines()
        if len(lines) < self.rows:
            lines.extend([""] * (self.rows - len(lines)))
        return [line[: self.cols].ljust(self.cols) for line in lines[: self.rows]]

    def send_literal(self, text: str, enter: bool = False) -> None:
        self._run("send-keys", "-t", self.name, "-l", text)
        if enter:
            self._run("send-keys", "-t", self.name, "Enter")

    def send_keys(self, *keys: str) -> None:
        self._run("send-keys", "-t", self.name, *keys)

    def list_sessions(self) -> list[str]:
        r = self._run("list-sessions", "-F", "#{session_name}", check=False)
        if r.returncode != 0:
            return []
        return [line for line in r.stdout.splitlines() if line]

    def list_windows(self) -> list[tuple[int, str, bool]]:
        r = self._run(
            "list-windows",
            "-t",
            self.name,
            "-F",
            "#{window_index}\t#{window_name}\t#{window_active}",
            check=False,
        )
        windows: list[tuple[int, str, bool]] = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                windows.append((int(parts[0]), parts[1], parts[2] == "1"))
        return windows

    def select_window(self, target: str) -> None:
        self._run("select-window", "-t", f"{self.name}:{target}")

    def switch_session(self, name: str) -> None:
        self.name = name
        self.ensure()


def _align(n: int, multiple: int) -> int:
    return ((n + multiple - 1) // multiple) * multiple


class TerminalRenderer:
    def __init__(self, cols: int, rows: int, font_size: int) -> None:
        self.cols = cols
        self.rows = rows
        self.font = ImageFont.truetype(_find_font(), font_size)
        char_w = int(round(self.font.getlength("M")))
        ascent, descent = self.font.getmetrics()
        char_h = ascent + descent
        self.cell = CellSize(w=char_w, h=char_h)
        # webrtc encoders (VP8/VP9/H264) like dims aligned to 16 — avoids
        # stride/padding artifacts that can show up as color corruption.
        self.width = _align(cols * char_w, 16)
        self.height = _align(rows * char_h, 16)
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    def _feed_snapshot(self, ansi_text: str) -> None:
        self.screen.reset()
        # Feed each captured row at an absolute cursor position so newlines/wrap
        # in the source don't throw the emulator off.
        for y, line in enumerate(ansi_text.splitlines()):
            if y >= self.rows:
                break
            self.stream.feed(f"\x1b[{y + 1};1H")
            self.stream.feed(line)
            self.stream.feed("\x1b[0m")  # clear SGR between rows

    def render(self, ansi_text: str) -> Image.Image:
        self._feed_snapshot(ansi_text)
        img = Image.new("RGBA", (self.width, self.height), DEFAULT_BG)
        draw = ImageDraw.Draw(img)
        for y in range(self.rows):
            self._draw_row(draw, y, self.screen.buffer[y])
        return img

    def _draw_row(self, draw: ImageDraw.ImageDraw, y: int, row) -> None:
        x = 0
        py = y * self.cell.h
        while x < self.cols:
            cell = row[x]
            end = x + 1
            while end < self.cols and _attrs_equal(row[end], cell):
                end += 1
            fg = _resolve_color(cell.fg, DEFAULT_FG, bright=cell.bold)
            bg = _resolve_color(cell.bg, DEFAULT_BG)
            if cell.reverse:
                fg, bg = bg, fg
            text = "".join((row[i].data or " ") for i in range(x, end))
            px = x * self.cell.w
            width = (end - x) * self.cell.w
            if bg != DEFAULT_BG:
                draw.rectangle([px, py, px + width, py + self.cell.h], fill=bg)
            if text.strip():
                draw.text((px, py), text, font=self.font, fill=fg)
                if cell.underscore:
                    uy = py + self.cell.h - 2
                    draw.line([(px, uy), (px + width, uy)], fill=fg, width=1)
            x = end


def _attrs_equal(a, b) -> bool:
    return (
        a.fg == b.fg
        and a.bg == b.bg
        and a.bold == b.bold
        and a.reverse == b.reverse
        and a.underscore == b.underscore
    )


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

Use your tools to run commands, send text, send keys, and switch tmux sessions or windows.
Prefer `run_command` for normal shell commands. Use `send_text` when typing into an
interactive prompt. Use `send_key` for special keys like Enter, Tab, arrows, Escape, or
combos like C-c, C-d, C-l.

Narrate briefly what you are about to do (one short sentence), then run it. After running,
summarize the result in one or two short sentences. Keep responses concise and
terminal-aware. Do not read long file contents aloud unless asked.
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

    tmux = TmuxSession(TMUX_SESSION, COLS, ROWS)
    tmux.ensure()

    renderer = TerminalRenderer(COLS, ROWS, FONT_SIZE)

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
        renderer.width,
        renderer.height,
        vid_w,
        vid_h,
        COMPAT_VIDEO,
        _COMPAT_TRACK_SRC if COMPAT_VIDEO else "n/a",
        COMPAT_I420 if COMPAT_VIDEO else False,
        renderer.cell.w,
        renderer.cell.h,
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
    big_font = ImageFont.truetype(_find_font(), min(120, oh // 4))
    td.text((ow // 2 - 140, oh // 2 - 70), "TMUX", font=big_font, fill=(255, 255, 255, 255))
    if TEST_MODE:
        test_img.save("/tmp/tmux_agent_testcard.png")
        logger.info("TEST MODE: publishing static test card (%dx%d)", ow, oh)

    frame_delta_us = 1_000_000 // max(FPS, 1)

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
                    pub_img = renderer.render(tmux.capture_ansi())
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
        return "\n".join(tmux.capture_plain()[-20:])

    @function_tool
    async def run_command(command: str) -> str:
        """Run a shell command in the tmux session by typing it and pressing Enter.

        Use for normal shell commands (e.g. 'ls -la', 'cd ~/code', 'git status').

        Args:
            command: The full shell command to run.
        """
        logger.info("run_command: %s", command)
        tmux.send_literal(command, enter=True)
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
        tmux.send_literal(text, enter=press_enter)
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
        tmux.send_keys(key)
        await asyncio.sleep(0.25)
        return _tail()

    @function_tool
    async def read_screen() -> str:
        """Return the current visible contents of the tmux pane (plain text)."""
        return "\n".join(tmux.capture_plain())

    @function_tool
    async def list_sessions() -> str:
        """List all tmux sessions on the host. The current session is marked with '*'."""
        sessions = tmux.list_sessions()
        if not sessions:
            return "No tmux sessions found."
        return "\n".join(f"{'*' if s == tmux.name else ' '} {s}" for s in sessions)

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
            list_sessions,
            switch_session,
            list_windows,
            switch_window,
        ],
    )

    session = AgentSession(llm=openai.realtime.RealtimeModel())

    try:
        # record={"audio": False}: skip audio encoding (Opus is marked experimental in
        # the bundled ffmpeg and breaks recorder_io); keep traces/logs/transcripts.
        await session.start(agent=agent, room=ctx.room, record={"audio": False})
        # session.start returns after setup; the session runs on its own tasks.
        # We must keep the entrypoint coroutine alive (matches
        # livekit_info/examples/browsing_agent/main.py pattern) or the framework
        # cancels any tasks we created — including the video publisher.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
    finally:
        video_task.cancel()


if __name__ == "__main__":
    cli.run_app(server)
