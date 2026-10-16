"""LiveKit byte-stream ↔ HTTP proxy used by the tmux_agent iOS client.

The iOS app's `WKURLSchemeHandler` serializes each WKWebView request as a
byte stream on topic `http.request` (with method/path/headers in stream
attributes and the body as stream bytes). `RtcProxy` receives the stream,
re-issues the request via `aiohttp` against `PROXY_TARGET` (default
`http://localhost:3000`) on the agent's own machine, and streams the
response back on topic `http.response`.

We would prefer a transparent per-app VPN so the webview could load plain
`http://` URLs, but `WKWebsiteDataStore.proxyConfigurations` is silently
ignored for non-browser apps on iOS (requires Apple's browser-engine
entitlement). See `ios/README.md` for the full story.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from livekit import rtc

logger = logging.getLogger("tmux-agent")

PROXY_TARGET = os.getenv("TMUX_PROXY_TARGET", "http://localhost:3000").rstrip("/")
PROXY_REQUEST_TOPIC = os.getenv("TMUX_PROXY_REQ_TOPIC", "http.request")
PROXY_RESPONSE_TOPIC = os.getenv("TMUX_PROXY_RES_TOPIC", "http.response")
PROXY_TIMEOUT = float(os.getenv("TMUX_PROXY_TIMEOUT", "30"))

_HOP_BY_HOP = frozenset(
    {"host", "connection", "content-length", "transfer-encoding"}
)
# aiohttp transparently decompresses response bodies, so the upstream
# Content-Encoding no longer describes what we send back. Forwarding it
# makes WKWebView try to gunzip plain bytes and render garbage.
_DROP_RESPONSE = _HOP_BY_HOP | {"content-encoding"}

TUNNEL_ORIGIN = "tunnel://localhost"


def _merge_headers(items, drop: frozenset[str]) -> dict[str, str]:
    """Flatten a multi-valued header list into a dict, joining repeats with
    ', ' (Foundation does the same, and parses comma-joined Set-Cookie)."""
    out: dict[str, str] = {}
    for k, v in items:
        if k.lower() in drop:
            continue
        out[k] = f"{out[k]}, {v}" if k in out else v
    return out


def _error_html(status: int, title: str, message: str, target: str) -> bytes:
    """Mobile-friendly HTML error page so WKWebView shows a readable message
    instead of blank content when the upstream dev server is down or slow."""
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
        f"<div class='foot'>tmux-agent proxy → {target}</div>"
        f"</div></body></html>"
    )
    return body.encode("utf-8")


def _rewrite_location(value: str, target: str) -> str:
    """Map an absolute redirect to the dev server back into the tunnel.

    With `allow_redirects=False` the webview follows 3xx itself; a Location
    of `http://localhost:3000/login` would escape the tunnel and hit the
    phone's own localhost. Relative locations are left alone.
    """
    try:
        loc = urlsplit(value)
        tgt = urlsplit(target)
    except ValueError:
        return value
    if not loc.netloc:
        return value
    local_hosts = {"localhost", "127.0.0.1", "::1", (tgt.hostname or "").lower()}
    default_port = {"http": 80, "https": 443}
    loc_port = loc.port or default_port.get(loc.scheme or tgt.scheme)
    tgt_port = tgt.port or default_port.get(tgt.scheme)
    if (loc.hostname or "").lower() in local_hosts and loc_port == tgt_port:
        return urlunsplit(("tunnel", "localhost", loc.path or "/", loc.query, loc.fragment))
    return value


class RtcProxy:
    """Attach to a LiveKit `Room`, proxy iOS webview requests to a local
    HTTP target, stream responses back.

    Usage:

        proxy = RtcProxy()
        await proxy.attach(ctx.room)
        try:
            ...  # agent main loop
        finally:
            await proxy.aclose()
    """

    def __init__(
        self,
        target: str = PROXY_TARGET,
        request_topic: str = PROXY_REQUEST_TOPIC,
        response_topic: str = PROXY_RESPONSE_TOPIC,
        timeout: float = PROXY_TIMEOUT,
        is_allowed: Callable[[str], bool] = lambda _identity: True,
    ) -> None:
        self.target = target.rstrip("/")
        self.request_topic = request_topic
        self.response_topic = response_topic
        self.timeout = timeout
        self._is_allowed = is_allowed
        self._http: aiohttp.ClientSession | None = None
        self._room: rtc.Room | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def attach(self, room: rtc.Room) -> None:
        """Register our byte-stream handler on the room. Safe to call across
        dev-mode reloads — any stale handler for the same topic is dropped
        first, since the room object can outlive the previous entrypoint."""
        self._room = room
        self._http = aiohttp.ClientSession()
        try:
            room.unregister_byte_stream_handler(self.request_topic)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        room.register_byte_stream_handler(self.request_topic, self._on_request)
        logger.info(
            "http proxy ready: topic=%s target=%s",
            self.request_topic, self.target,
        )

    async def aclose(self) -> None:
        """Unregister the handler, cancel any in-flight proxy tasks, and
        close the aiohttp session."""
        if self._room is not None:
            try:
                self._room.unregister_byte_stream_handler(self.request_topic)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        for t in list(self._tasks):
            t.cancel()
        if self._http is not None:
            await self._http.close()
            self._http = None

    # --- internal -------------------------------------------------------

    def _on_request(
        self, reader: rtc.ByteStreamReader, remote_identity: str
    ) -> None:
        task = asyncio.create_task(self._handle(reader, remote_identity))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(
        self, reader: rtc.ByteStreamReader, remote_identity: str
    ) -> None:
        """Handle a single proxied HTTP request.

        Wire format:
          Request stream attrs:  id, method, path, headers (JSON dict str)
          Request stream body:   request body bytes (possibly empty)
          Response stream attrs: id, status, status_text, headers (JSON)
          Response stream body:  response body bytes
        """
        if self._room is None or self._http is None:
            return

        attrs = dict(reader.info.attributes or {})
        req_id = attrs.get("id", reader.info.stream_id)
        method = attrs.get("method", "GET").upper()
        path = attrs.get("path", "/")
        try:
            headers = json.loads(attrs.get("headers", "{}") or "{}")
        except json.JSONDecodeError:
            headers = {}

        if not self._is_allowed(remote_identity):
            logger.warning(
                "proxy: rejecting request from unauthorized identity %r",
                remote_identity,
            )
            await self._respond(
                remote_identity, req_id, 403, "Forbidden",
                {"Content-Type": "text/html; charset=utf-8"},
                _error_html(403, "Forbidden",
                            f"Identity '{remote_identity}' is not allowed to "
                            "use this tunnel.", self.target),
            )
            return

        body_chunks: list[bytes] = []
        async for chunk in reader:
            body_chunks.append(chunk)
        body = b"".join(body_chunks) if body_chunks else None

        url = f"{self.target}{path if path.startswith('/') else '/' + path}"
        logger.info(
            "proxy %s %s (id=%s, %d bytes in)",
            method, url, req_id, len(body or b""),
        )

        fwd_headers = {
            k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP
        }

        status = 502
        status_text = "Bad Gateway"
        res_headers: dict[str, str] = {}
        res_body = b""
        try:
            async with self._http.request(
                method, url,
                headers=fwd_headers, data=body,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                status = resp.status
                status_text = resp.reason or ""
                res_headers = _merge_headers(resp.headers.items(), _DROP_RESPONSE)
                for k in list(res_headers):
                    if k.lower() == "location":
                        res_headers[k] = _rewrite_location(res_headers[k], self.target)
                res_body = await resp.read()
        except asyncio.TimeoutError:
            status, status_text = 504, "Gateway Timeout"
            res_body = _error_html(
                status, "Gateway Timeout",
                f"The agent's upstream at {self.target} didn't respond "
                f"within {self.timeout:.0f}s.\n\nCheck that your dev "
                f"server (e.g. npm run dev) is still running.",
                self.target,
            )
            res_headers = {"Content-Type": "text/html; charset=utf-8"}
        except aiohttp.ClientError as e:
            status, status_text = 502, "Bad Gateway"
            res_body = _error_html(
                status, "Bad Gateway",
                f"The agent could not reach {self.target}.\n\n{e}\n\n"
                f"Start something on that port (e.g. npm run dev, python "
                f"-m http.server 3000) and refresh.",
                self.target,
            )
            res_headers = {"Content-Type": "text/html; charset=utf-8"}
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.exception("proxy request failed")
            status, status_text = 500, "Internal Server Error"
            res_body = _error_html(
                status, "Proxy Error",
                f"Unexpected error in the agent proxy: {e}",
                self.target,
            )
            res_headers = {"Content-Type": "text/html; charset=utf-8"}

        logger.info(
            "proxy %s %s -> %d (id=%s, %d bytes out)",
            method, url, status, req_id, len(res_body),
        )

        await self._respond(
            remote_identity, req_id, status, status_text, res_headers, res_body
        )

    async def _respond(
        self,
        remote_identity: str,
        req_id: str,
        status: int,
        status_text: str,
        res_headers: dict[str, str],
        res_body: bytes,
    ) -> None:
        assert self._room is not None
        content_type = next(
            (v for k, v in res_headers.items() if k.lower() == "content-type"),
            "application/octet-stream",
        )
        writer = await self._room.local_participant.stream_bytes(
            name=f"response-{req_id}",
            topic=self.response_topic,
            destination_identities=[remote_identity],
            attributes={
                "id": req_id,
                "status": str(status),
                "status_text": status_text,
                "headers": json.dumps(res_headers),
            },
            total_size=len(res_body),
            mime_type=content_type.split(";")[0].strip(),
        )
        try:
            if res_body:
                await writer.write(res_body)
        finally:
            await writer.aclose()
