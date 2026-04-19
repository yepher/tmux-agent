"""LiveKit agent that shares a tmux session as a screen-share video track and
accepts voice commands (via OpenAI Realtime) to interact with it.

Uses `pyte` as a virtual terminal to render colors and attributes captured from
tmux via `capture-pane -e`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.agents.llm import function_tool
from livekit.plugins import openai

from rtc_control import RtcControl
from rtc_proxy import RtcProxy
from tmux_helper import TmuxHelper
from video_publisher import VideoPublisher, load_static_png

load_dotenv()

logger = logging.getLogger("tmux-agent")
logger.setLevel(logging.INFO)

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_STATIC_PNG = _SCRIPT_DIR / "res" / "static_share.png"
TMUX_STATIC_IMAGE = os.getenv("TMUX_STATIC_IMAGE", str(_DEFAULT_STATIC_PNG))
# Diagnostic: stream a static PNG instead of the live tmux pane.
STREAM_STATIC_PNG = os.getenv("TMUX_STREAM_STATIC_PNG", "0").strip().lower() in (
    "1", "true", "yes",
)

TMUX_SESSION = os.getenv("TMUX_SESSION_NAME", "agent")
COLS = int(os.getenv("TMUX_COLS", "100"))
ROWS = int(os.getenv("TMUX_ROWS", "30"))
FONT_SIZE = int(os.getenv("TMUX_FONT_SIZE", "20"))


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
    # get "ignoring byte stream ... no callback attached".
    rtc_proxy = RtcProxy()
    await rtc_proxy.attach(ctx.room)

    tmux = TmuxHelper(TMUX_SESSION, COLS, ROWS, FONT_SIZE)
    tmux.ensure()

    control = RtcControl(tmux)
    await control.attach(ctx.room)

    video = VideoPublisher(tmux.render_frame)

    # Diagnostic: override live tmux frames with a static PNG.
    if STREAM_STATIC_PNG:
        try:
            static_img = load_static_png(
                TMUX_STATIC_IMAGE, video.width, video.height
            )
            video.frame_source = lambda: static_img
            logger.info(
                "streaming static PNG %dx%d from %s",
                video.width, video.height, TMUX_STATIC_IMAGE,
            )
        except OSError as e:
            logger.error(
                "static PNG mode failed (%s); falling back to live tmux video",
                e,
            )

    await video.publish(ctx.room)
    video.start()

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

    session = AgentSession(
        llm=openai.realtime.RealtimeModel()
    )

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
        await control.aclose()
        await rtc_proxy.aclose()
        await video.aclose()


if __name__ == "__main__":
    cli.run_app(server)
