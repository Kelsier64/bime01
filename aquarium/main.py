import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal
from uuid import uuid4

from anyio import CancelScope
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .camera import CameraUnavailable, RaspberryPiCamera
from .contracts import (
    FEED_POINT, FEED_RADIUS, HEIGHT, MIN_TRANSMITTANCE, WIDTH, Actuator, DeviceCommand,
    DeviceUnavailable, FeedCommand, LightCommand, Message, ObservationFrame, Telemetry,
)
from .history import History, measure
from .simulation import AUTO_FEED_SECONDS, TICK_SECONDS, Simulation

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "frontend" / "dist"
FEED_COOLDOWN_MS = 30_000
TRACK_FRESH_MS = 2500
SENSOR_FRESH_MS = 10_000
logger = logging.getLogger(__name__)
Mode = Literal["simulation", "hardware"]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def create_live_tracker(fish: int, profile: str):
    from .vision import LiveFishTracker

    return LiveFishTracker(fish, profile)


class LightSetting(Message):
    mode: Literal["auto", "on", "off"]


class DemoSetting(Message):
    action: Literal["lost", "clarity", "reset"]


class SimulationSetting(Message):
    enabled: bool


class Aquarium:
    def __init__(self, history: History, mode: Mode, actuator: Actuator | None, camera_enabled: bool = False):
        self.history = history
        self.hardware_actuator = actuator
        self.camera_enabled = camera_enabled
        self.camera_live = False
        self.camera_tracking: dict | None = None
        self.commands = asyncio.Lock()
        self.simulation_task: asyncio.Task | None = None
        self.configure_mode(mode)

    def configure_mode(self, mode: Mode):
        if self.simulation_task is not None:
            self.simulation_task.cancel()
        self.mode = mode
        self.simulation = Simulation() if mode == "simulation" else None
        self.actuator = self.simulation if self.simulation is not None else self.hardware_actuator
        self.reset_live_state()
        self.simulation_task = asyncio.create_task(self.simulate()) if self.simulation is not None else None

    async def set_simulation(self, enabled: bool):
        async with self.commands:
            if enabled and self.camera_live:
                raise HTTPException(409, "請先停止即時影像，再開啟模擬")
            mode = "simulation" if enabled else "hardware"
            if self.mode != mode:
                self.configure_mode(mode)

    async def start_camera(self):
        async with self.commands:
            if self.camera_live:
                raise CameraUnavailable("相機使用中，請先關閉另一個相機預覽")
            if self.mode != "hardware":
                self.configure_mode("hardware")
            self.camera_live = True

    def reset_live_state(self):
        self.run_id = uuid4().hex
        self.frame: ObservationFrame | None = None
        self.telemetry: Telemetry | None = None
        self.fish: list[dict] = []
        self.last_motion_sample = 0
        self.last_sensor_sample = 0
        self.last_feed: int | None = None
        self.next_feed = now_ms() + AUTO_FEED_SECONDS * 1000 if self.simulation else None
        self.light_mode = "auto" if self.simulation else "unknown"
        self.light_on = True if self.simulation else None

    def accept_frame(self, frame: ObservationFrame):
        self.check_timestamp(frame.timestamp_ms, self.frame, TRACK_FRESH_MS)
        velocities, sample = measure(frame, self.frame)
        last_positions = {fish["id"]: fish["position"] for fish in self.fish}
        fish = [
            {
                "id": track.id,
                "status": track.status,
                "position": (
                    track.position.model_dump() if track.status == "tracked" and track.position is not None
                    else last_positions.get(track.id)
                ),
                "velocity": velocities.get(track.id),
            }
            for track in frame.tracks
        ]
        if frame.timestamp_ms // 1000 > self.last_motion_sample:
            self.history.save_motion(self.mode, self.run_id, frame.timestamp_ms, sample)
            self.last_motion_sample = frame.timestamp_ms // 1000
        self.fish = fish
        self.frame = frame

    def accept_telemetry(self, telemetry: Telemetry):
        self.check_timestamp(telemetry.timestamp_ms, self.telemetry, SENSOR_FRESH_MS)
        if telemetry.timestamp_ms // 1000 > self.last_sensor_sample:
            self.history.save_telemetry(self.mode, telemetry)
            self.last_sensor_sample = telemetry.timestamp_ms // 1000
        self.telemetry = telemetry

    @staticmethod
    def check_timestamp(timestamp, previous, freshness):
        current = now_ms()
        if not current - freshness <= timestamp <= current + 1000:
            raise HTTPException(422, "資料時間超出即時觀測範圍，請確認設備時鐘")
        if previous is not None and timestamp <= previous.timestamp_ms:
            raise HTTPException(409, "資料時間必須比前一筆新")

    async def execute(self, command: DeviceCommand, origin: str) -> int:
        if self.actuator is None:
            raise HTTPException(503, "設備控制器尚未接入")
        try:
            await self.actuator.execute(command)
        except DeviceUnavailable as error:
            raise HTTPException(503, "設備未確認指令") from error
        timestamp = now_ms()
        self.history.save_event(
            command.id, self.mode, self.run_id, timestamp, command.action, origin,
            command.enabled if isinstance(command, LightCommand) else None,
        )
        return timestamp

    async def feed(self, origin: str):
        async with self.commands:
            if self.last_feed is not None and now_ms() - self.last_feed < FEED_COOLDOWN_MS:
                raise HTTPException(409, "兩次餵食需間隔至少 30 秒")
            self.last_feed = await self.execute(FeedCommand(id=uuid4().hex), origin)
            if self.simulation:
                self.next_feed = self.last_feed + AUTO_FEED_SECONDS * 1000

    def auto_light(self) -> bool:
        assert self.simulation is not None
        return self.simulation.elapsed % 80 < 65

    async def set_light(self, setting: str):
        async with self.commands:
            if setting == "auto" and self.simulation is None:
                raise HTTPException(409, "自動排程目前適用於模擬模式")
            enabled = self.auto_light() if setting == "auto" else setting == "on"
            if enabled != self.light_on:
                await self.execute(
                    LightCommand(id=uuid4().hex, enabled=enabled),
                    "auto" if setting == "auto" else "manual",
                )
            self.light_on = enabled
            self.light_mode = setting

    async def simulate(self):
        assert self.simulation is not None
        while True:
            timestamp = now_ms()
            if self.frame is None or timestamp > self.frame.timestamp_ms:
                frame, telemetry = self.simulation.step(timestamp)
                self.accept_frame(frame)
                self.accept_telemetry(telemetry)
            if self.next_feed is not None and timestamp >= self.next_feed:
                await self.feed("auto")
            if self.light_mode == "auto" and self.auto_light() != self.light_on:
                await self.set_light("auto")
            await asyncio.sleep(TICK_SECONDS)

    def snapshot(self):
        timestamp = now_ms()
        track_fresh = self.frame is not None and timestamp - self.frame.timestamp_ms <= TRACK_FRESH_MS
        sensor_fresh = self.telemetry is not None and timestamp - self.telemetry.timestamp_ms <= SENSOR_FRESH_MS
        telemetry = self.telemetry.model_dump() if sensor_fresh else None
        clarity = telemetry["transmittance_pct"] if telemetry else None
        return {
            "timestamp_ms": timestamp,
            "source": self.mode,
            "camera_enabled": self.camera_enabled,
            "camera_live": self.camera_live,
            "camera_tracking": self.camera_tracking,
            "run_id": self.run_id,
            "tank": {
                "width": WIDTH, "height": HEIGHT,
                "feed_point": FEED_POINT, "feed_radius": FEED_RADIUS,
            },
            "tracking_fresh": track_fresh,
            "observation_timestamp_ms": self.frame.timestamp_ms if self.frame else None,
            "fish": self.fish,
            "telemetry": telemetry,
            "clarity_alert": clarity is not None and clarity < MIN_TRANSMITTANCE,
            "controls": {
                "available": self.actuator is not None,
                "light_mode": self.light_mode,
                "light_on": self.light_on,
                "last_feed_ms": self.last_feed,
                "next_feed_ms": self.next_feed,
                "can_feed": self.last_feed is None or timestamp - self.last_feed >= FEED_COOLDOWN_MS,
                "low_clarity": self.simulation.low_clarity if self.simulation else False,
            },
        }


def create_app(
    *, mode: Mode = "simulation",
    database: Path = ROOT / ".data" / "aquarium.sqlite3",
    ingest_token: str = "", actuator: Actuator | None = None,
    camera_enabled: bool = False,
    replay_directory: Path = ROOT / ".data" / "replay",
) -> FastAPI:
    if mode not in ("simulation", "hardware"):
        raise ValueError("AQUARIUM_MODE must be simulation or hardware")
    camera = RaspberryPiCamera() if camera_enabled else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        history = History(database)
        aquarium = Aquarium(history, mode, actuator, camera_enabled=camera_enabled)
        app.state.aquarium = aquarium
        try:
            yield
        finally:
            try:
                task = aquarium.simulation_task
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            finally:
                history.db.close()

    app = FastAPI(title="Aquarium Lab", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)
    IngestCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]

    def require_hardware(credentials: HTTPAuthorizationCredentials | None) -> Aquarium:
        aquarium = app.state.aquarium
        if aquarium.mode != "hardware":
            raise HTTPException(409, "請先切換至硬體模式")
        if not ingest_token or credentials is None or not compare_digest(credentials.credentials.encode(), ingest_token.encode()):
            raise HTTPException(401, "設備權杖無效", headers={"WWW-Authenticate": "Bearer"})
        return aquarium

    async def same_origin(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "控制指令需要來自控制台同源")

    @app.get("/api/state")
    async def state():
        return app.state.aquarium.snapshot()

    @app.get("/api/history")
    async def history(range: Literal["1m", "15m", "24h"] = "1m"):
        aquarium = app.state.aquarium
        result = aquarium.history.read(aquarium.mode, aquarium.run_id, range, now_ms())
        return {**result, "source": aquarium.mode, "run_id": aquarium.run_id}

    @app.put("/api/simulation", dependencies=[Depends(same_origin)])
    async def simulation(setting: SimulationSetting):
        aquarium = app.state.aquarium
        await aquarium.set_simulation(setting.enabled)
        return aquarium.snapshot()

    @app.post(
        "/api/camera/snapshot", dependencies=[Depends(same_origin)],
        responses={200: {"content": {"image/jpeg": {}}}}, response_class=Response,
    )
    def camera_snapshot():
        if camera is None:
            raise HTTPException(503, "相機預覽尚未啟用")
        try:
            image = camera.capture()
        except CameraUnavailable as error:
            raise HTTPException(503, str(error)) from error
        return Response(image, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin",
        })

    @app.websocket("/ws/camera/live")
    async def camera_live(
        socket: WebSocket, fish: Annotated[int | None, Query(ge=1, le=2)] = None,
        profile: Literal["auto", "contrast", "gold"] = "auto",
    ):
        origin = socket.headers.get("origin")
        scheme = "https" if socket.url.scheme == "wss" else "http"
        if origin is not None and origin != f"{scheme}://{socket.url.netloc}":
            await socket.close(code=1008)
            return
        await socket.accept()

        async def send_frames():
            if camera is None:
                raise CameraUnavailable("相機預覽尚未啟用")
            aquarium = app.state.aquarium
            await aquarium.start_camera()
            try:
                tracker = None
                if fish is not None:
                    try:
                        tracker = await asyncio.to_thread(create_live_tracker, fish, profile)
                    except ImportError as error:
                        logger.warning("Live tracking dependencies unavailable: %s", error)
                async with camera.live() as frames:
                    while True:
                        try:
                            image = await asyncio.wait_for(anext(frames), 8)
                        except (StopAsyncIteration, TimeoutError) as error:
                            raise CameraUnavailable("相機沒有送出影像，請檢查相機連線") from error
                        if fish is not None:
                            tracking = (await asyncio.to_thread(tracker.process, image) if tracker is not None
                                        else {"stage": "unavailable", "tracks": []})
                            aquarium.camera_tracking = {**tracking, "timestamp_ms": now_ms()}
                            await socket.send_json({"type": "camera_tracking", **aquarium.camera_tracking})
                        await socket.send_bytes(image)
            finally:
                aquarium.camera_live = False
                aquarium.camera_tracking = None

        async def wait_for_disconnect():
            while (await socket.receive())["type"] != "websocket.disconnect":
                pass

        stream = asyncio.create_task(send_frames())
        disconnect = asyncio.create_task(wait_for_disconnect())
        try:
            done, _ = await asyncio.wait((stream, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect not in done:
                stream.result()
        except CameraUnavailable as error:
            with suppress(WebSocketDisconnect):
                await socket.send_text(str(error))
                await socket.close(code=1011)
        except WebSocketDisconnect:
            pass
        finally:
            with CancelScope(shield=True):
                stream.cancel()
                disconnect.cancel()
                await asyncio.gather(stream, disconnect, return_exceptions=True)

    @app.websocket("/ws/live")
    async def live(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                await socket.send_json(app.state.aquarium.snapshot())
                try:
                    if (await asyncio.wait_for(socket.receive(), TICK_SECONDS))["type"] == "websocket.disconnect":
                        break
                except TimeoutError:
                    continue
        except WebSocketDisconnect:
            pass

    @app.post("/api/control/feed", dependencies=[Depends(same_origin)])
    async def feed():
        aquarium = app.state.aquarium
        await aquarium.feed("manual")
        return aquarium.snapshot()

    @app.put("/api/control/light", dependencies=[Depends(same_origin)])
    async def light(setting: LightSetting):
        aquarium = app.state.aquarium
        await aquarium.set_light(setting.mode)
        return aquarium.snapshot()

    @app.post("/api/demo", dependencies=[Depends(same_origin)])
    async def demo(setting: DemoSetting):
        aquarium = app.state.aquarium
        simulator = aquarium.simulation
        if simulator is None:
            raise HTTPException(409, "此功能適用於模擬模式")
        if setting.action == "lost":
            simulator.lost_until = simulator.elapsed + 7
        elif setting.action == "clarity":
            simulator.low_clarity = not simulator.low_clarity
        else:
            simulator.reset()
            aquarium.reset_live_state()
        return aquarium.snapshot()

    @app.post("/api/ingest/observations", status_code=204)
    async def observations(frame: ObservationFrame, credentials: IngestCredentials):
        require_hardware(credentials).accept_frame(frame)

    @app.post("/api/ingest/telemetry", status_code=204)
    async def telemetry(sample: Telemetry, credentials: IngestCredentials):
        require_hardware(credentials).accept_telemetry(sample)

    @app.get("/api/replay/video", include_in_schema=False)
    def replay_video():
        path = replay_directory / "front.webm"
        if not path.is_file():
            raise HTTPException(404, "尚未準備正面測試影片")
        return FileResponse(path, media_type="video/webm", headers={"Cache-Control": "no-cache"})

    @app.get("/api/replay/tracks", include_in_schema=False)
    def replay_tracks():
        path = replay_directory / "tracks.csv"
        if not path.is_file():
            raise HTTPException(404, "尚未準備 2D 追蹤結果")
        return FileResponse(path, media_type="text/csv", headers={"Cache-Control": "no-cache"})

    if (UI / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")

    @app.get("/replay", include_in_schema=False)
    async def replay():
        if (UI / "replay.html").is_file():
            return FileResponse(UI / "replay.html")
        return PlainTextResponse("Build the UI with npm --prefix frontend run build.", 503)

    @app.get("/", include_in_schema=False)
    async def index():
        if (UI / "index.html").is_file():
            return FileResponse(UI / "index.html")
        return PlainTextResponse("Build the UI with npm --prefix frontend run build. API docs at /docs.", 503)

    return app


camera_enabled = os.getenv("AQUARIUM_CAMERA") == "1"
app = create_app(
    mode=os.getenv("AQUARIUM_MODE", "hardware" if camera_enabled else "simulation"),
    database=Path(os.getenv("AQUARIUM_DB", str(ROOT / ".data" / "aquarium.sqlite3"))),
    ingest_token=os.getenv("AQUARIUM_INGEST_TOKEN", ""),
    camera_enabled=camera_enabled,
)
