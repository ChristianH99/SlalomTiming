from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TimingPulse:
    """One raw event received from a timing device."""

    channel: str
    device_time: datetime | None
    bib_number: int | None = None
    raw: dict | None = None


class TimingDeviceConnector(ABC):
    """Interface every timing device backend must implement.

    The rest of the system (ingestion service, live dashboard) only ever
    depends on this interface, never on a concrete backend, so the real
    device driver and the simulator are drop-in replacements for each
    other via the TIMING_CONNECTOR setting.
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def pulses(self) -> AsyncIterator[TimingPulse]:
        """Yield timing pulses as they arrive, until disconnected."""
        ...
