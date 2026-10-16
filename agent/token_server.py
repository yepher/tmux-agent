"""Tiny token server for the iOS client, so the LiveKit API secret never
has to live on the phone.

    python token_server.py            # listens on TOKEN_SERVER_HOST:PORT

POST /token
    Authorization: Bearer <TOKEN_SERVER_KEY>
    {"identity": "chris-phone", "room": "tmux-agent"}
→ 200 {"url": "<LIVEKIT_URL>", "token": "<jwt>", "room": "<room>-<epoch>"}

The token:
  * is scoped to one freshly-suffixed room,
  * dispatches the tmux agent explicitly (roomConfig.agents) so the worker
    never needs automatic dispatch,
  * is only minted for identities in TMUX_ALLOWED_IDENTITIES (when set).

Expose it to your phone however you reach your machine (Tailscale, LAN,
an ngrok/cloudflared tunnel). It is a dev tool: one shared bearer key,
no rate limiting.
"""

from __future__ import annotations

import datetime
import hmac
import logging
import os
import time

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

load_dotenv()

logger = logging.getLogger("tmux-token-server")
logging.basicConfig(level=logging.INFO)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
SERVER_KEY = os.getenv("TOKEN_SERVER_KEY", "")
HOST = os.getenv("TOKEN_SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("TOKEN_SERVER_PORT", "8787"))
AGENT_NAME = os.getenv("TMUX_AGENT_NAME", "tmux-agent").strip()
TTL = datetime.timedelta(seconds=int(os.getenv("TOKEN_TTL_SECONDS", "3600")))
ALLOWED = frozenset(
    i.strip()
    for i in os.getenv("TMUX_ALLOWED_IDENTITIES", "").split(",")
    if i.strip()
)


def _authorized(request: web.Request) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip(), SERVER_KEY)


async def handle_token(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    identity = str(body.get("identity", "")).strip()
    room_base = str(body.get("room", "tmux-agent")).strip() or "tmux-agent"
    if not identity:
        return web.json_response({"error": "identity required"}, status=400)
    if ALLOWED and identity not in ALLOWED:
        logger.warning("refusing token for identity %r", identity)
        return web.json_response({"error": "identity not allowed"}, status=403)

    room = f"{room_base}-{int(time.time())}"
    token = (
        api.AccessToken()
        .with_identity(identity)
        .with_name(identity)
        .with_ttl(TTL)
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room,
            can_publish=True,
            can_publish_data=True,
            can_subscribe=True,
        ))
    )
    if AGENT_NAME:
        token = token.with_room_config(api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)],
        ))
    logger.info("minted token identity=%s room=%s", identity, room)
    return web.json_response(
        {"url": LIVEKIT_URL, "token": token.to_jwt(), "room": room}
    )


def main() -> None:
    if not SERVER_KEY:
        raise SystemExit("Set TOKEN_SERVER_KEY (shared secret the app sends).")
    if not LIVEKIT_URL:
        raise SystemExit("Set LIVEKIT_URL.")
    app = web.Application()
    app.router.add_post("/token", handle_token)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
