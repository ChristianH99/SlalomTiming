import asyncio
import random
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from .base import TimingDeviceConnector, TimingPulse


class SimulatorConnector(TimingDeviceConnector):
    """Fakes a timing device by emitting random start/finish pulses.

    Lets the ingestion pipeline and live dashboard be built and tested
    without any real timing hardware connected.
    """

    def __init__(
        self,
        *,
        min_interval: float = 1.5,
        max_interval: float = 4.0,
        bib_range: tuple[int, int] = (1, 120),
    ):
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._bib_range = bib_range
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def pulses(self) -> AsyncIterator[TimingPulse]:
        channels = ["start", "finish"]
        while self._connected:
            await asyncio.sleep(random.uniform(self._min_interval, self._max_interval))
            if not self._connected:
                break
            yield TimingPulse(
                channel=random.choice(channels),
                device_time=datetime.now(timezone.utc),
                bib_number=random.randint(*self._bib_range),
                raw={"simulated": True},
            )
