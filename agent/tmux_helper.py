"""Helpers for interacting with a tmux session and rendering the active pane
to an RGBA image.

`TmuxHelper` is the façade the rest of the agent imports. It owns:
  * a `_TmuxSession` — thin wrapper around `tmux(1)` subprocess calls.
  * a `_TerminalRenderer` — `pyte` virtual terminal + PIL glyph drawer.
  * static helpers that inspect the pane contents for Claude Code state.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import pyte
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("tmux-agent")

DEFAULT_BG: tuple[int, int, int, int] = (20, 20, 24, 255)
DEFAULT_FG: tuple[int, int, int, int] = (220, 220, 220, 255)

_FONT_CANDIDATES = [
    os.getenv("TMUX_FONT_PATH", ""),
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Courier.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]

# Xterm-like palette used to resolve pyte's named colors.
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
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


def find_font() -> str:
    """Locate a usable monospace font. Honors `TMUX_FONT_PATH`."""
    for path in _FONT_CANDIDATES:
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
    key = f"bright_{name}" if bright and f"bright_{name}" in _NAMED_COLORS else name
    if key in _NAMED_COLORS:
        r, g, b = _NAMED_COLORS[key]
        return (r, g, b, 255)
    if len(name) == 6:
        try:
            return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16), 255)
        except ValueError:
            pass
    return default


def _attrs_equal(a, b) -> bool:
    return (
        a.fg == b.fg
        and a.bg == b.bg
        and a.bold == b.bold
        and a.reverse == b.reverse
        and a.underscore == b.underscore
    )


def _align(n: int, multiple: int) -> int:
    return ((n + multiple - 1) // multiple) * multiple


@dataclass
class CellSize:
    w: int
    h: int


class _TmuxSession:
    """Thin wrapper around `tmux` CLI for one named session."""

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
                "new-session", "-d", "-s", self.name,
                "-x", str(self.cols), "-y", str(self.rows),
            )
        else:
            logger.info("attaching to existing tmux session %s", self.name)
            self._run(
                "resize-window", "-t", self.name,
                "-x", str(self.cols), "-y", str(self.rows),
                check=False,
            )

    def capture_ansi(self) -> str:
        """Capture the active pane with SGR escapes preserved."""
        return self._run("capture-pane", "-p", "-e", "-t", self.name).stdout

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
            "list-windows", "-t", self.name,
            "-F", "#{window_index}\t#{window_name}\t#{window_active}",
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


class _TerminalRenderer:
    """pyte virtual terminal + PIL glyph drawer. Produces RGBA images."""

    def __init__(self, cols: int, rows: int, font_size: int) -> None:
        self.cols = cols
        self.rows = rows
        self.font = ImageFont.truetype(find_font(), font_size)
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
            self.stream.feed("\x1b[0m")

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


class TmuxHelper:
    """Façade over a tmux session + terminal renderer.

    All tmux-specific logic (subprocess calls, ANSI rendering, Claude Code
    state detection) lives here so the rest of the agent only deals with
    PIL images, strings, and a small action API.
    """

    def __init__(self, session_name: str, cols: int, rows: int, font_size: int) -> None:
        self._session = _TmuxSession(session_name, cols, rows)
        self._renderer = _TerminalRenderer(cols, rows, font_size)

    # --- lifecycle --------------------------------------------------------

    def ensure(self) -> None:
        """Create the tmux session if missing; resize otherwise."""
        self._session.ensure()

    # --- identity / geometry ---------------------------------------------

    @property
    def session_name(self) -> str:
        return self._session.name

    @property
    def cols(self) -> int:
        return self._session.cols

    @property
    def rows(self) -> int:
        return self._session.rows

    @property
    def render_width(self) -> int:
        return self._renderer.width

    @property
    def render_height(self) -> int:
        return self._renderer.height

    @property
    def cell_size(self) -> CellSize:
        return self._renderer.cell

    # --- capture ----------------------------------------------------------

    def render_frame(self) -> Image.Image:
        """Capture the active pane as ANSI and render it to an RGBA image."""
        return self._renderer.render(self._session.capture_ansi())

    def capture_lines(self) -> list[str]:
        """Plain-text lines of the visible pane, padded/truncated to the grid."""
        return self._session.capture_plain()

    def capture_text(self) -> str:
        """`capture_lines()` joined with newlines."""
        return "\n".join(self._session.capture_plain())

    # --- input ------------------------------------------------------------

    def send_text(self, text: str, press_enter: bool = False) -> None:
        """Type literal text; optionally press Enter after."""
        self._session.send_literal(text, enter=press_enter)

    def send_key(self, key: str) -> None:
        """Send a tmux-style key name (e.g. 'Enter', 'C-c', 'S-Tab')."""
        self._session.send_keys(key)

    # --- sessions / windows ----------------------------------------------

    def list_sessions(self) -> list[str]:
        return self._session.list_sessions()

    def list_windows(self) -> list[tuple[int, str, bool]]:
        return self._session.list_windows()

    def switch_session(self, name: str) -> None:
        self._session.switch_session(name)

    def select_window(self, target: str) -> None:
        self._session.select_window(target)

    # --- Claude Code state detection -------------------------------------

    @staticmethod
    def is_claude_busy(screen: str) -> bool:
        """True if the pane text looks like Claude Code is processing
        (shows "esc to interrupt" / Thinking / Running cues).
        """
        low = screen.lower()
        return (
            "esc to interrupt" in low
            or "thinking…" in low
            or "thinking..." in low
            or "running…" in low
            or "running..." in low
        )

    @staticmethod
    def detect_claude_prompt(lines: list[str]) -> str | None:
        """Return the question + options when the pane shows a Claude Code
        confirmation prompt, else None.

        Shape:
            Do you want to proceed?
            ❯ 1. Yes
              2. Yes, and don't ask again …
              3. No, tell Claude what to do differently
        """
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if not (low.startswith("do you want") or low.startswith("would you like")):
                continue
            has_yes = False
            has_no = False
            context = [line]
            for follow in lines[i + 1 : i + 10]:
                s = follow.strip().lstrip("❯›>•").strip()
                if not s:
                    continue
                context.append(s)
                if s.startswith("1.") and "yes" in s.lower():
                    has_yes = True
                if s.startswith("3.") and "no" in s.lower():
                    has_no = True
            if has_yes and has_no:
                return "\n".join(context[:4])
        return None
