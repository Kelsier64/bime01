"""User-editable settings, persisted as JSON next to the database."""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Point = tuple[float, float]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Calibration(Model):
    """Maps the water area seen by the front camera to tank centimetres.

    `corners` are image pixels in the order top-left, top-right, bottom-right,
    bottom-left of the water area. Tank coordinates put x = 0 at the left wall
    and y = 0 at the bottom, so y is the fish's height above the floor.
    """

    corners: tuple[Point, Point, Point, Point]
    frame_width: int = Field(gt=0, le=10_000)
    frame_height: int = Field(gt=0, le=10_000)
    tank_width_cm: float = Field(gt=0, le=500)
    tank_height_cm: float = Field(gt=0, le=300)

    @model_validator(mode="after")
    def check_corners(self):
        for x, y in self.corners:
            if not (0 <= x <= self.frame_width and 0 <= y <= self.frame_height):
                raise ValueError("Corners must lie inside the camera frame")
        for index in range(4):
            (ax, ay), (bx, by), (cx, cy) = (self.corners[(index + offset) % 4] for offset in range(3))
            if (bx - ax) * (cy - by) - (by - ay) * (cx - bx) <= 0:
                raise ValueError("Corners must form a convex area in the order top-left, top-right, bottom-right, bottom-left")
        return self


class Settings(Model):
    fish_count: int = Field(default=1, ge=1, le=4)
    detection: Literal["color", "background"] = "color"
    dark_threshold: int = Field(default=40, ge=0, le=250, description="Mean brightness (0-255) below which the tank counts as dark")
    calibration: Calibration | None = None
    camera_timeout_s: int = Field(default=30, ge=5, le=3600)
    low_activity_window_min: int = Field(default=60, ge=5, le=1440)
    low_activity_ratio: float = Field(default=0.5, gt=0, lt=1)
    baseline_days: int = Field(default=3, ge=1, le=30)
    feed_window_min: int = Field(default=5, ge=1, le=60)


class SettingsStore:
    def __init__(self, path: Path, defaults: Settings):
        self.path = path
        try:
            self.value = Settings.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.value = defaults

    def save(self, settings: Settings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(settings.model_dump(mode="json"), file, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)
        self.value = settings
