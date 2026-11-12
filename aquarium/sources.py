"""Camera frame sources. Each yields JPEG frames from an async iterator.

A new source (for example the ESP32-CAM's MJPEG stream for the future 3D
version) only has to implement `frames()` the same way.
"""

import asyncio
import logging
import shlex
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Protocol

logger = logging.getLogger(__name__)
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
MAX_FRAME_BYTES = 1024 * 1024


class CameraError(Exception):
    pass


class FrameSource(Protocol):
    name: str

    def frames(self) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """Open the camera; the context yields an iterator of JPEG frames."""


class PiCamera:
    """Pi Camera 3 through `rpicam-vid`, streamed as MJPEG on stdout."""

    name = "picamera"

    def __init__(self, width=640, height=360, fps=10, extra_args: str = ""):
        self.command = [
            "rpicam-vid", "--nopreview", "--timeout", "0", "--codec", "mjpeg",
            "--width", str(width), "--height", str(height), "--framerate", str(fps),
            *shlex.split(extra_args), "--output", "-",
        ]

    @asynccontextmanager
    async def frames(self):
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, NotImplementedError) as error:
            raise CameraError("無法啟動 rpicam-vid，請確認相機連接與 rpicam-apps 安裝") from error
        try:
            assert process.stdout is not None
            yield jpeg_frames(process.stdout)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


async def jpeg_frames(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    buffer = bytearray()
    while chunk := await stream.read(32 * 1024):
        buffer.extend(chunk)
        while buffer:
            start = buffer.find(JPEG_START)
            if start < 0:
                del buffer[:-1]
                break
            if start:
                del buffer[:start]
            end = buffer.find(JPEG_END, 2)
            if end < 0:
                if len(buffer) > MAX_FRAME_BYTES:
                    raise CameraError("相機畫格超出大小限制")
                break
            end += len(JPEG_END)
            yield bytes(buffer[:end])
            del buffer[:end]
    raise CameraError("相機串流已結束")
