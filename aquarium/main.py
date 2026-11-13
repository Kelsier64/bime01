"""HTTP/WebSocket API and dashboard for the aquarium monitor.

Run a single Uvicorn worker: the active monitor owns the camera.

Environment:
  AQUARIUM_SOURCE       preferred source on first start: "picamera", "fake" or "video:<file stem>"
                        (afterwards the source picked in the dashboard is remembered)
  AQUARIUM_DATA         data directory, default .data
  AQUARIUM_VIDEOS       folder of test videos played as a camera, default .data/videos
  AQUARIUM_CAMERA       "1" / "0" to force the Pi camera option on or off (default: if rpicam-vid exists)
  AQUARIUM_RPICAM_ARGS  extra rpicam-vid arguments, e.g. "--autofocus-mode manual --lens-position 3"
"""

import asyncio
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .devices import DeviceError, Feeder
from .hub import Catalog, Hub
from .monitor import Monitor
from .settings import Model, Settings
from .sources import VIDEO_SUFFIXES

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "frontend" / "dist"
SNAPSHOT_NAME = re.compile(r"^\d+-[a-z_]+\.jpg$")
VIDEO_NAME = re.compile(r"^[\w][\w .()-]{0,80}$")
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
RANGES = {"1h": (3600, 60), "24h": (86_400, 600), "7d": (7 * 86_400, 3600), "30d": (30 * 86_400, 10_800)}
STATE_INTERVAL_S = 0.5


class SourceChoice(Model):
    id: str


class ClockSync(Model):
    now_ms: int


CLOCK_TOLERANCE_MS = 30_000


async def set_system_clock(now_ms: int) -> str | None:
    """Set the Pi's clock (no battery clock, often no NTP over USB). Returns an error message or None."""
    if os.name != "posix":
        return "只能在 Raspberry Pi 上調整系統時間"
    try:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", "date", "-u", "-s", f"@{now_ms / 1000:.3f}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), 10)
    except (OSError, TimeoutError) as error:
        return f"無法執行 date：{error}"
    if process.returncode:
        return "沒有權限調整時間（需要免密碼 sudo）：" + error.decode(errors="replace").strip()
    return None


class DemoAction(Model):
    action: Literal["dark", "sluggish", "offline", "reset"]


def create_app(
    *, catalog: Catalog, data_root: Path, preferred: str | None = None,
    feeder: Feeder | None = None, start_monitor: bool = True,
) -> FastAPI:
    hub = Hub(catalog, data_root, feeder, start_monitor)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.activate(hub.initial(preferred))
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title="Aquarium Lab", version="0.3.0", lifespan=lifespan)
    app.state.hub = hub

    def monitor() -> Monitor:
        if hub.monitor is None:
            raise HTTPException(503, "影像來源切換中")
        return hub.monitor

    async def same_origin(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "指令需要來自控制台同源")

    write = [Depends(same_origin)]

    @app.get("/api/state")
    async def state():
        return monitor().state()

    @app.post("/api/clock", dependencies=write)
    async def sync_clock(body: ClockSync):
        """Set the system clock from the dashboard computer's time when they disagree."""
        current = monitor()
        skew = body.now_ms - current.clock()
        if abs(skew) <= CLOCK_TOLERANCE_MS:
            return {"adjusted": False, "skew_ms": skew}
        problem = await set_system_clock(body.now_ms)
        if problem:
            raise HTTPException(409, problem)
        current.clock_changed()
        current.store.add_event(current.clock(), "clock", {"skew_ms": skew})
        return {"adjusted": True, "skew_ms": skew}

    @app.get("/api/sources")
    async def sources():
        return hub.sources()

    @app.put("/api/source", dependencies=write)
    async def choose_source(choice: SourceChoice):
        try:
            await hub.activate(choice.id)
        except KeyError as error:
            raise HTTPException(404, "找不到這個影像來源") from error
        return monitor().state()

    @app.put("/api/videos/{name}", dependencies=write)
    async def upload_video(name: str, request: Request):
        """Raw request body upload; the video then appears as a source."""
        path = catalog.videos / name
        if not VIDEO_NAME.match(name) or path.suffix.lower() not in VIDEO_SUFFIXES:
            raise HTTPException(422, f"檔名需為英數字，副檔名為 {'、'.join(sorted(VIDEO_SUFFIXES))}")
        catalog.videos.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        size = 0
        try:
            with partial.open("wb") as file:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "影片超過 300 MB")
                    file.write(chunk)
            if not size:
                raise HTTPException(422, "沒有收到影片內容")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        return hub.sources()

    @app.get("/api/frame.jpg", responses={200: {"content": {"image/jpeg": {}}}}, response_class=Response)
    async def frame():
        current = monitor()
        if current.frame is None:
            raise HTTPException(404, "尚未收到相機畫面")
        return Response(current.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/history")
    async def history(range: Literal["1h", "24h", "7d", "30d"] = "24h"):
        span, step = RANGES[range]
        current = monitor()
        end = current.clock() // 1000
        start = (end - span) // step * step
        result = current.store.history(start, end, step)
        events = current.store.events(start * 1000, end * 1000 + 999, 100)
        return {"range": range, "start_ms": start * 1000, "end_ms": end * 1000, "step_ms": step * 1000,
                **result, "events": events}

    @app.get("/api/events")
    async def events(limit: int = 30):
        current = monitor()
        return current.store.events(0, current.clock(), max(1, min(limit, 200)))

    @app.get("/api/feeding")
    async def feeding():
        return monitor().feeding_response()

    @app.post("/api/feed", dependencies=write)
    async def feed():
        current = monitor()
        try:
            await current.feed()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except DeviceError as error:
            raise HTTPException(503, "餵食器沒有確認指令") from error
        return current.feeding_response()

    @app.get("/api/settings")
    async def get_settings() -> Settings:
        return monitor().settings.value

    @app.put("/api/settings", dependencies=write)
    async def put_settings(settings: Settings) -> Settings:
        current = monitor()
        previous = current.settings.value
        current.settings.save(settings)
        current.configure()
        if settings.calibration != previous.calibration:
            current.store.add_event(current.clock(), "calibration", {
                "tank_width_cm": settings.calibration.tank_width_cm if settings.calibration else None,
                "tank_height_cm": settings.calibration.tank_height_cm if settings.calibration else None,
            }, current.save_snapshot("calibration"))
        return settings

    @app.post("/api/demo", dependencies=write)
    async def demo(body: DemoAction):
        current = monitor()
        control = getattr(current.source, "demo", None)
        if control is None:
            raise HTTPException(409, "模擬情境只在假相機模式可用")
        control(body.action)
        return current.state()

    @app.get("/api/snapshots/{name}", include_in_schema=False)
    async def snapshot(name: str):
        path = monitor().snapshots / name
        if not SNAPSHOT_NAME.match(name) or not path.is_file():
            raise HTTPException(404, "找不到快照")
        return FileResponse(path, media_type="image/jpeg")

    @app.websocket("/ws/live")
    async def live(socket: WebSocket, video: bool = True):
        """JSON state messages, each followed by the JPEG it describes when video is on."""
        await socket.accept()
        current = None
        sent_version = -1
        loop = asyncio.get_running_loop()
        last_state = 0.0
        try:
            while True:
                if hub.monitor is not current:
                    current, sent_version = hub.monitor, -1
                if current is None:
                    await asyncio.sleep(STATE_INTERVAL_S)
                    continue
                event = current.frame_event
                with suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), STATE_INTERVAL_S)
                fresh = video and current.frame is not None and current.frame_version != sent_version
                if fresh or loop.time() - last_state >= STATE_INTERVAL_S:
                    frame = current.frame
                    await socket.send_json(current.state())
                    last_state = loop.time()
                    if fresh:
                        sent_version = current.frame_version
                        await socket.send_bytes(frame)
        except (WebSocketDisconnect, RuntimeError):
            pass

    if (UI / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        if (UI / "index.html").is_file():
            return FileResponse(UI / "index.html")
        return PlainTextResponse("Build the UI with npm --prefix frontend run build. API docs at /docs.", 503)

    return app


def app_from_environment() -> FastAPI:
    data_root = Path(os.getenv("AQUARIUM_DATA", str(ROOT / ".data")))
    camera = {"1": True, "0": False}.get(os.getenv("AQUARIUM_CAMERA", ""))
    catalog = Catalog(
        Path(os.getenv("AQUARIUM_VIDEOS", str(data_root / "videos"))), camera,
        os.getenv("AQUARIUM_RPICAM_ARGS", ""),
    )
    return create_app(catalog=catalog, data_root=data_root, preferred=os.getenv("AQUARIUM_SOURCE", "picamera"))


app = app_from_environment()
