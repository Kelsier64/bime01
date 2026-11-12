"""Device interfaces for hardware that is not connected yet.

Only the feeder is wired into the app today. Without a feeder the dashboard's
feed button logs a manual feeding so the before/after comparison still works.
To add real hardware, implement the protocol and pass it to `create_app`.
"""

from collections.abc import Callable
from typing import Literal, Protocol


class DeviceError(Exception):
    """Raise when the device cannot confirm the command."""


class Feeder(Protocol):
    kind: Literal["manual", "device"]

    async def feed(self) -> None:
        """Dispense food and return after the device confirms, or raise DeviceError."""


class Light(Protocol):
    async def set(self, on: bool) -> None:
        """Switch the tank light, or raise DeviceError. (Not wired into the app yet.)"""


class Thermometer(Protocol):
    async def read(self) -> float:
        """Water temperature in °C, or raise DeviceError. (Not wired into the app yet.)"""


class ManualFeeder:
    """The keeper feeds by hand and presses the button; nothing is actuated."""

    kind = "manual"

    def __init__(self, on_feed: Callable[[], None] | None = None):
        self.on_feed = on_feed

    async def feed(self) -> None:
        if self.on_feed is not None:
            self.on_feed()
