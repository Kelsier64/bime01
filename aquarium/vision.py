"""Fish detection and multi-target tracking on single front-view frames.

Runs inside a worker thread. Coordinates are in decoded-frame pixels.
"""

from dataclasses import dataclass, field
from itertools import product
from math import dist

import cv2
import numpy as np

GOLD_LOW = (4, 85, 45)
GOLD_HIGH = (40, 255, 255)
TRAIL_POINTS = 20


def decode(jpeg: bytes):
    return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)


def water_mask(shape, corners) -> np.ndarray | None:
    if corners is None:
        return None
    mask = np.zeros(shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [np.round(corners).astype(np.int32)], 255)
    return mask


def brightness(gray: np.ndarray, mask: np.ndarray | None) -> float:
    return float(cv2.mean(gray, mask=mask)[0])


def blob_centers(mask: np.ndarray, min_area: float, max_area: float, limit: int = 6):
    _, _, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [(int(area), (float(x), float(y))) for area, (x, y) in zip(stats[1:, cv2.CC_STAT_AREA], centers[1:])
             if min_area <= area <= max_area]
    blobs.sort(reverse=True)
    return [point for _, point in blobs[:limit]]


def detect_color(image: np.ndarray, roi: np.ndarray | None) -> list[tuple[float, float]]:
    """Orange/gold fish against a pale background."""
    mask = cv2.inRange(cv2.cvtColor(image, cv2.COLOR_BGR2HSV), GOLD_LOW, GOLD_HIGH)
    if roi is not None:
        mask = cv2.bitwise_and(mask, roi)
    area = mask.size
    return blob_centers(cv2.medianBlur(mask, 5), area * 0.0008, area * 0.25)


class BackgroundDetector:
    """Fish darker than a learned static background (for non-gold fish or IR light)."""

    samples_needed = 15

    def __init__(self):
        self.samples = []
        self.background = None
        self.count = 0

    @property
    def ready(self):
        return self.background is not None

    def detect(self, gray: np.ndarray, roi: np.ndarray | None) -> list[tuple[float, float]]:
        if self.background is None or self.background.shape != gray.shape:
            self.count += 1
            if self.count % 4 == 0:
                self.samples.append(gray)
            if len(self.samples) >= self.samples_needed:
                self.background = np.median(self.samples, axis=0).astype(np.uint8)
                self.samples.clear()
            return []
        _, mask = cv2.threshold(cv2.subtract(self.background, gray), 13, 255, cv2.THRESH_BINARY)
        if roi is not None:
            mask = cv2.bitwise_and(mask, roi)
        area = mask.size
        return blob_centers(cv2.medianBlur(mask, 3), area * 0.0001, area * 0.05)


@dataclass
class Track:
    id: int
    point: tuple[float, float]
    frame: int
    velocity: tuple[float, float] = (0.0, 0.0)
    trail: list = field(default_factory=list)

    def predict(self, frame: int):
        delta = min(frame - self.frame, 2)
        return tuple(value + speed * delta for value, speed in zip(self.point, self.velocity))

    def update(self, point, frame):
        delta = frame - self.frame
        self.velocity = tuple((new - old) / delta for new, old in zip(point, self.point))
        self.point = point
        self.frame = frame
        self.trail = [*self.trail[-(TRAIL_POINTS - 1):], point]


class Tracker:
    """Nearest-neighbour assignment with constant-velocity prediction for a handful of fish."""

    def __init__(self, count: int, max_gap: int, gate: float):
        self.count = count
        self.max_gap = max_gap
        self.gate = gate
        self.tracks: list[Track] = []
        self.next_id = 1

    def step(self, detections, frame: int) -> list[Track]:
        self.tracks = [track for track in self.tracks if frame - track.frame <= self.max_gap]
        candidates = []
        for track in self.tracks:
            prediction = track.predict(frame)
            gate = self.gate * min(2, 1 + 0.2 * (frame - track.frame - 1))
            options = [(-1, 0.0)]
            options.extend((index, distance) for index, point in enumerate(detections)
                           if (distance := dist(prediction, point)) <= gate)
            candidates.append(options)

        best_cost, used, assignment = (0, 0.0), set(), ()
        for choices in product(*candidates):
            assigned = [index for index, _ in choices if index >= 0]
            if len(set(assigned)) != len(assigned):
                continue
            cost = (-len(assigned), sum(distance for _, distance in choices))
            if cost < best_cost:
                best_cost, used, assignment = cost, set(assigned), choices

        for track, (index, _) in zip(self.tracks, assignment):
            if index >= 0:
                track.update(detections[index], frame)

        for index, point in enumerate(detections):
            if len(self.tracks) >= self.count:
                break
            if index not in used:
                self.tracks.append(Track(self.next_id, point, frame, trail=[point]))
                self.next_id += 1
        return self.tracks


class FishTracker:
    def __init__(self, fish_count: int, method: str, frame_width: int = 640):
        self.method = method
        self.frame = 0
        self.tracker = Tracker(fish_count, max_gap=20, gate=frame_width * 0.08)
        self.background = BackgroundDetector() if method == "background" else None

    def process(self, image: np.ndarray, gray: np.ndarray, roi: np.ndarray | None) -> dict:
        self.frame += 1
        if self.background is not None:
            detections = self.background.detect(gray, roi)
            if not self.background.ready:
                return {"stage": "learning", "tracks": []}
        else:
            detections = detect_color(image, roi)
        active = self.tracker.step(detections, self.frame)
        tracks = [
            {"id": track.id, "x_px": track.point[0], "y_px": track.point[1], "trail": track.trail}
            for track in active if track.frame == self.frame
        ]
        return {"stage": "tracking", "tracks": tracks}
