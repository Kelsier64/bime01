"""Camera frame sources. Each yields JPEG frames from an async iterator.

An iterator may also yield `None` to mark a cut (a looping video jumping back
to its start), so the pipeline does not count the jump as fish movement.
A new source (for example the ESP32-CAM's MJPEG stream for the future 3D
version) only has to implement `frames()` the same way.
"""

import asyncio
import logging
import shlex
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
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

    def frames(self) -> AbstractAsyncContextManager[AsyncIterator[bytes | None]]:
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
                *self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, NotImplementedError) as error:
            raise CameraError("無法啟動 rpicam-vid，請確認相機連接與 rpicam-apps 安裝") from error
        errors: deque[str] = deque(maxlen=5)

        async def collect_errors():
            assert process.stderr is not None
            while line := await process.stderr.readline():
                errors.append(line.decode(errors="replace").strip())

        collector = asyncio.create_task(collect_errors())

        async def frames():
            assert process.stdout is not None
            try:
                async for frame in jpeg_frames(process.stdout):
                    yield frame
            except CameraError as error:
                await asyncio.sleep(0.2)
                detail = next((line for line in reversed(errors) if line), None)
                raise CameraError(f"rpicam-vid 停止：{detail}" if detail else str(error)) from error

        try:
            yield frames()
        finally:
            collector.cancel()
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


VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v"}


class VideoFile:
    """Plays a recorded video in real time as if it were the camera, looping at the end."""

    def __init__(self, path: Path, max_width=640, max_fps=10):
        self.path = path
        self.name = f"video:{path.stem}"
        self.max_width = max_width
        self.max_fps = max_fps

    @asynccontextmanager
    async def frames(self):
        import cv2

        capture = await asyncio.to_thread(cv2.VideoCapture, str(self.path))
        if not capture.isOpened():
            raise CameraError(f"無法開啟影片 {self.path.name}")
        fps = capture.get(cv2.CAP_PROP_FPS)
        fps = fps if 1 <= fps <= 240 else 15
        step = max(1, round(fps / self.max_fps))
        try:
            yield self.play(capture, step, step / fps)
        finally:
            await asyncio.to_thread(capture.release)

    async def play(self, capture, step, interval):
        import cv2

        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            frame = await asyncio.to_thread(self.read, capture, step)
            if frame is None:
                await asyncio.to_thread(capture.set, cv2.CAP_PROP_POS_FRAMES, 0)
                frame = await asyncio.to_thread(self.read, capture, 1)
                if frame is None:
                    raise CameraError(f"無法讀取影片 {self.path.name}")
                yield None
            due += interval
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -1:
                due = loop.time()
            yield frame

    def read(self, capture, step) -> bytes | None:
        import cv2

        for _ in range(step - 1):
            capture.grab()
        ok, image = capture.read()
        if not ok:
            return None
        height, width = image.shape[:2]
        if width > self.max_width:
            image = cv2.resize(image, (self.max_width, round(height * self.max_width / width)), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", image, (cv2.IMWRITE_JPEG_QUALITY, 85))
        return encoded.tobytes() if ok else None


def find_videos(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.suffix.lower() in VIDEO_SUFFIXES and path.is_file())
