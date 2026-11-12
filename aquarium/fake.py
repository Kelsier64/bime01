"""A fake camera that renders goldfish frames, so the real pipeline runs without hardware.

Demo scenarios (lights off, sluggish fish, camera unplugged) change the rendered
frames only; detection, statistics and alerts all go through the normal path.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import atan2, cos, degrees, hypot, pi, sin
from random import Random

import cv2
import numpy as np

from .settings import Calibration, Settings
from .store import GRID, Store

WIDTH, HEIGHT = 640, 360
WATER = (48, 36, 592, 318)  # left, top, right, bottom in pixels
TANK_CM = (60.0, 31.0)
SCALE = ((WATER[2] - WATER[0]) / TANK_CM[0], (WATER[3] - WATER[1]) / TANK_CM[1])
OFFLINE_SECONDS = 45


def default_calibration() -> Calibration:
    left, top, right, bottom = WATER
    return Calibration(
        corners=((left, top), (right, top), (right, bottom), (left, bottom)),
        frame_width=WIDTH, frame_height=HEIGHT, tank_width_cm=TANK_CM[0], tank_height_cm=TANK_CM[1],
    )


def default_settings() -> Settings:
    return Settings(fish_count=2, calibration=default_calibration(), low_activity_window_min=5)


@dataclass
class Fish:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    target: tuple[float, float] = (30.0, 15.0)
    retarget_at: float = 0.0
    rest_until: float = 0.0
    cruise: float = 5.0
    delay: float = 0.0
    facing: int = 1


class FakeTank:
    name = "fake"

    def __init__(self, fish_count=2, seed=7, clock=time.monotonic):
        self.fish_count = fish_count
        self.seed = seed
        self.clock = clock
        self.background = self.render_background()
        self.reset()

    def reset(self):
        self.random = Random(self.seed)
        self.time = 0.0
        self.last_step = None
        self.dark = False
        self.sluggish = False
        self.offline_until = 0.0
        self.fed_at = None
        self.feed_x = 0.0
        self.fish = [
            Fish(x=self.random.uniform(10, 50), y=self.random.uniform(6, 22),
                 cruise=self.random.uniform(3.5, 6.5), delay=self.random.uniform(0, 2.5))
            for _ in range(self.fish_count)
        ]

    # scenario controls ------------------------------------------------------

    def demo(self, action: str):
        if action == "dark":
            self.dark = not self.dark
        elif action == "sluggish":
            self.sluggish = not self.sluggish
        elif action == "offline":
            self.offline_until = self.time + OFFLINE_SECONDS
        elif action == "reset":
            self.reset()
        else:
            raise ValueError(action)

    def demo_state(self):
        return {
            "dark": self.dark, "sluggish": self.sluggish,
            "offline_s": max(0, round(self.offline_until - self.time)),
        }

    def feed(self):
        self.fed_at = self.time
        self.feed_x = self.random.uniform(15, 45)

    # simulation -------------------------------------------------------------

    def step(self, dt: float):
        self.time += dt
        width, height = TANK_CM
        for fish in self.fish:
            since_feed = self.time - self.fed_at if self.fed_at is not None else None
            if since_feed is not None and 1.5 + fish.delay <= since_feed < 45:
                spread = (fish.delay - 1.25) * 8
                target, speed = (self.feed_x + spread + 3 * sin(self.time + fish.delay), height - 3), 9.0
            elif self.sluggish:
                target, speed = fish.target, 0.4
            elif fish.rest_until > self.time:
                target, speed = fish.target, 0.3
            else:
                reached = hypot(fish.target[0] - fish.x, fish.target[1] - fish.y) < 2
                if reached or self.time >= fish.retarget_at:
                    fish.target = (self.random.uniform(6, width - 6), self.random.uniform(4, height - 5))
                    fish.retarget_at = self.time + self.random.uniform(4, 10)
                    if self.random.random() < 0.2:
                        fish.rest_until = self.time + self.random.uniform(4, 12)
                target, speed = fish.target, fish.cruise
            dx, dy = target[0] - fish.x, target[1] - fish.y
            distance = hypot(dx, dy) or 1
            desired = (dx / distance * speed, dy / distance * speed)
            blend = min(1.0, dt * 1.5)
            fish.vx += (desired[0] - fish.vx) * blend
            fish.vy += (desired[1] - fish.vy) * blend
            fish.x = min(width - 4, max(4, fish.x + fish.vx * dt))
            fish.y = min(height - 2.5, max(2.5, fish.y + fish.vy * dt))
            if abs(fish.vx) > 0.3:
                fish.facing = 1 if fish.vx > 0 else -1

    def render_background(self):
        image = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
        image[:] = (70, 74, 78)
        left, top, right, bottom = WATER
        for row in range(top, bottom):
            shade = (row - top) / (bottom - top)
            image[row, left:right] = (214 - 22 * shade, 208 - 20 * shade, 196 - 24 * shade)
        cv2.rectangle(image, (left, bottom - 10), (right - 1, bottom - 1), (96, 112, 128), -1)
        cv2.line(image, (left, top + 2), (right - 1, top + 2), (228, 226, 220), 2)
        return image

    def render(self) -> bytes:
        image = self.background.copy()
        left, _, _, bottom = WATER
        for fish in self.fish:
            cx = left + fish.x * SCALE[0]
            cy = bottom - fish.y * SCALE[1]
            tilt = max(-25.0, min(25.0, degrees(atan2(-fish.vy, abs(fish.vx) + 0.5))))
            angle = tilt if fish.facing > 0 else 180 - tilt
            radians = angle * pi / 180
            back = (cx - cos(radians) * 30, cy - sin(radians) * 30)
            normal = (-sin(radians) * 13, cos(radians) * 13)
            tail_tip = (back[0] - cos(radians) * 16, back[1] - sin(radians) * 16)
            tail = np.array([back, (tail_tip[0] + normal[0], tail_tip[1] + normal[1]),
                             (tail_tip[0] - normal[0], tail_tip[1] - normal[1])], dtype=np.int32)
            cv2.fillPoly(image, [tail], (40, 150, 250), cv2.LINE_AA)
            cv2.ellipse(image, (round(cx), round(cy)), (30, 13), angle, 0, 360, (20, 125, 240), -1, cv2.LINE_AA)
            eye = (round(cx + cos(radians) * 19 - sin(radians) * 3), round(cy + sin(radians) * 19 + cos(radians) * 3))
            cv2.circle(image, eye, 3, (30, 30, 30), -1, cv2.LINE_AA)
        if self.dark:
            image = (image * 0.12).astype(np.uint8)
        _, encoded = cv2.imencode(".jpg", image, (cv2.IMWRITE_JPEG_QUALITY, 80))
        return encoded.tobytes()

    def next_frame(self) -> bytes | None:
        now = self.clock()
        dt = 0.1 if self.last_step is None else min(0.5, now - self.last_step)
        self.last_step = now
        self.step(dt)
        return None if self.time < self.offline_until else self.render()

    @asynccontextmanager
    async def frames(self):
        async def iterate():
            while True:
                await asyncio.sleep(0.1)
                frame = self.next_frame()
                if frame is not None:
                    yield frame
        yield iterate()


def seed_history(store: Store, now_s: int, fish_count: int, days: int = 3, seed: int = 11):
    """Give a fresh fake database a few days of plausible minute roll-ups."""
    if store.has_minutes():
        return
    random = Random(seed)
    width, height = TANK_CM
    rows = []
    start = (now_s - days * 86_400) // 60 * 60
    for minute in range(start, now_s // 60 * 60, 60):
        hour = time.localtime(minute).tm_hour + time.localtime(minute).tm_min / 60
        if not 7 <= hour < 22:
            rows.append((minute, 60, 0, 0, 0.0, 0, 0, 0.0, 0, None))
            continue
        # Matches what the pipeline measures on the fake tank (rests included), so a normal demo stays quiet.
        speed = max(0.5, 2.6 + 0.5 * sin((hour - 7) / 15 * 2 * pi) + random.gauss(0, 0.3))
        samples = 60 * fish_count
        heat = [0] * (GRID[0] * GRID[1])
        heights = []
        for _ in range(samples // 10):
            x = min(width - 0.01, max(0, random.gauss(width * 0.4, width * 0.22)))
            y = min(height - 0.01, max(0, random.gauss(height * 0.35, height * 0.2)))
            heights.append(y)
            row = min(GRID[1] - 1, int((height - y) / height * GRID[1]))
            heat[row * GRID[0] + min(GRID[0] - 1, int(x / width * GRID[0]))] += 10
        mean_height = sum(heights) / len(heights)
        rows.append((minute, 60, 60, samples, speed * samples, samples, round(samples * 0.75),
                     mean_height * samples, samples, json.dumps(heat)))
    with store.db:
        store.db.executemany("INSERT OR IGNORE INTO minutes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
