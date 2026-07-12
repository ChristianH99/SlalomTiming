import logging

from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer

from apps.competitions.models import Competition
from apps.participants.models import EventEntry

from .connectors import TimingPulse, get_connector
from .models import TimingEvent

logger = logging.getLogger(__name__)

TIMING_GROUP = "timing_updates"


@sync_to_async
def _persist_pulse(pulse: TimingPulse, connector_name: str) -> dict:
    participant = None
    if pulse.bib_number is not None:
        competition = Competition.get_current()
        if competition is not None:
            entry = (
                EventEntry.objects.select_related("participant")
                .filter(competition=competition, bib_number=pulse.bib_number)
                .first()
            )
            participant = entry.participant if entry else None

    event = TimingEvent.objects.create(
        channel=pulse.channel,
        bib_number=pulse.bib_number,
        device_time=pulse.device_time,
        connector=connector_name,
        raw_payload=pulse.raw,
        participant=participant,
    )
    return {
        "id": event.id,
        "channel": event.channel,
        "bib_number": event.bib_number,
        "participant_name": str(participant) if participant else None,
        "device_time": event.device_time.isoformat() if event.device_time else None,
        "received_at": event.received_at.isoformat(),
    }


async def run_ingestion() -> None:
    """Connect to the configured timing device and stream events forever.

    Every pulse is written to the database before being broadcast, so a
    dropped websocket or a crashed dashboard never loses timing data.
    """
    connector = get_connector()
    connector_name = type(connector).__name__
    channel_layer = get_channel_layer()

    await connector.connect()
    logger.info("Timing connector %s connected", connector_name)
    try:
        async for pulse in connector.pulses():
            payload = await _persist_pulse(pulse, connector_name)
            if channel_layer is not None:
                await channel_layer.group_send(
                    TIMING_GROUP,
                    {"type": "timing.event", "event": payload},
                )
    finally:
        await connector.disconnect()
        logger.info("Timing connector %s disconnected", connector_name)
