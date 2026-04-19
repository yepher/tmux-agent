"""LiveKit video publisher for the tmux screen-share track.

`VideoPublisher` owns the LiveKit `VideoSource` + `LocalVideoTrack`, a
persistent frame buffer, and the per-frame loop. It is generic over a
`frame_source` callable (`() -> PIL.Image`) so the caller can swap in a
static PNG or the live tmux pane.

### The persistent-buffer pattern

The publisher keeps one `bytearray` alive for the life of the source and
mutates it in place every iteration (via a numpy view). Each frame wraps
that same bytearray in a fresh `rtc.VideoFrame`.

This is intentional. The LiveKit FFI stores the buffer *pointer* when
`capture_frame()` returns and reads the pixel bytes asynchronously from the
Rust encoder. If we hand it a fresh short-lived `bytes` object per frame,
Python GC can invalidate the pointer before the encoder reads it — which
shows up as green / rainbow stripes in the browser. Reusing one stable
buffer keeps the pointer valid forever. Pattern lifted from
`python-sdks/examples/publish_hue.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

import numpy as np
from livekit import rtc
from PIL import Image, ImageOps

from tmux_helper import DEFAULT_BG

logger = logging.getLogger("tmux-agent")

FPS = int(os.getenv("TMUX_FPS", "10"))
MAX_BITRATE = int(os.getenv("TMUX_MAX_BITRATE", "8000000"))

# Compat publish path (default — the verified-working one).
COMPAT_VIDEO = os.getenv("TMUX_COMPAT_VIDEO", "1").lower() in ("1", "true", "yes")
COMPAT_WIDTH = int(os.getenv("TMUX_COMPAT_WIDTH", "1280"))
COMPAT_HEIGHT = int(os.getenv("TMUX_COMPAT_HEIGHT", "720"))
_COMPAT_TRACK_SRC = os.getenv("TMUX_COMPAT_TRACK_SOURCE", "screenshare").strip().lower()

# Advanced / non-compat publish path.
OUT_WIDTH = int(os.getenv("TMUX_OUT_WIDTH", "1280"))
OUT_HEIGHT = int(os.getenv("TMUX_OUT_HEIGHT", "720"))
SCREENCAST_SOURCE = os.getenv("TMUX_SCREENCAST", "0").lower() in ("1", "true", "yes")
_VIDEO_CODEC_STR = os.getenv("TMUX_VIDEO_CODEC", "h264").strip().lower()
_TRACK_SRC_STR = os.getenv("TMUX_TRACK_SOURCE", "screenshare").strip().lower()


def _pad(img: Image.Image, w: int, h: int) -> Image.Image:
    """Letterbox a PIL image to (w, h)."""
    if img.size == (w, h):
        return img
    return ImageOps.pad(
        img, (w, h),
        method=Image.Resampling.LANCZOS,
        color=DEFAULT_BG,
    )


def _track_source_enum(s: str) -> int:
    if s in ("camera", "cam"):
        return rtc.TrackSource.SOURCE_CAMERA
    return rtc.TrackSource.SOURCE_SCREENSHARE


def _video_codec(s: str) -> int:
    return {
        "vp8": rtc.VideoCodec.VP8,
        "h264": rtc.VideoCodec.H264,
        "h265": rtc.VideoCodec.H265,
        "hevc": rtc.VideoCodec.H265,
        "vp9": rtc.VideoCodec.VP9,
        "av1": rtc.VideoCodec.AV1,
    }.get(s, rtc.VideoCodec.H264)


def _publish_options_compat() -> rtc.TrackPublishOptions:
    return rtc.TrackPublishOptions(source=_track_source_enum(_COMPAT_TRACK_SRC))


def _publish_options_advanced(fps: int) -> rtc.TrackPublishOptions:
    opts = rtc.TrackPublishOptions()
    opts.source = _track_source_enum(_TRACK_SRC_STR)
    opts.video_codec = _video_codec(_VIDEO_CODEC_STR)
    ve = rtc.VideoEncoding()
    ve.max_bitrate = MAX_BITRATE
    ve.max_framerate = max(fps, 1)
    opts.video_encoding.CopyFrom(ve)
    return opts


def load_static_png(path: str | Path, w: int, h: int) -> Image.Image:
    """Load a PNG and letterbox it to (w, h). Used by `TMUX_STREAM_STATIC_PNG`."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(
            f"Static image not found: {p}. "
            "Set TMUX_STATIC_IMAGE or add res/static_share.png"
        )
    return _pad(Image.open(p).convert("RGBA"), w, h)


class VideoPublisher:
    """Publish a LiveKit screen-share track fed by a frame-producing callable.

    ```python
    vp = VideoPublisher(frame_source=tmux.render_frame)
    await vp.publish(room)
    vp.start()
    try:
        ...  # agent main loop
    finally:
        await vp.aclose()
    ```

    Dimensions default to the `TMUX_COMPAT_WIDTH` / `TMUX_COMPAT_HEIGHT` env
    vars (1280×720). `frame_source` is a mutable attribute so callers can
    swap in a different source (e.g. a static PNG) before `publish()`.
    """

    def __init__(
        self,
        frame_source: Callable[[], Image.Image],
        *,
        width: int | None = None,
        height: int | None = None,
        fps: int = FPS,
        compat: bool = COMPAT_VIDEO,
    ) -> None:
        self.frame_source = frame_source
        self._fps = max(fps, 1)
        self._compat = compat
        self.width = width if width is not None else (
            COMPAT_WIDTH if compat else OUT_WIDTH
        )
        self.height = height if height is not None else (
            COMPAT_HEIGHT if compat else OUT_HEIGHT
        )
        self._buffer = bytearray(self.width * self.height * 4)
        self._view = np.frombuffer(self._buffer, dtype=np.uint8).reshape(
            self.height, self.width, 4
        )
        self._source: rtc.VideoSource | None = None
        self._task: asyncio.Task[None] | None = None

    async def publish(self, room: rtc.Room) -> str:
        """Create and publish the track. Returns the track SID."""
        self._source = rtc.VideoSource(
            self.width, self.height,
            is_screencast=False if self._compat else SCREENCAST_SOURCE,
        )
        track = rtc.LocalVideoTrack.create_video_track(
            "tmux-screen", self._source
        )
        opts = (
            _publish_options_compat()
            if self._compat
            else _publish_options_advanced(self._fps)
        )
        pub = await room.local_participant.publish_track(track, opts)
        logger.info(
            "video track published sid=%s %dx%d fps=%d compat=%s",
            pub.sid, self.width, self.height, self._fps, self._compat,
        )
        return pub.sid

    def start(self) -> None:
        """Kick off the frame loop. Call after `publish()`."""
        if self._task is not None:
            return
        if self._source is None:
            raise RuntimeError("call publish() before start()")
        self._task = asyncio.create_task(self._stream())

    async def aclose(self) -> None:
        """Cancel the frame loop."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _stream(self) -> None:
        assert self._source is not None
        logger.info(
            "stream task start %dx%d fps=%d buf_len=%d",
            self.width, self.height, self._fps, len(self._buffer),
        )
        dt = 1.0 / self._fps
        next_t = perf_counter()
        frames = 0
        try:
            while True:
                img = self.frame_source()
                if img.size != (self.width, self.height):
                    img = _pad(img, self.width, self.height)
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                np.copyto(self._view, np.asarray(img, dtype=np.uint8))
                self._source.capture_frame(rtc.VideoFrame(
                    self.width, self.height,
                    rtc.VideoBufferType.RGBA, self._buffer,
                ))
                frames += 1
                if frames <= 5 or frames % 100 == 0:
                    logger.info("pushed frame %d", frames)
                next_t += dt
                await asyncio.sleep(next_t - perf_counter())
        except asyncio.CancelledError:
            logger.info("stream cancelled at frame %d", frames)
            raise
        except Exception:
            logger.exception("stream died at frame %d", frames)
            raise
