"""The always-on pipeline: camera → detection → calibration → recording → alerts.

It runs as a background task from server start, whether or not anyone has the
dashboard open. Viewers only subscribe to its latest frame and state.
"""

import asyncio
import logging
import time
from collections import deque
from pathlib import Path

import cv2

from .alerts import Alerts
from .calibration import TankMapper
from .devices import Feeder
from .recorder import Recorder
from .settings import SettingsStore
from .sources import FrameSource
from .store import Store
from .vision import FishTracker, brightness, decode, water_mask

logger = logging.getLogger(__name__)
FRAME_STALL_S = 10
LIVE_WITHIN_MS = 3000
DARK_HYSTERESIS = 5
FEED_DEBOUNCE_MS = 10_000
KEEP_SECONDS_DAYS = 7
KEEP_SNAPSHOTS = 500


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Monitor:
    def __init__(self, source: FrameSource, store: Store, settings: SettingsStore, feeder: Feeder,
                 snapshots: Path, clock=now_ms):
        self.source = source
        self.store = store
        self.settings = settings
        self.feeder = feeder
        self.snapshots = snapshots
        self.clock = clock
        self.started_ms = clock()
        self.camera_error: str | None = None
        self.frame: bytes | None = None
        self.frame_ms: int | None = None
        self.frame_size: tuple[int, int] | None = None
        self.frame_version = 0
        self.frame_event = asyncio.Event()
        self.frame_times: deque[int] = deque(maxlen=30)
        self.lit: bool | None = None
        self.level: float | None = None
        self.tracking: dict = {"stage": "waiting", "tracks": []}
        self.tracker: FishTracker | None = None
        self.roi_key = None
        self.roi = None
        self.recorder = Recorder(store)
        self.alerts = Alerts(store, self.save_snapshot)
        last_feed = store.latest_event("feed")
        self.last_feed_ms = last_feed["ts_ms"] if last_feed else None
        self.configure()

    def configure(self):
        """Apply the current settings (calibration, fish count, detection method)."""
        calibration = self.settings.value.calibration
        self.mapper = TankMapper(calibration) if calibration else None
        self.recorder.tank_size = self.mapper.size if self.mapper else None
        self.recorder.reset_motion()
        self.tracker = None
        self.roi_key = None

    # frame processing -------------------------------------------------------

    def analyze(self, jpeg: bytes) -> dict | None:
        """CPU-heavy part; runs in a worker thread, one frame at a time."""
        image = decode(jpeg)
        if image is None:
            return None
        height, width = image.shape[:2]
        mapper = self.mapper
        if self.roi_key != (width, height, id(mapper)):
            corners = mapper.scaled_corners(width, height) if mapper else None
            self.roi = water_mask(image.shape, corners)
            self.roi_key = (width, height, id(mapper))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        level = brightness(gray, self.roi)
        threshold = self.settings.value.dark_threshold + (DARK_HYSTERESIS if self.lit is False else 0)
        lit = level >= threshold
        if not lit:
            self.tracker = None
            tracking = {"stage": "dark", "tracks": []}
        else:
            if self.tracker is None:
                settings = self.settings.value
                self.tracker = FishTracker(settings.fish_count, settings.detection, width)
            tracking = self.tracker.process(image, gray, self.roi)
        for track in tracking["tracks"]:
            if mapper is not None:
                track["x_cm"], track["y_cm"] = mapper.to_cm(track["x_px"], track["y_px"], width, height)
        return {"width": width, "height": height, "level": level, "lit": lit, "tracking": tracking}

    def apply(self, ts_ms: int, jpeg: bytes, analysis: dict | None):
        if analysis is None:
            return
        self.frame, self.frame_ms = jpeg, ts_ms
        self.frame_size = (analysis["width"], analysis["height"])
        self.frame_version += 1
        self.frame_times.append(ts_ms)
        self.camera_error = None
        self.alerts.clear("camera_offline", ts_ms)
        self.lit, self.level = analysis["lit"], analysis["level"]
        tracking = analysis["tracking"]
        fish = [(track["id"], (track["x_cm"], track["y_cm"]) if "x_cm" in track else None)
                for track in tracking["tracks"]]
        closed = self.recorder.add(ts_ms, self.lit, fish)
        for track in tracking["tracks"]:
            track["speed_cm_s"] = self.recorder.last_speeds.get(track["id"])
        self.tracking = tracking
        self.after_minutes(closed)
        event, self.frame_event = self.frame_event, asyncio.Event()
        event.set()

    def process(self, jpeg: bytes, ts_ms: int | None = None):
        """Synchronous path, used by tests."""
        self.apply(self.clock() if ts_ms is None else ts_ms, jpeg, self.analyze(jpeg))

    def after_minutes(self, closed: list[int]):
        if closed:
            self.alerts.check_activity(closed[-1] + 60, self.settings.value)

    # background tasks -------------------------------------------------------

    async def run(self):
        await asyncio.gather(self.capture_loop(), self.watchdog_loop(), self.housekeeping_loop())

    async def capture_loop(self):
        delay = 1
        while True:
            try:
                async with self.source.frames() as frames:
                    while True:
                        jpeg = await asyncio.wait_for(anext(frames), FRAME_STALL_S)
                        analysis = await asyncio.to_thread(self.analyze, jpeg)
                        self.apply(self.clock(), jpeg, analysis)
                        delay = 1
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self.camera_error = f"相機 {FRAME_STALL_S} 秒沒有畫面，正在重新連線"
            except Exception as error:  # noqa: BLE001 - keep the monitor alive whatever the camera does
                logger.warning("Camera source failed: %s", error)
                self.camera_error = str(error) or type(error).__name__
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def watchdog_loop(self):
        while True:
            await asyncio.sleep(1)
            now = self.clock()
            self.alerts.check_camera(now, self.frame_ms, self.started_ms, self.settings.value.camera_timeout_s)
            if self.frame_ms is None or now - self.frame_ms > 2000:
                self.after_minutes(self.recorder.idle(now))

    async def housekeeping_loop(self):
        while True:
            self.store.prune_seconds(self.clock() // 1000 - KEEP_SECONDS_DAYS * 86_400)
            files = sorted(self.snapshots.glob("*.jpg"))
            for path in files[:-KEEP_SNAPSHOTS]:
                path.unlink(missing_ok=True)
            await asyncio.sleep(3600)

    def close(self):
        self.recorder.close()

    # actions ----------------------------------------------------------------

    def save_snapshot(self, label: str) -> str | None:
        if self.frame is None or self.frame_ms is None or self.clock() - self.frame_ms > LIVE_WITHIN_MS:
            return None
        self.snapshots.mkdir(parents=True, exist_ok=True)
        name = f"{self.frame_ms}-{label}.jpg"
        (self.snapshots / name).write_bytes(self.frame)
        return name

    async def feed(self):
        now = self.clock()
        if self.last_feed_ms is not None and now - self.last_feed_ms < FEED_DEBOUNCE_MS:
            raise ValueError("剛剛已記錄餵食，請稍候再試")
        await self.feeder.feed()
        self.store.add_event(now, "feed", {"origin": self.feeder.kind}, self.save_snapshot("feed"))
        self.last_feed_ms = now

    def feeding_response(self) -> dict | None:
        event = self.store.latest_event("feed")
        if event is None:
            return None
        window = self.settings.value.feed_window_min * 60
        fed = event["ts_ms"] // 1000
        now = self.clock() // 1000
        return {
            "ts_ms": event["ts_ms"], "snapshot": event["snapshot"], "window_min": window // 60,
            "elapsed_s": max(0, now - fed), "collecting": now < fed + window,
            "before": self.store.window(fed - window, fed),
            "after": self.store.window(fed, min(fed + window, now + 1)),
        }

    # state ------------------------------------------------------------------

    def camera_status(self, now: int) -> str:
        if self.frame_ms is not None and now - self.frame_ms <= LIVE_WITHIN_MS:
            return "live"
        return "connecting" if self.frame_ms is None and "camera_offline" not in self.alerts.active else "offline"

    def state(self) -> dict:
        now = self.clock()
        settings = self.settings.value
        times = self.frame_times
        fps = (len(times) - 1) * 1000 / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else None
        status = self.camera_status(now)
        demo = getattr(self.source, "demo_state", None)
        return {
            "now_ms": now,
            "source": self.source.name,
            "camera": {
                "status": status, "error": self.camera_error, "last_frame_ms": self.frame_ms,
                "fps": round(fps, 1) if fps and status == "live" else None,
                "width": self.frame_size[0] if self.frame_size else None,
                "height": self.frame_size[1] if self.frame_size else None,
            },
            "light": {
                "lit": self.lit if status == "live" else None,
                "brightness": round(self.level, 1) if self.level is not None else None,
                "threshold": settings.dark_threshold,
            },
            "tracking": {
                "stage": self.tracking["stage"] if status == "live" else "waiting",
                "expected": settings.fish_count,
                "fish": self.tracking["tracks"] if status == "live" else [],
            },
            "calibration": settings.calibration.model_dump() if settings.calibration else None,
            "alerts": sorted(self.alerts.active.values(), key=lambda alert: alert["since_ms"]),
            "feeder": {"kind": self.feeder.kind, "last_feed_ms": self.last_feed_ms},
            "demo": demo() if demo else None,
        }
