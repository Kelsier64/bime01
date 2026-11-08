from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

WIDTH, HEIGHT, DEPTH = 80.0, 40.0, 36.0
FEED_POINT = (12.0, 34.0, 0.0)
FEED_RADIUS = 15.0
MIN_TRANSMITTANCE = 60.0


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Position(Message):
    x: float = Field(ge=-WIDTH / 2, le=WIDTH / 2)
    y: float = Field(ge=0, le=HEIGHT)
    z: float = Field(ge=-DEPTH / 2, le=DEPTH / 2)

    def coordinates(self) -> tuple[float, float, float]:
        return self.x, self.y, self.z


class Track(Message):
    id: str = Field(min_length=1, max_length=64)
    status: Literal["tracked", "lost"]
    position: Position | None = None

    @model_validator(mode="after")
    def require_tracked_position(self):
        if self.status == "tracked" and self.position is None:
            raise ValueError("A tracked fish needs a position")
        return self


class ObservationFrame(Message):
    timestamp_ms: int = Field(gt=0)
    tracks: list[Track] = Field(max_length=128)

    @model_validator(mode="after")
    def require_unique_tracks(self):
        if len({track.id for track in self.tracks}) != len(self.tracks):
            raise ValueError("Track IDs must be unique within a frame")
        return self


class Telemetry(Message):
    timestamp_ms: int = Field(gt=0)
    temperature_c: float | None = Field(default=None, ge=0, le=100)
    transmittance_pct: float | None = Field(default=None, ge=0, le=100)


class FeedCommand(Message):
    id: str
    action: Literal["feed"] = "feed"


class LightCommand(Message):
    id: str
    action: Literal["light"] = "light"
    enabled: bool


DeviceCommand = Annotated[FeedCommand | LightCommand, Field(discriminator="action")]


class DeviceUnavailable(Exception):
    pass


class Actuator(Protocol):
    async def execute(self, command: DeviceCommand) -> None:
        """Return after confirmation, or raise DeviceUnavailable."""
