"""LiveKit RPC control plane for UI-driven tmux session management.

Exposes two small request/response RPC methods that the iOS client calls
via `LocalParticipant.performRpc`:

    sessions.list     → {"sessions": [{"name": str, "current": bool}, ...],
                         "current": str}
    sessions.switch   → request  {"name": str}
                        response {"ok": bool, "current": str?, "error": str?}

LiveKit RPC is request/response with small payloads (the byte-stream proxy
is overkill for control traffic). `switch` attaches to an existing session
or creates one if it doesn't exist — same semantics as the voice tool.
"""

from __future__ import annotations

import json
import logging

from livekit import rtc

from tmux_helper import TmuxHelper

logger = logging.getLogger("tmux-agent")

METHOD_LIST = "sessions.list"
METHOD_SWITCH = "sessions.switch"


class RtcControl:
    """Owns the lifecycle of the session-control RPC handlers.

    Usage:
        control = RtcControl(tmux)
        await control.attach(ctx.room)
        try:
            ...
        finally:
            await control.aclose()
    """

    def __init__(self, tmux: TmuxHelper) -> None:
        self._tmux = tmux
        self._room: rtc.Room | None = None

    async def attach(self, room: rtc.Room) -> None:
        """Register RPC handlers on the room's local participant."""
        self._room = room
        lp = room.local_participant
        # Re-registration is idempotent across dev-mode reloads: unregister
        # first (ignore the error if no handler was attached).
        for method in (METHOD_LIST, METHOD_SWITCH):
            try:
                lp.unregister_rpc_method(method)
            except Exception:
                pass
        lp.register_rpc_method(METHOD_LIST, self._handle_list)
        lp.register_rpc_method(METHOD_SWITCH, self._handle_switch)
        logger.info("rtc control ready: rpc=%s,%s", METHOD_LIST, METHOD_SWITCH)

    async def aclose(self) -> None:
        if self._room is None:
            return
        lp = self._room.local_participant
        for method in (METHOD_LIST, METHOD_SWITCH):
            try:
                lp.unregister_rpc_method(method)
            except Exception:
                pass

    # --- handlers --------------------------------------------------------

    async def _handle_list(self, data: rtc.RpcInvocationData) -> str:
        current = self._tmux.session_name
        sessions = self._tmux.list_sessions()
        # Ensure the current session is always in the list, even if tmux
        # hasn't flushed it yet.
        if current and current not in sessions:
            sessions.append(current)
        return json.dumps({
            "sessions": [
                {"name": s, "current": s == current} for s in sessions
            ],
            "current": current,
        })

    async def _handle_switch(self, data: rtc.RpcInvocationData) -> str:
        try:
            req = json.loads(data.payload or "{}")
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid JSON payload"})
        name = (req.get("name") or "").strip()
        if not name:
            return json.dumps({"ok": False, "error": "missing session name"})
        try:
            self._tmux.switch_session(name)
            logger.info("rpc sessions.switch: now on %s", name)
            return json.dumps({"ok": True, "current": name})
        except Exception as e:
            logger.exception("rpc sessions.switch failed")
            return json.dumps({"ok": False, "error": str(e)})
