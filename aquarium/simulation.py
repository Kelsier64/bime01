from dataclasses import dataclass
from math import dist, hypot, sin
from random import Random

from .contracts import (
    FEED_POINT,
    HEIGHT,
    WIDTH,
    DeviceCommand,
    FeedCommand,
    ObservationFrame,
    Position,
    Telemetry,
    Track,
)

TICK_SECONDS = 0.1
FEED_SECONDS = 14
AUTO_FEED_SECONDS = 180
BOUNDS = ((-WIDTH / 2 + 3, WIDTH / 2 - 3), (3, HEIGHT - 3))


@dataclass
class Fish:
    position: list[float]
    velocity: list[float]
    home: tuple[float, float]
    speed: float
    phase: float
    delay: float


class Simulation:
    def __init__(self):
        self.reset()

    def reset(self):
        random = Random(37)
        self.elapsed = 0.0
        self.fed_at = -FEED_SECONDS
        self.lost_until = 0.0
        self.low_clarity = False
        self.transmittance = 91.0
        self.light_on = True
        self.fish = [
            Fish(
                position=[random.uniform(-32, 32), random.uniform(10, 29)],
                velocity=[random.uniform(-7, 7), 0],
                home=(random.uniform(-30, 30), random.uniform(8, 19)),
                speed=random.uniform(8, 12),
                phase=random.uniform(0, 35),
                delay=random.uniform(0.3, 2.0),
            )
            for _ in range(8)
        ]

    async def execute(self, command: DeviceCommand) -> None:
        if isinstance(command, FeedCommand):
            self.fed_at = self.elapsed
        else:
            self.light_on = command.enabled

    def step(self, timestamp_ms: int) -> tuple[ObservationFrame, Telemetry]:
        self.elapsed += TICK_SECONDS
        positions = [fish.position.copy() for fish in self.fish]
        velocities = [fish.velocity.copy() for fish in self.fish]
        for index, fish in enumerate(self.fish):
            self.move(index, fish, positions, velocities)

        target = 48.0 if self.low_clarity else 91.0
        self.transmittance += (target - self.transmittance) * 0.08
        tracks = []
        for index, fish in enumerate(self.fish):
            track_id = f"ZF-{index + 1:02}"
            if index in (1, 4) and self.elapsed < self.lost_until:
                tracks.append(Track(id=track_id, status="lost"))
            else:
                tracks.append(Track(
                    id=track_id,
                    status="tracked",
                    position=Position(x=fish.position[0], y=fish.position[1]),
                ))

        telemetry = Telemetry(
            timestamp_ms=timestamp_ms,
            temperature_c=round(25.2 + 0.3 * sin(self.elapsed / 40), 2),
            transmittance_pct=round(self.transmittance, 1),
        )
        return ObservationFrame(timestamp_ms=timestamp_ms, tracks=tracks), telemetry

    def move(self, index, fish, positions, velocities):
        position = positions[index]
        phase = self.elapsed + fish.phase
        time_since_feed = self.elapsed - self.fed_at
        feeding = fish.delay <= time_since_feed < FEED_SECONDS
        resting = not feeding and phase % 36 > 28
        if feeding:
            goal = (
                FEED_POINT[0] + 7 * sin(phase * 1.15),
                FEED_POINT[1] - 2 + 2 * sin(phase * 0.6),
            )
            desired = [(goal[axis] - value) * 1.3 for axis, value in enumerate(position)]
            speed_limit = fish.speed * 2.0
        elif resting:
            desired = [(fish.home[axis] - value) * 0.45 for axis, value in enumerate(position)]
            speed_limit = fish.speed * 0.36
        else:
            desired = [
                fish.speed * sin(phase * 0.46),
                2.4 * sin(phase * 0.75) + (19 - position[1]) * 0.21,
            ]
            speed_limit = fish.speed

        for other_index, other in enumerate(positions):
            if index == other_index:
                continue
            distance = dist(position, other)
            if distance >= 23:
                continue
            for axis, value in enumerate(position):
                offset = other[axis] - value
                if distance < 5:
                    desired[axis] -= offset * (5 - distance) * 0.42
                if not feeding and not resting:
                    desired[axis] += offset * 0.037
                    desired[axis] += (velocities[other_index][axis] - fish.velocity[axis]) * 0.062

        for axis, (low, high) in enumerate(BOUNDS):
            desired[axis] += max(0, low + 6 - position[axis]) * 1.8
            desired[axis] -= max(0, position[axis] - high + 6) * 1.8

        scale = speed_limit / max(speed_limit, hypot(*desired))
        for axis, (low, high) in enumerate(BOUNDS):
            fish.velocity[axis] += (desired[axis] * scale - fish.velocity[axis]) * 0.22
            next_value = position[axis] + fish.velocity[axis] * TICK_SECONDS
            fish.position[axis] = min(high, max(low, next_value))
            if fish.position[axis] != next_value:
                fish.velocity[axis] = 0
