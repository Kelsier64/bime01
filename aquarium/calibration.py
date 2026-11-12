"""Perspective mapping from camera pixels to tank centimetres."""

import numpy as np

from .settings import Calibration


def homography(source, target) -> np.ndarray:
    rows, values = [], []
    for (x, y), (u, v) in zip(source, target):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.extend((u, v))
    solution = np.linalg.solve(np.array(rows, dtype=float), np.array(values, dtype=float))
    return np.append(solution, 1).reshape(3, 3)


class TankMapper:
    def __init__(self, calibration: Calibration):
        self.calibration = calibration
        width, height = calibration.tank_width_cm, calibration.tank_height_cm
        self.size = (width, height)
        self.matrix = homography(calibration.corners, [(0, height), (width, height), (width, 0), (0, 0)])

    def scaled_corners(self, frame_width: int, frame_height: int) -> np.ndarray:
        scale = np.array([frame_width / self.calibration.frame_width, frame_height / self.calibration.frame_height])
        return np.array(self.calibration.corners, dtype=float) * scale

    def to_cm(self, x_px: float, y_px: float, frame_width: int, frame_height: int) -> tuple[float, float]:
        x = x_px * self.calibration.frame_width / frame_width
        y = y_px * self.calibration.frame_height / frame_height
        u, v, w = self.matrix @ (x, y, 1)
        width, height = self.size
        return min(width, max(0.0, u / w)), min(height, max(0.0, v / w))
