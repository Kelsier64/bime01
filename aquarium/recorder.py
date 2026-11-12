"""Turns per-frame tracking results into per-second rows and per-minute roll-ups."""

from math import dist

from .store import GRID, MINUTE_FIELDS, Store

MOVING_CM_S = 1.0
MAX_SPEED_GAP_S = 2


class Recorder:
    def __init__(self, store: Store):
        self.store = store
        self.tank_size: tuple[float, float] | None = None
        self.second: int | None = None
        self.frames = 0
        self.dark_frames = 0
        self.seen: dict[int, tuple[float, float] | None] = {}
        self.previous_second: int | None = None
        self.previous_positions: dict[int, tuple[float, float]] = {}
        self.minute: int | None = None
        self.totals = dict.fromkeys(MINUTE_FIELDS, 0)
        self.heat = [0] * (GRID[0] * GRID[1])
        self.last_speeds: dict[int, float] = {}

    def add(self, ts_ms: int, lit: bool, fish: list[tuple[int, tuple[float, float] | None]]) -> list[int]:
        """Record one processed frame. Returns the start (unix s) of any minutes that just closed."""
        second = ts_ms // 1000
        closed = []
        if self.second is not None and second != self.second:
            closed = self.finish_second()
        self.second = second
        self.frames += 1
        if lit:
            for track_id, position in fish:
                self.seen[track_id] = position
        else:
            self.dark_frames += 1
        return closed

    def finish_second(self) -> list[int]:
        second = self.second
        dark = self.dark_frames * 2 > self.frames
        positions = {track_id: position for track_id, position in self.seen.items() if position is not None}
        calibrated = self.tank_size is not None
        speeds = {}
        if dark:
            status, fish = "dark", 0
        else:
            status, fish = ("lit" if calibrated else "uncalibrated"), len(self.seen)
            gap = second - self.previous_second if self.previous_second is not None else None
            if calibrated and gap is not None and 0 < gap <= MAX_SPEED_GAP_S:
                for track_id, position in positions.items():
                    if track_id in self.previous_positions:
                        speeds[track_id] = dist(position, self.previous_positions[track_id]) / gap

        heights = [y for _, y in positions.values()] if calibrated and not dark else []
        moving = sum(speed > MOVING_CM_S for speed in speeds.values())
        self.store.insert_second(
            second, status, fish,
            sum(speeds.values()) / len(speeds) if speeds else None,
            moving / len(speeds) if speeds else None,
            sum(heights) / len(heights) if heights else None,
            [[round(x, 2), round(y, 2)] for x, y in positions.values()] if calibrated and not dark else None,
        )
        self.last_speeds = speeds

        closed = []
        minute = second // 60 * 60
        if self.minute is not None and minute != self.minute:
            closed.append(self.flush_minute())
        self.minute = minute
        totals = self.totals
        totals["seconds"] += 1
        if not dark:
            totals["lit"] += 1
            totals["fish_seconds"] += fish
            totals["speed_sum"] += sum(speeds.values())
            totals["speed_n"] += len(speeds)
            totals["moving_n"] += moving
            totals["height_sum"] += sum(heights)
            totals["height_n"] += len(heights)
            if calibrated:
                width, height = self.tank_size
                for x, y in positions.values():
                    column = min(GRID[0] - 1, int(x / width * GRID[0]))
                    row = min(GRID[1] - 1, int((height - y) / height * GRID[1]))
                    self.heat[row * GRID[0] + column] += 1

        if dark:
            self.previous_second, self.previous_positions = None, {}
        else:
            self.previous_second, self.previous_positions = second, positions
        self.frames = self.dark_frames = 0
        self.seen = {}
        return closed

    def flush_minute(self) -> int:
        minute = self.minute
        self.store.save_minute(minute, self.totals, self.heat if any(self.heat) else None)
        self.totals = dict.fromkeys(MINUTE_FIELDS, 0)
        self.heat = [0] * (GRID[0] * GRID[1])
        return minute

    def idle(self, now_ms: int) -> list[int]:
        """Called while no frames arrive, so the last second and minute still get saved."""
        closed = []
        if self.second is not None and now_ms // 1000 > self.second + 1:
            closed = self.finish_second()
            self.second = None
        if self.second is None and self.minute is not None and now_ms // 60_000 * 60 > self.minute:
            closed.append(self.flush_minute())
            self.minute = None
        return closed

    def close(self):
        """Persist partial data on shutdown."""
        if self.second is not None:
            self.finish_second()
            self.second = None
        if self.minute is not None:
            self.flush_minute()
            self.minute = None

    def reset_motion(self):
        """Forget positions so a calibration change cannot create a fake speed spike."""
        self.previous_second, self.previous_positions = None, {}
        self.seen = {}
