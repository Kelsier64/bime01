import asyncio
import logging
import subprocess
from contextlib import asynccontextmanager, suppress
from threading import Lock

logger = logging.getLogger(__name__)
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
MAX_FRAME_BYTES = 1024 * 1024


class CameraUnavailable(Exception):
    pass


class RaspberryPiCamera:
    def __init__(self):
        self.capture_lock = Lock()

    def capture(self) -> bytes:
        if not self.capture_lock.acquire(blocking=False):
            raise CameraUnavailable("相機正在擷取，請稍後再試")
        try:
            result = subprocess.run(
                [
                    "rpicam-still", "--nopreview", "--timeout", "700", "--width", "960",
                    "--height", "540", "--quality", "82", "--encoding", "jpg", "--output", "-",
                ],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.warning("Camera capture failed: %s", error)
            raise CameraUnavailable("無法擷取相機影像，請檢查相機連線") from error
        finally:
            self.capture_lock.release()

        image = result.stdout
        if result.returncode or not (image.startswith(JPEG_START) and image.endswith(JPEG_END)):
            logger.warning("Camera returned no JPEG: %s", result.stderr.decode(errors="replace").strip())
            raise CameraUnavailable("無法擷取相機影像，請檢查相機連線")
        return image

    @asynccontextmanager
    async def live(self):
        if not self.capture_lock.acquire(blocking=False):
            raise CameraUnavailable("相機使用中，請先關閉另一個相機預覽")
        process = None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    "rpicam-vid", "--nopreview", "--timeout", "0", "--codec", "mjpeg",
                    "--width", "640", "--height", "360", "--framerate", "10",
                    "--output", "-", stdout=asyncio.subprocess.PIPE,
                )
            except (OSError, NotImplementedError) as error:
                logger.warning("Camera stream could not start: %s", error)
                raise CameraUnavailable("無法啟動相機，請確認 Zero 2 W 已安裝 rpicam-vid") from error
            assert process.stdout is not None
            yield jpeg_frames(process.stdout)
        finally:
            try:
                if process is not None:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.terminate()
                    try:
                        await asyncio.wait_for(process.communicate(), 2)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.communicate()
            finally:
                self.capture_lock.release()


async def jpeg_frames(source: asyncio.StreamReader):
    buffer = bytearray()
    while chunk := await source.read(32 * 1024):
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
                    raise CameraUnavailable("相機送出的畫格超出大小限制")
                break
            end += len(JPEG_END)
            if end > MAX_FRAME_BYTES:
                raise CameraUnavailable("相機送出的畫格超出大小限制")
            yield bytes(buffer[:end])
            del buffer[:end]
