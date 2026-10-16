"""Helpers for interacting with a tmux session and rendering the active pane
to an RGBA image.

`TmuxHelper` is the façade the rest of the agent imports. It owns:
  * a `_TmuxSession` — thin wrapper around `tmux(1)` subprocess calls.
  * a `_TerminalRenderer` — `pyte` virtual terminal + PIL glyph drawer.
  * static helpers that inspect the pane contents for Claude Code state.

All methods are synchronous (they shell out to `tmux`). Call them from a
worker thread (`asyncio.to_thread`) when running inside the event loop.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

import pyte
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("tmux-agent")

DEFAULT_BG: tuple[int, int, int, int] = (20, 20, 24, 255)
DEFAULT_FG: tuple[int, int, int, int] = (220, 220, 220, 255)

# Pin the tmux window to COLS x ROWS so a laptop client attaching to the same
# session can't resize it out from under the renderer.
LOCK_SIZE = os.getenv("TMUX_LOCK_SIZE", "1").strip().lower() in ("1", "true", "yes")

_FONT_CANDIDATES = [
    os.getenv("TMUX_FONT_PATH", ""),
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Courier.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]

# Xterm-like palette keyed by pyte's color names. pyte names the ANSI
# 33/43 color "brown" and the aixterm 90-97/100-107 colors "bright<name>"
# (no underscore).
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "red": (205, 0, 0),
    "green": (0, 205, 0),
    "brown": (205, 205, 0),
    "yellow": (205, 205, 0),
    "blue": (0, 0, 238),
    "magenta": (205, 0, 205),
    "cyan": (0, 205, 205),
    "white": (229, 229, 229),
    "brightblack": (127, 127, 127),
    "brightred": (255, 0, 0),
    "brightgreen": (0, 255, 0),
    "brightbrown": (255, 255, 0),
    "brightyellow": (255, 255, 0),
    "brightblue": (92, 92, 255),
    "brightmagenta": (255, 0, 255),
    "brightcyan": (0, 255, 255),
    "brightwhite": (255, 255, 255),
}

# tmux rewrites ':' and '.' in session names, and they are target separators.
_INVALID_SESSION_CHARS = re.compile(r"[:.\s]")

# "❯ 1. Yes" / "  2. No, tell Claude…" inside Claude Code's prompt box.
_OPTION_RE = re.compile(r"^(?:[❯›>•*]\s*)?(\d+)[.)]\s+(.+)$")
_BOX_CHARS = "│┃|╭╮╰╯─ "


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
    key = name
    if bright and not name.startswith("bright") and f"bright{name}" in _NAMED_COLORS:
        key = f"bright{name}"
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


def validate_session_name(name: str) -> str:
    """Return a cleaned session name or raise ValueError."""
    name = (name or "").strip()
    if not name:
        raise ValueError("session name is empty")
    if _INVALID_SESSION_CHARS.search(name):
        raise ValueError(
            f"invalid session name {name!r}: no spaces, ':' or '.' allowed"
        )
    return name


@dataclass
class CellSize:
    w: int
    h: int


@dataclass
class ClaudePrompt:
    """A Claude Code multiple-choice prompt detected on screen."""

    question: str
    options: list[tuple[int, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.question + "|" + "|".join(f"{n}.{t}" for n, t in self.options)

    def option_for(self, *prefixes: str) -> int | None:
        """Number of the first option whose label starts with any prefix."""
        for num, label in self.options:
            low = label.lower()
            if any(low.startswith(p) for p in prefixes):
                return num
        return None


class _TmuxSession:
    """Thin wrapper around `tmux` CLI for one named session.

    Targets use tmux's `=` prefix so names match exactly (plain `-t foo`
    prefix-matches, so `-t a` would hit a session called `agent`).
    """

    def __init__(self, name: str, cols: int, rows: int) -> None:
        self.name = validate_session_name(name)
        self.cols = cols
        self.rows = rows
        self._last_failure_msg = ""

    # --- targets ---------------------------------------------------------

    def _session_target(self, name: str | None = None) -> str:
        return f"={name or self.name}"

    def _pane_target(self, name: str | None = None) -> str:
        return f"={name or self.name}:"

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=check
        )

    # --- lifecycle -------------------------------------------------------

    def ensure(self, name: str | None = None) -> None:
        """Create session `name` (default: current) if missing, then size it."""
        name = name or self.name
        if not shutil.which("tmux"):
            raise RuntimeError("tmux is not installed or not in PATH")
        exists = (
            self._run("has-session", "-t", self._session_target(name), check=False)
            .returncode == 0
        )
        if not exists:
            logger.info("creating tmux session %s (%dx%d)", name, self.cols, self.rows)
            self._run(
                "new-session", "-d", "-s", name,
                "-x", str(self.cols), "-y", str(self.rows),
            )
        else:
            logger.info("attaching to existing tmux session %s", name)
        if LOCK_SIZE:
            # `window-size` is a session option on older tmux and a window
            # option on newer tmux; try both, ignore whichever fails.
            self._run("set-option", "-t", self._session_target(name),
                      "window-size", "manual", check=False)
            self._run("set-option", "-w", "-t", self._pane_target(name),
                      "window-size", "manual", check=False)
        self._run(
            "resize-window", "-t", self._pane_target(name),
            "-x", str(self.cols), "-y", str(self.rows),
            check=False,
        )

    # --- capture ---------------------------------------------------------

    def capture_ansi(self) -> str:
        """Capture the active pane with SGR escapes preserved.

        Returns "" if the session is gone (e.g. the user typed `exit` in
        its last pane). That lets the video loop keep running with a blank
        frame instead of crashing.
        """
        result = self._run(
            "capture-pane", "-p", "-e", "-t", self._pane_target(), check=False,
        )
        if result.returncode != 0:
            self._log_capture_failure("capture_ansi", result)
            return ""
        return result.stdout

    def capture_plain(self) -> list[str]:
        result = self._run(
            "capture-pane", "-p", "-t", self._pane_target(), check=False,
        )
        if result.returncode != 0:
            self._log_capture_failure("capture_plain", result)
            return [" " * self.cols] * self.rows
        lines = result.stdout.splitlines()
        if len(lines) < self.rows:
            lines.extend([""] * (self.rows - len(lines)))
        return [line[: self.cols].ljust(self.cols) for line in lines[: self.rows]]

    def capture_scrollback(self, lines: int) -> str:
        """Capture the pane plus `lines` rows of scrollback as plain text."""
        result = self._run(
            "capture-pane", "-p", "-S", f"-{lines}", "-t", self._pane_target(),
            check=False,
        )
        if result.returncode != 0:
            self._log_capture_failure("capture_scrollback", result)
            return ""
        return result.stdout

    def _log_capture_failure(
        self, op: str, result: subprocess.CompletedProcess
    ) -> None:
        # Throttle "session gone" logs — they fire per frame otherwise.
        msg = f"{op} failed on '{self.name}': {(result.stderr or '').strip()}"
        if msg != self._last_failure_msg:
            logger.warning("%s", msg)
            self._last_failure_msg = msg

    # --- input -----------------------------------------------------------

    def send_literal(self, text: str, enter: bool = False) -> None:
        # `--` stops option parsing so text like "--help" or "-x" is typed
        # instead of being read as a send-keys flag.
        self._run("send-keys", "-t", self._pane_target(), "-l", "--", text)
        if enter:
            self._run("send-keys", "-t", self._pane_target(), "Enter")

    def send_keys(self, *keys: str) -> None:
        self._run("send-keys", "-t", self._pane_target(), *keys)

    # --- sessions / windows ---------------------------------------------

    def list_sessions(self) -> list[str]:
        r = self._run("list-sessions", "-F", "#{session_name}", check=False)
        if r.returncode != 0:
            return []
        return [line for line in r.stdout.splitlines() if line]

    def list_windows(self) -> list[tuple[int, str, bool]]:
        r = self._run(
            "list-windows", "-t", self._session_target(),
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
        self._run("select-window", "-t", f"={self.name}:{target}")

    def switch_session(self, name: str) -> str:
        """Attach to (or create) `name`. Only commits the switch on success."""
        name = validate_session_name(name)
        self.ensure(name)
        self.name = name
        return name


class _TerminalRenderer:
    """pyte virtual terminal + PIL glyph drawer. Produces RGBA images.

    Caches the last rendered image keyed on the captured ANSI text, so an
    idle terminal costs one `tmux capture-pane` per frame and no drawing.
    """

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
        self._lock = threading.Lock()
        self._last_ansi: str | None = None
        self._last_img: Image.Image | None = None

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
        with self._lock:
            if ansi_text == self._last_ansi and self._last_img is not None:
                return self._last_img
            self._feed_snapshot(ansi_text)
            img = Image.new("RGBA", (self.width, self.height), DEFAULT_BG)
            draw = ImageDraw.Draw(img)
            for y in range(self.rows):
                self._draw_row(draw, y, self.screen.buffer[y])
            self._last_ansi = ansi_text
            self._last_img = img
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
        """Create the tmux session if missing; size it otherwise."""
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
        """Capture the active pane as ANSI and render it to an RGBA image.

        Returns the *same* Image object when the pane hasn't changed, so
        callers can cheaply skip work with an identity check.
        """
        return self._renderer.render(self._session.capture_ansi())

    def capture_lines(self) -> list[str]:
        """Plain-text lines of the visible pane, padded/truncated to the grid."""
        return self._session.capture_plain()

    def capture_text(self) -> str:
        """`capture_lines()` joined with newlines."""
        return "\n".join(self._session.capture_plain())

    def capture_scrollback(self, lines: int) -> str:
        """Plain-text pane plus `lines` rows of scrollback history."""
        return self._session.capture_scrollback(lines)

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

    def switch_session(self, name: str) -> str:
        """Switch to `name` (creating it if needed). Returns the clean name."""
        return self._session.switch_session(name)

    def select_window(self, target: str) -> None:
        self._session.select_window(target)

    # --- Claude Code state detection -------------------------------------

    @staticmethod
    def _tail_lines(screen: str, n: int) -> list[str]:
        return [ln for ln in screen.splitlines() if ln.strip()][-n:]

    @staticmethod
    def is_claude_busy(screen: str) -> bool:
        """True if Claude Code's status area shows it is working.

        Only the bottom of the pane is inspected (where Claude Code draws its
        spinner / "esc to interrupt" hint) so stale text higher up — or a
        shell command that prints "Running..." — doesn't count.
        """
        tail = "\n".join(TmuxHelper._tail_lines(screen, 8)).lower()
        return (
            "esc to interrupt" in tail
            or "thinking…" in tail
            or "thinking..." in tail
        )

    @staticmethod
    def detect_claude_prompt(lines: list[str]) -> ClaudePrompt | None:
        """Return the most recent question + numbered options on screen.

        Shape (inside Claude Code's prompt box):
            Do you want to proceed?
            ❯ 1. Yes
              2. Yes, and don't ask again …
              3. No, tell Claude what to do differently

        Works for any number of options ≥ 2, as long as they are numbered
        consecutively from 1 and start within a few lines of a line ending
        in '?'.
        """
        cleaned = [ln.strip().strip(_BOX_CHARS).strip() for ln in lines]
        found: ClaudePrompt | None = None
        for i, line in enumerate(cleaned):
            if not line.endswith("?"):
                continue
            options: list[tuple[int, str]] = []
            gap = 0
            for follow in cleaned[i + 1 : i + 16]:
                if not follow:
                    continue
                m = _OPTION_RE.match(follow)
                if m and int(m.group(1)) == len(options) + 1:
                    options.append((int(m.group(1)), m.group(2).strip()))
                    continue
                if options:
                    break
                gap += 1
                if gap > 3:
                    break
            if len(options) >= 2:
                found = ClaudePrompt(question=line, options=options)
        return found
