import datetime
import json
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.urls import reverse

from apps.competitions.models import Competition, CompetitionClass, CompetitionType
from apps.participants.models import ClassAssignment, EventEntry, Participant

from . import arrangement, calc
from .connectors import TimingPulse, get_connector
from .connectors.simulator import SimulatorConnector
from .models import TimedRun, TimingEvent, TimingSettings, TimingSignal
from .services import _persist_pulse
from .views import serialize_arrangement

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


# ----- timing settings -----

def test_timing_settings_is_a_singleton():
    a = TimingSettings.load()
    a.start_channel = 5
    a.save()
    b = TimingSettings.load()
    assert b.pk == 1 and b.start_channel == 5
    assert TimingSettings.objects.count() == 1


def test_settings_page_saves_simulator(client):
    response = client.post(reverse("timing:settings"), {
        "device": "simulator", "start_channel": "1", "finish_channel": "2", "ip_address": "",
    })
    assert response.status_code == 302
    settings = TimingSettings.load()
    assert settings.device == "simulator"
    assert settings.ip_address is None


def test_settings_requires_ip_for_tp540(client):
    response = client.post(reverse("timing:settings"), {
        "device": "tp540", "start_channel": "1", "finish_channel": "2", "ip_address": "",
    })
    assert response.status_code == 200
    assert "ip_address" in response.context["form"].errors


def test_settings_clears_ip_when_not_tp540(client):
    TimingSettings.objects.create(device="tp540", ip_address="192.168.0.9")
    client.post(reverse("timing:settings"), {
        "device": "simulator", "start_channel": "1", "finish_channel": "2", "ip_address": "192.168.0.9",
    })
    assert TimingSettings.load().ip_address is None


def test_channel_rejects_two_digits(client):
    response = client.post(reverse("timing:settings"), {
        "device": "simulator", "start_channel": "10", "finish_channel": "2", "ip_address": "",
    })
    assert response.status_code == 200
    assert "start_channel" in response.context["form"].errors


# ----- simulator + signal endpoint -----

def test_simulator_page_renders_standalone(client):
    response = client.get(reverse("timing:simulator"))
    assert response.status_code == 200
    body = response.content.decode()
    # Standalone: no app sidebar, but the 2x4 pad and clock are present.
    assert "shell-sidebar" not in body
    assert body.count('class="sim-btn') == 8
    assert 'id="sim-clock"' in body


def _post_signal(client, **payload):
    return client.post(
        reverse("timing:signal"), data=json.dumps(payload), content_type="application/json"
    )


def test_signal_endpoint_records_barrier_signal(client):
    response = _post_signal(client, running_number=1, port=2, is_manual=False, time="14:03:22.481")
    assert response.status_code == 200 and response.json()["ok"] is True
    signal = TimingSignal.objects.get()
    assert (signal.running_number, signal.port, signal.is_manual) == (1, 2, False)
    assert signal.device_time == datetime.time(14, 3, 22, 481000)


def test_signal_endpoint_records_manual_as_same_port(client):
    _post_signal(client, running_number=7, port=3, is_manual=True, time="09:00:00.000")
    signal = TimingSignal.objects.get()
    assert signal.port == 3 and signal.is_manual is True


@pytest.mark.parametrize("payload", [
    {"running_number": 1, "port": 5, "time": "14:03:22.481"},   # port out of range
    {"running_number": 0, "port": 1, "time": "14:03:22.481"},   # running number < 1
    {"running_number": 1, "port": 1, "time": "not-a-time"},     # bad time
    {"running_number": 1, "port": 1},                            # missing time
])
def test_signal_endpoint_rejects_bad_input(client, payload):
    response = _post_signal(client, **payload)
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert not TimingSignal.objects.exists()


# ----- live timing view: calc, causal pairing, endpoints -----

def _t(text):
    return datetime.datetime.strptime(text, "%H:%M:%S.%f").time()


def make_active_competition(**type_kwargs):
    # 1/1000 precision by default so device times keep their milliseconds in tests.
    type_kwargs.setdefault("timing_precision", CompetitionType.Precision.THOUSANDTHS)
    ctype = CompetitionType.objects.create(name="Moto", **type_kwargs)
    return Competition.objects.create(
        competition_type=ctype, name="Race", date=datetime.date(2026, 5, 1), is_active=True
    )


def signal_in(competition, port, time_str, running=1):
    """Create a signal and place it into the arrangement, like ingestion does."""
    sig = TimingSignal.objects.create(
        competition=competition, running_number=running, port=port, device_time=_t(time_str)
    )
    arrangement.ingest(sig, TimingSettings.load())
    return sig


def run_of(start_signal):
    return TimedRun.objects.get(start_signal=start_signal)


def post_json(client, name, **body):
    return client.post(reverse(name), data=json.dumps(body), content_type="application/json")


def test_run_time_truncates_not_rounds():
    assert calc.run_time(_t("00:00:10.000000"), _t("00:00:22.345900"), 2) == Decimal("12.34")
    assert calc.run_time(_t("00:00:10.000000"), _t("00:00:22.345900"), 1) == Decimal("12.3")
    assert calc.run_time(_t("00:00:10.000000"), _t("00:00:22.345900"), 3) == Decimal("12.345")


def test_run_time_missing_or_negative_is_none():
    assert calc.run_time(None, _t("00:00:10.000000"), 2) is None
    assert calc.run_time(_t("00:00:22.000000"), _t("00:00:10.000000"), 2) is None


def test_total_penalty_only_when_enabled():
    on = CompetitionType(penalties_enabled=True, pylon_penalty=5, task_penalty=10, stop_line_penalty=3)
    run = TimedRun(pylon_count=2, task_count=1, stopline_count=0)
    assert calc.total_penalty(run, on) == 20
    off = CompetitionType(penalties_enabled=False, pylon_penalty=5)
    assert calc.total_penalty(run, off) == 0


def test_finish_pairs_with_oldest_open_start_newest_row_on_top():
    comp = make_active_competition()  # channels default start=1, finish=2
    signal_in(comp, 1, "10:00:00.000", running=1)  # start A
    signal_in(comp, 1, "10:00:05.000", running=2)  # start B
    signal_in(comp, 2, "10:00:12.500", running=3)  # finish → closes A (oldest open)
    rows = serialize_arrangement(comp)["rows"]
    # Newest first: start B (still open) on top, then A with its finish.
    assert rows[0]["start"]["time"] == "10:00:05.000" and rows[0]["finish"] is None
    assert rows[1]["start"]["time"] == "10:00:00.000"
    assert rows[1]["finish"]["time"] == "10:00:12.500"
    assert rows[1]["run_time"] == "12.500"


def test_start_does_not_adopt_earlier_orphan_finish():
    comp = make_active_competition()
    signal_in(comp, 2, "10:00:00.000", running=1)  # finish before any start → orphan
    signal_in(comp, 1, "10:00:05.000", running=2)  # start after it
    rows = serialize_arrangement(comp)["rows"]
    assert rows[0]["start"]["time"] == "10:00:05.000" and rows[0]["finish"] is None
    assert rows[1]["start"] is None and rows[1]["finish"]["time"] == "10:00:00.000"


def test_ignore_removes_time_from_its_run(client):
    comp = make_active_competition()
    start = signal_in(comp, 1, "10:00:00.000", running=1)
    finish = signal_in(comp, 2, "10:00:10.000", running=2)  # pairs with start
    post_json(client, "timing:ignore", signal_id=finish.id, ignored=True)
    data = serialize_arrangement(comp)
    assert data["rows"][0]["finish"] is None  # detached from the run
    assert [ig["id"] for ig in data["ignored"]] == [finish.id]
    run_of(start).refresh_from_db()


def test_pair_endpoint_joins_orphans(client):
    comp = make_active_competition()
    finish = signal_in(comp, 2, "10:00:12.000", running=1)  # orphan finish
    start = signal_in(comp, 1, "10:00:00.000", running=2)   # separate open start
    run = run_of(start)
    resp = post_json(client, "timing:pair", signal_id=finish.id, run_id=run.id, slot="finish").json()
    assert resp["ok"] is True
    run.refresh_from_db()
    assert run.finish_signal_id == finish.id


def test_pair_endpoint_rejects_start_after_finish(client):
    comp = make_active_competition()
    finish = signal_in(comp, 2, "10:00:00.000", running=1)  # early finish (orphan row)
    start = signal_in(comp, 1, "10:00:05.000", running=2)   # later start
    run = TimedRun.objects.get(finish_signal=finish)
    resp = post_json(client, "timing:pair", signal_id=start.id, run_id=run.id, slot="start").json()
    assert resp["ok"] is False and resp["rejected"] is True
    run.refresh_from_db()
    assert run.start_signal_id is None  # unchanged


def test_run_update_resolves_name_and_defaults_class(client):
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True)
    cclass = comp.classes.get(name="1")
    participant = Participant.objects.create(
        competition_type=comp.competition_type, first_name="Ada", last_name="Lovelace",
        date_of_birth=datetime.date(2010, 1, 1), license_number="L1",
    )
    EventEntry.objects.create(participant=participant, competition=comp, bib_number=7)
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    resp = post_json(client, "timing:run-update", run_id=run.id, bib_number="7").json()
    assert resp["ok"] and resp["row"]["run"]["name"] == "Ada Lovelace"
    assert resp["row"]["run"]["class_id"] == cclass.pk


def test_run_update_marks_over_max(client):
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True, counted_runs=1)  # 1 counted run
    cclass = comp.classes.get(name="1")
    r0 = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    r1 = run_of(signal_in(comp, 1, "10:00:05.000", running=2))
    for run, number in ((r0, 1), (r1, 2)):
        post_json(client, "timing:run-update", run_id=run.id, bib_number="9",
                  class_id=cclass.pk, run_value=f"counted-{number}")
    rows = serialize_arrangement(comp)["rows"]
    assert all(row["run"]["over_max"] for row in rows)


def test_run_update_rejects_without_active_competition(client):
    comp = make_active_competition()
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    Competition.objects.update(is_active=False)
    resp = post_json(client, "timing:run-update", run_id=run.id, bib_number="1")
    assert resp.status_code == 400


def test_live_view_renders_and_prompts_without_competition(client):
    make_active_competition()
    assert client.get(reverse("timing:times")).status_code == 200
    Competition.objects.update(is_active=False)
    body = client.get(reverse("timing:times")).content.decode()
    assert "pick a competition" in body.lower()


def test_single_light_barrier_alternates_start_finish():
    comp = make_active_competition()
    settings = TimingSettings.load()
    settings.start_channel = settings.finish_channel = 1  # one beam for both
    settings.save()
    signal_in(comp, 1, "10:00:00.000", running=1)  # start
    signal_in(comp, 1, "10:00:30.000", running=2)  # finish → closes the start
    signal_in(comp, 1, "10:01:00.000", running=3)  # start again
    rows = serialize_arrangement(comp)["rows"]
    assert rows[0]["start"]["time"] == "10:01:00.000" and rows[0]["finish"] is None
    assert rows[1]["start"]["time"] == "10:00:00.000"
    assert rows[1]["finish"]["time"] == "10:00:30.000"


def test_already_recorded_run_is_disabled(client):
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True, counted_runs=2)
    cclass = comp.classes.get(name="1")
    r0 = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    r1 = run_of(signal_in(comp, 1, "10:00:30.000", running=2))
    post_json(client, "timing:run-update", run_id=r0.id, bib_number="5",
              class_id=cclass.pk, run_value="counted-1")
    post_json(client, "timing:run-update", run_id=r1.id, bib_number="5", class_id=cclass.pk)
    rows = {row["run"]["id"]: row for row in serialize_arrangement(comp)["rows"]}
    options = {o["value"]: o["disabled"] for o in rows[r1.id]["run"]["run_options"]}
    assert options["counted-1"] is True   # #5 already has a C1
    assert options["counted-2"] is False


def test_serialized_time_carries_manual_flag():
    comp = make_active_competition()
    manual = TimingSignal.objects.create(
        competition=comp, running_number=1, port=1, device_time=_t("10:00:00.000"), is_manual=True
    )
    arrangement.ingest(manual, TimingSettings.load())
    row = serialize_arrangement(comp)["rows"][0]
    assert row["start"]["manual"] is True


def _bib_participant(comp, bib, cclass):
    p = Participant.objects.create(
        competition_type=comp.competition_type, first_name=f"P{bib}", last_name="X",
        date_of_birth=datetime.date(2010, 1, 1), license_number=str(bib),
    )
    EventEntry.objects.create(participant=p, competition=comp, bib_number=bib)
    ClassAssignment.objects.create(participant=p, competition_class=cclass)
    return p


def test_start_finish_times_truncated_to_precision():
    comp = make_active_competition(timing_precision=CompetitionType.Precision.HUNDREDTHS)
    signal_in(comp, 1, "10:00:00.567", running=1)
    row = serialize_arrangement(comp)["rows"][0]
    assert row["start"]["time"] == "10:00:00.56"  # cut to 1/100, not .567 or rounded


def test_run_auto_sets_to_next_undone(client):
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True)  # practice_runs=1, counted_runs=2
    cclass = comp.classes.get(name="1")
    _bib_participant(comp, 1, cclass)
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    resp = post_json(client, "timing:run-update", run_id=run.id, bib_number="1").json()
    assert resp["row"]["run"]["run_value"] == "practice-1"


def test_new_bib_updates_class_and_clearing_bib_clears_it(client):
    comp = make_active_competition()
    comp.classes.filter(name__in=["1", "2"]).update(is_running=True)
    c1, c2 = comp.classes.get(name="1"), comp.classes.get(name="2")
    _bib_participant(comp, 1, c1)
    _bib_participant(comp, 2, c2)
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    first = post_json(client, "timing:run-update", run_id=run.id, bib_number="1").json()
    assert first["row"]["run"]["class_id"] == c1.pk
    second = post_json(client, "timing:run-update", run_id=run.id, bib_number="2").json()
    assert second["row"]["run"]["class_id"] == c2.pk  # updated, not sticky
    cleared = post_json(client, "timing:run-update", run_id=run.id, bib_number="").json()
    assert cleared["row"]["run"]["bib_number"] is None
    assert cleared["row"]["run"]["class_id"] is None


def test_unknown_bib_is_flagged_but_kept(client):
    comp = make_active_competition()
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    resp = post_json(client, "timing:run-update", run_id=run.id, bib_number="999").json()
    assert resp["row"]["run"]["bib_number"] == 999
    assert resp["row"]["run"]["bib_unknown"] is True


def test_start_fills_oldest_placeholder(client):
    comp = make_active_competition()
    post_json(client, "timing:run-add")  # placeholder A (created first)
    post_json(client, "timing:run-add")  # placeholder B
    a, b = list(TimedRun.objects.order_by("id"))
    assert serialize_arrangement(comp)["rows"][0]["placeholder"] is True
    signal_in(comp, 1, "10:00:00.000", running=1)  # first start fills A
    a.refresh_from_db(); b.refresh_from_db()
    assert a.start_signal is not None and b.start_signal is None
    assert TimedRun.objects.count() == 2  # no extra row created


def test_delete_removes_only_empty_placeholder(client):
    comp = make_active_competition()
    timed = run_of(signal_in(comp, 1, "10:00:00.000"))  # timed run first (no placeholder)
    post_json(client, "timing:run-add")                 # then an empty placeholder
    placeholder = TimedRun.objects.exclude(pk=timed.pk).get()
    post_json(client, "timing:run-delete", run_id=timed.id)       # has a time → kept
    post_json(client, "timing:run-delete", run_id=placeholder.id)  # empty → removed
    assert TimedRun.objects.filter(pk=timed.pk).exists()
    assert not TimedRun.objects.filter(pk=placeholder.pk).exists()


def test_ignoring_sole_time_keeps_pre_entered_row(client):
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True)
    _bib_participant(comp, 1, comp.classes.get(name="1"))
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    post_json(client, "timing:run-update", run_id=run.id, bib_number="1")  # pre-enter a bib
    start = run.start_signal
    post_json(client, "timing:ignore", signal_id=start.id, ignored=True)
    run.refresh_from_db()
    # The wrong start is gone but the row survives as a placeholder holding the bib.
    assert run.start_signal_id is None and run.bib_number == 1
