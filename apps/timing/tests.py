import datetime

import pytest
from asgiref.sync import async_to_sync

from apps.competitions.models import Competition, CompetitionType
from apps.participants.models import EventEntry, Participant

from .connectors import TimingPulse, get_connector
from .connectors.simulator import SimulatorConnector
from .models import TimingEvent
from .services import _persist_pulse

pytestmark = pytest.mark.django_db


def make_participant(ctype, bib, competition):
    participant = Participant.objects.create(
        competition_type=ctype, first_name="Bib", last_name=f"Holder{bib}",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number=str(bib), email=f"{bib}@x.com",
    )
    EventEntry.objects.create(participant=participant, competition=competition, bib_number=bib)
    return participant


def test_persist_pulse_resolves_participant_by_bib():
    ctype = CompetitionType.objects.create(name="Motorcycle")
    competition = Competition.objects.create(
        competition_type=ctype, name="Race", date=datetime.date(2026, 5, 1), is_active=True
    )
    participant = make_participant(ctype, 12, competition)

    pulse = TimingPulse(
        channel="finish", device_time=None, bib_number=12, raw={"simulated": True}
    )
    payload = async_to_sync(_persist_pulse)(pulse, "SimulatorConnector")

    assert payload["participant_name"] == str(participant)
    assert payload["channel"] == "finish"
    assert payload["channel_display"] == "Finish"
    assert TimingEvent.objects.filter(bib_number=12, participant=participant).exists()


def test_persist_pulse_without_matching_bib_stores_null_participant():
    pulse = TimingPulse(channel="start", device_time=None, bib_number=999)
    payload = async_to_sync(_persist_pulse)(pulse, "SimulatorConnector")
    assert payload["participant_name"] is None
    assert payload["channel_display"] == "Start"
    assert TimingEvent.objects.filter(bib_number=999, participant__isnull=True).exists()


def test_persist_pulse_with_no_bib():
    pulse = TimingPulse(channel="intermediate", device_time=None, bib_number=None)
    payload = async_to_sync(_persist_pulse)(pulse, "SimulatorConnector")
    assert payload["bib_number"] is None
    assert payload["participant_name"] is None


def test_timing_event_str_handles_missing_bib():
    event = TimingEvent.objects.create(channel="start", connector="Test")
    assert "?" in str(event)


def test_get_connector_returns_configured_class(settings):
    settings.TIMING_CONNECTOR = "apps.timing.connectors.simulator.SimulatorConnector"
    assert isinstance(get_connector(), SimulatorConnector)


def test_simulator_emits_pulses():
    connector = SimulatorConnector(min_interval=0.001, max_interval=0.002, bib_range=(1, 3))

    async def collect():
        await connector.connect()
        pulses = []
        async for pulse in connector.pulses():
            pulses.append(pulse)
            if len(pulses) >= 3:
                await connector.disconnect()
        return pulses

    pulses = async_to_sync(collect)()
    assert len(pulses) >= 3
    for pulse in pulses:
        assert pulse.channel in {"start", "finish"}
        assert 1 <= pulse.bib_number <= 3


def test_dashboard_view_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Live timing feed" in response.content
