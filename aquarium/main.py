"""HTTP/WebSocket API and dashboard for the aquarium monitor.

Run a single Uvicorn worker: the monitor task owns the camera.

Environment:
  AQUARIUM_SOURCE       "picamera" (Pi Camera 3) or "fake" (rendered test tank, default)
  AQUARIUM_DATA         data directory; defaults to .data/<source>
  AQUARIUM_RPICAM_ARGS  extra rpicam-vid arguments, e.g. "--lens-position 3"
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

from .devices import DeviceError, Feeder, ManualFeeder
from .monitor import Monitor
from .settings import Model, Settings, SettingsStore
from .sources import FrameSource
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "frontend" / "dist"
SNAPSHOT_NAME = re.compile(r"^\d+-[a-z_]+\.jpg$")
RANGES = {"1h": (3600, 60), "24h": (86_400, 600), "7d": (7 * 86_400, 3600), "30d": (30 * 86_400, 10_800)}
STATE_INTERVAL_S = 0.5


class DemoAction(Model):
    action: Literal["dark", "sluggish", "offline", "reset"]


def create_app(
    *, source: FrameSource, data_dir: Path, feeder: Feeder | None = None,
    default_settings: Settings | None = None, seed=None, start_monitor: bool = True,
) -> FastAPI:
    """`seed(store, now_s, settings)` fills a new database (fake mode only)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(data_dir / "aquarium.sqlite3")
        settings = SettingsStore(data_dir / "settings.json", default_settings or Settings())
        monitor = Monitor(
            source, store, settings, feeder or ManualFeeder(getattr(source, "feed", None)), data_dir / "snapshots",
        )
        if seed is not None:
            seed(store, monitor.clock() // 1000, settings.value)
        app.state.monitor = monitor
        task = asyncio.create_task(monitor.run()) if start_monitor else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            monitor.close()
            store.close()

    app = FastAPI(title="Aquarium Lab", version="0.2.0", lifespan=lifespan)

    def monitor() -> Monitor:
        return app.state.monitor

    async def same_origin(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "指令需要來自控制台同源")

    write = [Depends(same_origin)]

    @app.get("/api/state")
    async def state():
        return monitor().state()

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
        path = data_dir / "snapshots" / name
        if not SNAPSHOT_NAME.match(name) or not path.is_file():
            raise HTTPException(404, "找不到快照")
        return FileResponse(path, media_type="image/jpeg")

    @app.websocket("/ws/live")
    async def live(socket: WebSocket, video: bool = True):
        """JSON state messages, each followed by the JPEG it describes when video is on."""
        await socket.accept()
        current = monitor()
        sent_version = -1
        loop = asyncio.get_running_loop()
        last_state = 0.0
        try:
            while True:
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
    kind = os.getenv("AQUARIUM_SOURCE", "fake")
    data_dir = Path(os.getenv("AQUARIUM_DATA", str(ROOT / ".data" / kind)))
    if kind == "picamera":
        from .sources import PiCamera

        return create_app(source=PiCamera(extra_args=os.getenv("AQUARIUM_RPICAM_ARGS", "")), data_dir=data_dir)
    if kind == "fake":
        from .fake import FakeTank, default_settings, seed_history

        return create_app(
            source=FakeTank(), data_dir=data_dir, default_settings=default_settings(),
            seed=lambda store, now_s, settings: seed_history(store, now_s, settings.fish_count),
        )
    raise ValueError("AQUARIUM_SOURCE must be picamera or fake")


app = app_from_environment()
