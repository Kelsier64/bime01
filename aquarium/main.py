import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .contracts import (
    DEPTH, FEED_POINT, FEED_RADIUS, HEIGHT, MIN_TRANSMITTANCE, WIDTH, Actuator, DeviceCommand,
    DeviceUnavailable, FeedCommand, LightCommand, Message, ObservationFrame, Telemetry,
)
from .history import History, measure
from .simulation import AUTO_FEED_SECONDS, TICK_SECONDS, Simulation

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "frontend" / "dist"
FEED_COOLDOWN_MS = 30_000
TRACK_FRESH_MS = 2500
SENSOR_FRESH_MS = 10_000


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class LightSetting(Message):
    mode: Literal["auto", "on", "off"]


class DemoSetting(Message):
    action: Literal["lost", "clarity", "reset"]


class Aquarium:
    def __init__(self, history: History, mode: str, actuator: Actuator | None):
        self.history = history
        self.mode = mode
        self.simulation = Simulation() if mode == "simulation" else None
        self.actuator = self.simulation if self.simulation is not None else actuator
        self.commands = asyncio.Lock()
        self.reset_live_state()

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
        self.fish = [
            {
                "id": track.id,
                "status": track.status,
                "position": track.position.model_dump() if track.position else last_positions.get(track.id),
                "velocity": velocities.get(track.id),
            }
            for track in frame.tracks
        ]
        if frame.timestamp_ms // 1000 > self.last_motion_sample:
            self.history.save_motion(self.mode, self.run_id, frame.timestamp_ms, sample)
            self.last_motion_sample = frame.timestamp_ms // 1000
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
            "run_id": self.run_id,
            "tank": {
                "width": WIDTH, "height": HEIGHT, "depth": DEPTH,
                "feed_point": FEED_POINT, "feed_radius": FEED_RADIUS,
            },
            "tracking_fresh": track_fresh,
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
    *, mode: Literal["simulation", "hardware"] = "simulation",
    database: Path = ROOT / ".data" / "aquarium.sqlite3",
    ingest_token: str = "", actuator: Actuator | None = None,
) -> FastAPI:
    if mode not in ("simulation", "hardware"):
        raise ValueError("AQUARIUM_MODE must be simulation or hardware")
    if mode == "hardware" and not ingest_token:
        raise ValueError("Hardware mode requires AQUARIUM_INGEST_TOKEN")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        history = History(database)
        app.state.aquarium = Aquarium(history, mode, actuator)
        task = asyncio.create_task(app.state.aquarium.simulate()) if mode == "simulation" else None
        try:
            yield
        finally:
            try:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            finally:
                history.db.close()

    app = FastAPI(title="Aquarium Lab", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    async def require_hardware(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if mode != "hardware":
            raise HTTPException(409, "請先切換至硬體模式")
        if credentials is None or not compare_digest(credentials.credentials.encode(), ingest_token.encode()):
            raise HTTPException(401, "設備權杖無效", headers={"WWW-Authenticate": "Bearer"})

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
        return aquarium.history.read(mode, aquarium.run_id, range, now_ms())

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

    @app.post("/api/ingest/observations", status_code=204, dependencies=[Depends(require_hardware)])
    async def observations(frame: ObservationFrame):
        app.state.aquarium.accept_frame(frame)

    @app.post("/api/ingest/telemetry", status_code=204, dependencies=[Depends(require_hardware)])
    async def telemetry(sample: Telemetry):
        app.state.aquarium.accept_telemetry(sample)

    if (UI / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=UI / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        if (UI / "index.html").is_file():
            return FileResponse(UI / "index.html")
        return PlainTextResponse("Build the UI with npm --prefix frontend run build. API docs at /docs.", 503)

    return app


app = create_app(
    mode=os.getenv("AQUARIUM_MODE", "simulation"),
    database=Path(os.getenv("AQUARIUM_DB", str(ROOT / ".data" / "aquarium.sqlite3"))),
    ingest_token=os.getenv("AQUARIUM_INGEST_TOKEN", ""),
)
