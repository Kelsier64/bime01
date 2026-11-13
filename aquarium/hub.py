"""Which frame source is active, and switching between them at runtime.

Each source keeps its own data directory (database, settings with its own
calibration, snapshots), so video tests never mix with real tank history.
"""

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .devices import Feeder, ManualFeeder
from .monitor import Monitor
from .settings import Settings, SettingsStore
from .sources import FrameSource, PiCamera, VideoFile, find_videos
from .store import Store

logger = logging.getLogger(__name__)


@dataclass
class SourceOption:
    id: str
    label: str
    kind: Literal["camera", "video", "fake"]
    build: Callable[[], FrameSource]
    defaults: Callable[[], Settings] = Settings
    seed: Callable[[Store, int, Settings], None] | None = None
    note: str | None = None
    extra: dict = field(default_factory=dict)

    def describe(self, active: bool) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind, "note": self.note, "active": active}


def camera_available() -> bool:
    return shutil.which("rpicam-vid") is not None


def video_option(path: Path) -> SourceOption:
    """A video file; an optional `<name>.json` next to it sets label, note and default settings."""
    meta = {}
    sidecar = path.with_suffix(".json")
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("Ignoring %s: %s", sidecar, error)
    defaults = meta.get("settings", {})
    return SourceOption(
        id=f"video:{path.stem}", label=meta.get("label", f"影片：{path.name}"), kind="video",
        build=lambda: VideoFile(path), defaults=lambda: Settings.model_validate(defaults), note=meta.get("note"),
    )


def fake_option() -> SourceOption:
    from .fake import FakeTank, default_settings, seed_history

    return SourceOption(
        id="fake", label="假相機（模擬金魚）", kind="fake", build=FakeTank, defaults=default_settings,
        seed=lambda store, now_s, settings: seed_history(store, now_s, settings.fish_count),
        note="程式繪製的魚缸畫面，附 3 天示範歷史",
    )


class Catalog:
    def __init__(self, videos: Path, camera: bool | None = None, rpicam_args: str = "", fake: bool = True):
        self.videos = videos
        self.camera = camera_available() if camera is None else camera
        self.rpicam_args = rpicam_args
        self.fake = fake

    def options(self) -> list[SourceOption]:
        options = []
        if self.camera:
            options.append(SourceOption(
                id="picamera", label="Pi Camera 3（實機）", kind="camera",
                build=lambda: PiCamera(extra_args=self.rpicam_args),
            ))
        options.extend(video_option(path) for path in find_videos(self.videos))
        if self.fake:
            options.append(fake_option())
        return options


def directory_name(source_id: str) -> str:
    return re.sub(r"[^\w.-]", "-", source_id)


class Hub:
    def __init__(self, catalog: Catalog, data_root: Path, feeder: Feeder | None = None, start_monitor=True):
        self.catalog = catalog
        self.data_root = data_root
        self.feeder = feeder
        self.start_monitor = start_monitor
        self.monitor: Monitor | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.active_file = data_root / "active-source.txt"

    def sources(self) -> list[dict]:
        active = self.monitor.source.name if self.monitor else None
        return [option.describe(option.id == active) for option in self.catalog.options()]

    def initial(self, preferred: str | None) -> str:
        ids = [option.id for option in self.catalog.options()]
        with suppress(OSError):
            saved = self.active_file.read_text(encoding="utf-8").strip()
            if saved in ids:
                return saved
        if preferred in ids:
            return preferred
        if not ids:
            raise RuntimeError("沒有可用的影像來源：請接上相機或放入影片")
        return ids[0]

    async def activate(self, source_id: str):
        async with self.lock:
            option = next((item for item in self.catalog.options() if item.id == source_id), None)
            if option is None:
                raise KeyError(source_id)
            if self.monitor is not None and self.monitor.source.name == source_id:
                return
            await self.stop()
            data_dir = self.data_root / "sources" / directory_name(source_id)
            store = Store(data_dir / "aquarium.sqlite3")
            settings = SettingsStore(data_dir / "settings.json", option.defaults())
            source = option.build()
            feeder = self.feeder or ManualFeeder(getattr(source, "feed", None))
            monitor = Monitor(source, store, settings, feeder, data_dir / "snapshots")
            if option.seed is not None:
                option.seed(store, monitor.clock() // 1000, settings.value)
            self.monitor = monitor
            if self.start_monitor:
                self.task = asyncio.create_task(monitor.run())
            self.data_root.mkdir(parents=True, exist_ok=True)
            self.active_file.write_text(source_id, encoding="utf-8")
            logger.info("Active source: %s", source_id)

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.monitor is not None:
            self.monitor.close()
            self.monitor.store.close()
            self.monitor = None
