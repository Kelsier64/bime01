"""Computer vision shared by recorded and live fish tracking."""

from dataclasses import dataclass
from itertools import product
from math import dist

import cv2
import numpy as np


@dataclass
class Track:
    id: int
    point: tuple[float, float]
    frame: int
    velocity: tuple[float, float] = (0, 0)

    def predict(self, frame: int):
        delta = min(frame - self.frame, 2)
        return tuple(value + speed * delta for value, speed in zip(self.point, self.velocity))

    def update(self, point, frame):
        delta = frame - self.frame
        self.velocity = tuple((new - old) / delta for new, old in zip(point, self.point))
        self.point = point
        self.frame = frame


class Tracker:
    def __init__(self, count, max_gap, gate):
        self.count = count
        self.max_gap = max_gap
        self.gate = gate
        self.tracks = []
        self.next_id = 1

    def step(self, detections, frame):
        self.tracks = [track for track in self.tracks if frame - track.frame <= self.max_gap]
        candidates = []
        for track in self.tracks:
            prediction = track.predict(frame)
            gate = self.gate * min(2, 1 + 0.2 * (frame - track.frame - 1))
            options = [(-1, 0)]
            options.extend((index, distance) for index, point in enumerate(detections)
                           if (distance := dist(prediction, point)) <= gate)
            candidates.append(options)

        best_cost = (0, 0)
        used = set()
        assignment = ()
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
            if len(self.tracks) == self.count:
                break
            if index not in used:
                self.tracks.append(Track(self.next_id, point, frame))
                self.next_id += 1
        return self.tracks


def prepare(image, roi, size):
    x, y, width, height = roi
    crop = cv2.resize(image[y:y + height, x:x + width], size, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)


def find_fish(gray, background, threshold, min_area, max_area):
    difference = cv2.subtract(background, gray)
    _, mask = cv2.threshold(difference, threshold, 255, cv2.THRESH_BINARY)
    return blob_centers(cv2.medianBlur(mask, 3), min_area, max_area)


def blob_centers(mask, min_area, max_area):
    _, _, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [(int(area), tuple(center)) for area, center in zip(stats[1:, cv2.CC_STAT_AREA], centers[1:])
             if min_area <= area <= max_area]
    blobs.sort(reverse=True)
    return [point for _, point in blobs[:6]]


def find_gold_fish(image, min_area):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (4, 85, 45), (40, 255, 255))
    height, width = mask.shape
    margin_x, margin_y = width * 0.04, height * 0.04
    candidates = blob_centers(cv2.medianBlur(mask, 5), min_area, mask.size * 0.2)
    return [(x, y) for x, y in candidates if margin_x <= x < width - margin_x and margin_y <= y < height - margin_y]


class LiveFishTracker:
    """Build a background from a fixed 10 fps camera and report image-space tracks."""

    def __init__(self, fish_count: int, profile: str = "auto"):
        cv2.setNumThreads(1)
        self.fish_count = fish_count
        self.profile = profile
        self.reset()

    def reset(self):
        self.frame = 0
        self.samples = []
        self.background = None
        self.dimensions = None
        self.tracker = Tracker(self.fish_count, max_gap=5, gate=38)
        self.trails = {}

    def process(self, jpeg: bytes) -> dict:
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {"stage": "invalid_frame", "tracks": []}
        height, width = image.shape[:2]
        if self.dimensions != (width, height):
            self.reset()
            self.dimensions = (width, height)
        scaled_width = min(width, 560)
        size = (scaled_width, max(1, round(height * scaled_width / width)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        self.frame += 1
        result = {"width": width, "height": height, "stage": "learning", "tracks": []}
        detections = []
        if self.profile != "gold":
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if self.background is None:
                if self.frame % 4 == 0:
                    self.samples.append(gray)
                if len(self.samples) < 15:
                    return result
                self.background = np.median(self.samples, axis=0).astype(np.uint8)
                self.samples.clear()

        if self.profile != "contrast":
            min_gold_area = size[0] * size[1] * 0.004 if self.profile == "auto" else 35
            detections = find_gold_fish(image, min_gold_area)
        if not detections and self.profile != "gold":
            detections = find_fish(gray, self.background, threshold=13, min_area=22, max_area=550)
        active = self.tracker.step(detections, self.frame)
        trails = {}
        for track in active:
            if track.frame != self.frame:
                continue
            x, y = track.point
            point = [round(x * width / size[0], 1), round(y * height / size[1], 1)]
            trail = [*self.trails.get(track.id, [])[-9:], point]
            trails[track.id] = trail
            result["tracks"].append({"id": track.id, "x_px": point[0], "y_px": point[1], "trail": trail})
        self.trails = trails
        result["stage"] = "tracking"
        return result


