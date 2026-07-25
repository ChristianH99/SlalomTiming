import datetime
import json
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync
from django.urls import reverse

from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant

from . import arrangement, autotiming, calc, views
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


def test_dashboard_view_without_competition(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"No competition is selected" in response.content


def test_dashboard_view_renders_overview(client):
    make_active_competition()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Event overview" in response.content
    assert b"dash-data" in response.content


def test_dashboard_state_reports_progress(client):
    comp = make_active_competition()
    comp.start_pattern = [{"window": None, "chips": ["counted"]}]
    comp.save()
    cclass = comp.classes.create(name="A", is_running=True, run_position=1,
                                 practice_runs=0, counted_runs=1)
    participant = make_participant(comp.competition_type, 1, comp)
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)

    response = client.get(reverse("timing:dashboard-state"))
    data = response.json()
    assert data["competition"] is True
    # One starter, one counted run expected, nothing finished yet.
    assert data["progress"]["expected"] == 1
    assert data["progress"]["finished"] == 0
    assert data["stats"]["participants"] == 1
    names = [c["name"] for c in data["classes"]]
    assert "A" in names


def test_dashboard_state_without_competition(client):
    response = client.get(reverse("timing:dashboard-state"))
    assert response.json() == {"competition": False}


def test_dashboard_progress_without_start_pattern(client):
    # No start pattern: competitors turn up in any order, but the expected run
    # total is still known from the entries and their classes.
    comp = make_active_competition()
    assert comp.start_pattern == []
    cclass = comp.classes.create(name="A", is_running=True, run_position=1,
                                 practice_runs=1, counted_runs=2)
    participant = make_participant(comp.competition_type, 1, comp)
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)

    data = client.get(reverse("timing:dashboard-state")).json()
    # 1 practice + 2 counted = 3 expected runs, none finished, with no pattern.
    assert data["progress"]["expected"] == 3
    assert data["progress"]["finished"] == 0
    klass = next(c for c in data["classes"] if c["name"] == "A")
    assert klass["total"] == 3 and klass["status"] == "not_started"

    # A finished counted run recorded free-order (no start order) still counts.
    TimedRun.objects.create(
        competition=comp, bib_number=1, competition_class=cclass,
        class_occurrence=0, run_type=TimedRun.RunType.COUNTED, run_number=1,
        manual_run_time=Decimal("12.34"), manual_entry=True,
    )
    data = client.get(reverse("timing:dashboard-state")).json()
    assert data["progress"]["finished"] == 1
    klass = next(c for c in data["classes"] if c["name"] == "A")
    assert klass["finished"] == 1 and klass["status"] == "running"


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


def test_settings_requires_ip_and_port_for_cp540(client):
    response = client.post(reverse("timing:settings"), {
        "device": "cp540", "start_channel": "1", "finish_channel": "2", "ip_address": "", "port": "",
    })
    assert response.status_code == 200
    assert "ip_address" in response.context["form"].errors
    assert "port" in response.context["form"].errors


def test_settings_keeps_ip_and_port_when_not_cp540(client):
    # The address/port survive a switch away from the CP540, so they don't have to
    # be re-typed on switching back.
    TimingSettings.objects.create(device="cp540", ip_address="192.168.0.9", port=4001)
    client.post(reverse("timing:settings"), {
        "device": "simulator", "start_channel": "1", "finish_channel": "2",
        "ip_address": "192.168.0.9", "port": "4001",
    })
    settings = TimingSettings.load()
    assert settings.ip_address == "192.168.0.9"
    assert settings.port == 4001


# ----- CP540 line parsing -----

def test_cp540_parses_plain_tn_line():
    from . import cp540
    assert cp540.parse_tn("TN         1 M1     1:59.48100     0") == (
        1, 1, True, datetime.time(0, 1, 59, 481000)
    )
    assert cp540.parse_tn("TN         2  1     2:16.39900     0") == (
        2, 1, False, datetime.time(0, 2, 16, 399000)
    )
    assert cp540.parse_tn("TN         1 M4     2:21.55800     0") == (
        1, 4, True, datetime.time(0, 2, 21, 558000)
    )


def test_cp540_parses_net_time_tn_line_with_extra_column():
    from . import cp540
    # Net-time mode prepends a column; the running number is still the one just
    # before the input, and the input just before the time.
    assert cp540.parse_tn("TN    1    1 M1     3:32.93800     0") == (
        1, 1, True, datetime.time(0, 3, 32, 938000)
    )
    assert cp540.parse_tn("TN    3    3  1     3:44.84800     0") == (
        3, 1, False, datetime.time(0, 3, 44, 848000)
    )


def test_cp540_parses_full_clock_tn_line():
    from . import cp540
    # The device normally sends a full hh:mm:ss.fffff time of day.
    assert cp540.parse_tn("TN         1 M1     00:40:12.34500     0") == (
        1, 1, True, datetime.time(0, 40, 12, 345000)
    )
    assert cp540.parse_tn("TN    2    2  4     01:02:03.10000     0") == (
        2, 4, False, datetime.time(1, 2, 3, 100000)
    )


def test_cp540_ignores_non_tn_lines():
    from . import cp540
    for line in ("OP 01  00 PTB SEQUENTIAL 1-4", "CL 01", "RR    1    1           2.55000", ""):
        assert cp540.parse_tn(line) is None


def test_cp540_time_normalises_minutes_over_an_hour():
    from . import cp540
    # 75 minutes rolls into the hour field rather than staying an illegal minute.
    assert cp540.parse_time("75:30.50000") == datetime.time(1, 15, 30, 500000)


# ----- CP540 reconnect + the device-link alarm -----

def test_cp540_reader_reconnects_after_the_link_drops():
    """A knocked cable used to end the reader thread for good — every later time
    lost with nothing said. It has to keep trying while it is the live device."""
    import socket
    from . import cp540

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)
    host, port = server.getsockname()
    reader = cp540.CP540Reader()
    try:
        reader.start(host, port)
        first, _addr = server.accept()
        first.sendall(b"TN         1  1     10:00:00.00000     0\n")
        first.close()                       # the device drops the link
        second, _addr = server.accept()     # …and the reader comes back on its own
        assert second is not None
        second.close()
    finally:
        reader.stop()
        server.close()
    assert any("Reconnecting" in entry["text"] for entry in reader.snapshot()["log"])


def test_device_link_is_quiet_for_the_simulator():
    from . import cp540
    settings = TimingSettings.load()
    settings.device = TimingSettings.Device.SIMULATOR
    settings.save()
    # The simulator holds no connection, so there is nothing to alarm about.
    assert cp540.link_state(settings) == {
        "monitored": False, "ok": True, "status": "", "message": ""
    }


def test_device_link_alarms_when_the_cp540_is_selected_but_down(client):
    settings = TimingSettings.load()
    settings.device = TimingSettings.Device.CP540
    settings.save()
    comp = make_active_competition()
    # Both live views carry it, so the banner is raised wherever the operator is.
    assert serialize_arrangement(comp)["device_link"]["ok"] is False
    assert autotiming.serialize(comp)["device_link"]["monitored"] is True


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


def test_signal_endpoint_refused_when_device_is_cp540(client):
    # Only one source writes at a time: a stray simulator tab can't inject times
    # while the CP540 is the selected device.
    TimingSettings.objects.create(device="cp540", ip_address="1.2.3.4", port=7000)
    response = _post_signal(client, running_number=1, port=1, is_manual=False, time="10:00:00.000")
    assert response.status_code == 409 and response.json()["ok"] is False
    assert not TimingSignal.objects.exists()


def test_reconcile_places_saved_but_unplaced_signals():
    # A time persisted but never placed into a run (a lost placement race) is
    # self-healed by reconcile, so it is never left invisible.
    comp = make_active_competition()
    sig = TimingSignal.objects.create(
        competition=comp, running_number=1, port=1, device_time=_t("10:00:00.000")
    )
    assert not TimedRun.objects.filter(start_signal=sig).exists()
    arrangement.reconcile(comp, TimingSettings.load())
    assert TimedRun.objects.filter(start_signal=sig).exists()


def test_record_signal_captures_to_recovery_file_when_db_locked(tmp_path, monkeypatch):
    from django.db import OperationalError

    from apps.timing import ingest

    make_active_competition()
    monkeypatch.setattr(ingest, "UNRECORDED_LOG", tmp_path / "unrecorded.log")
    monkeypatch.setattr(ingest.time, "sleep", lambda *a, **k: None)  # no real waiting

    def locked(*args, **kwargs):
        raise OperationalError("database is locked")

    monkeypatch.setattr(ingest.TimingSignal.objects, "create", locked)

    result = ingest.record_signal(3, 1, True, _t("10:00:00.000"), source="cp540")
    assert result is None  # DB write failed…
    captured = (tmp_path / "unrecorded.log").read_text().strip()
    assert '"running_number": 3' in captured and '"source": "cp540"' in captured  # …but not lost


def test_input_lock_toggle_endpoint_persists(client):
    resp = post_json(client, "timing:input-lock", locked=True).json()
    assert resp["ok"] is True and resp["locked"] is True
    assert TimingSettings.load().ignore_incoming is True
    post_json(client, "timing:input-lock", locked=False)
    assert TimingSettings.load().ignore_incoming is False


def test_locked_input_sends_incoming_time_straight_to_ignore(client):
    comp = make_active_competition()
    TimingSettings.objects.create(device="simulator", ignore_incoming=True)
    _post_signal(client, running_number=1, port=1, is_manual=False, time="10:00:00.000")
    # The time is captured but ignored (not placed into a run).
    signal = TimingSignal.objects.get()
    assert signal.ignored is True
    assert not TimedRun.objects.filter(start_signal=signal).exists()
    data = serialize_arrangement(comp)
    assert data["input_locked"] is True
    assert data["rows"] == [] and [ig["id"] for ig in data["ignored"]] == [signal.id]


def test_unlocked_input_still_places_times(client):
    comp = make_active_competition()
    _post_signal(client, running_number=1, port=1, is_manual=False, time="10:00:00.000")
    signal = TimingSignal.objects.get()
    assert signal.ignored is False
    assert TimedRun.objects.filter(start_signal=signal).exists()
    assert serialize_arrangement(comp)["input_locked"] is False


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


def test_rows_order_by_arrival_not_the_device_clock():
    # A real device runs on its own clock (often not wall-clock). A freshly
    # arrived start whose device time is *earlier* than existing data must still
    # appear on top — ordering is by arrival, not the device clock.
    comp = make_active_competition()
    signal_in(comp, 1, "14:00:00.000", running=1)   # older data, later clock
    signal_in(comp, 1, "00:40:00.000", running=2)   # newer arrival, earlier clock
    rows = serialize_arrangement(comp)["rows"]
    assert rows[0]["start"]["time"] == "00:40:00.000"
    assert rows[1]["start"]["time"] == "14:00:00.000"


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
    assert resp["row"]["run"]["class_key"] == f"{cclass.pk}:0"


def test_run_update_marks_over_max(client):
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True, counted_runs=1)  # 1 counted run
    cclass = comp.classes.get(name="1")
    r0 = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    r1 = run_of(signal_in(comp, 1, "10:00:05.000", running=2))
    for run, number in ((r0, 1), (r1, 2)):
        post_json(client, "timing:run-update", run_id=run.id, bib_number="9",
                  class_key=f"{cclass.pk}:0", run_value=f"counted-{number}")
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
    assert client.get(reverse("timing:manual")).status_code == 200
    Competition.objects.update(is_active=False)
    body = client.get(reverse("timing:manual")).content.decode()
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
              class_key=f"{cclass.pk}:0", run_value="counted-1")
    post_json(client, "timing:run-update", run_id=r1.id, bib_number="5", class_key=f"{cclass.pk}:0")
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
    assert first["row"]["run"]["class_key"] == f"{c1.pk}:0"
    second = post_json(client, "timing:run-update", run_id=run.id, bib_number="2").json()
    assert second["row"]["run"]["class_key"] == f"{c2.pk}:0"  # updated, not sticky
    cleared = post_json(client, "timing:run-update", run_id=run.id, bib_number="").json()
    assert cleared["row"]["run"]["bib_number"] is None
    assert cleared["row"]["run"]["class_key"] is None


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


def test_timing_view_does_not_leak_across_competitions(client):
    comp_a = make_active_competition()  # active
    comp_b = Competition.objects.create(
        competition_type=comp_a.competition_type, name="B", date=datetime.date(2026, 5, 2)
    )
    signal_in(comp_a, 1, "10:00:00.000")  # a run under A
    assert serialize_arrangement(comp_a)["rows"]  # A has it
    # Switching the active competition must leave the live view empty for B.
    client.post(reverse("competitions:select", kwargs={"pk": comp_b.pk}))
    resp = client.get(reverse("timing:arrangement")).json()
    assert resp["competition"] is True and resp["rows"] == []


def _assign_twice(comp, bib, cclass):
    p = Participant.objects.create(
        competition_type=comp.competition_type, first_name=f"P{bib}", last_name="X",
        date_of_birth=datetime.date(2010, 1, 1), license_number=str(bib),
    )
    EventEntry.objects.create(participant=p, competition=comp, bib_number=bib)
    ClassAssignment.objects.create(participant=p, competition_class=cclass)
    ClassAssignment.objects.create(participant=p, competition_class=cclass)
    return p


def test_multi_entry_class_offers_separate_numbered_slots(client):
    comp = make_active_competition()
    comp.classes.filter(name="2").update(is_running=True, allow_multiple_entries=True)
    c2 = comp.classes.get(name="2")
    _assign_twice(comp, 1, c2)
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    resp = post_json(client, "timing:run-update", run_id=run.id, bib_number="1").json()
    opts = resp["row"]["run"]["class_options"]
    assert [o["value"] for o in opts] == [f"{c2.pk}:0", f"{c2.pk}:1"]
    assert [o["label"] for o in opts] == ["2 (1)", "2 (2)"]


def test_run_occurrences_are_independent(client):
    comp = make_active_competition()
    comp.classes.filter(name="2").update(is_running=True, allow_multiple_entries=True)
    c2 = comp.classes.get(name="2")
    _assign_twice(comp, 1, c2)
    r0 = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    post_json(client, "timing:run-update", run_id=r0.id, bib_number="1",
              class_key=f"{c2.pk}:0", run_value="practice-1")
    # A second run on occurrence 1 can still take P1 (independent of occurrence 0).
    r1 = run_of(signal_in(comp, 1, "10:00:10.000", running=2))
    resp = post_json(client, "timing:run-update", run_id=r1.id, bib_number="1",
                     class_key=f"{c2.pk}:1").json()
    disabled = {o["value"]: o["disabled"] for o in resp["row"]["run"]["run_options"]}
    assert disabled["practice-1"] is False


def test_completed_class_slot_is_disabled_and_default_advances(client):
    comp = make_active_competition()
    # 2 runs per slot (P1 + C1); allow the class twice.
    comp.classes.filter(name="2").update(
        is_running=True, allow_multiple_entries=True, practice_runs=1, counted_runs=1
    )
    c2 = comp.classes.get(name="2")
    _assign_twice(comp, 1, c2)
    r0 = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    r1 = run_of(signal_in(comp, 1, "10:00:10.000", running=2))
    post_json(client, "timing:run-update", run_id=r0.id, bib_number="1",
              class_key=f"{c2.pk}:0", run_value="practice-1")
    post_json(client, "timing:run-update", run_id=r1.id, bib_number="1",
              class_key=f"{c2.pk}:0", run_value="counted-1")  # occurrence 0 now complete
    r2 = run_of(signal_in(comp, 1, "10:00:20.000", running=3))
    resp = post_json(client, "timing:run-update", run_id=r2.id, bib_number="1").json()
    opts = {o["value"]: o["disabled"] for o in resp["row"]["run"]["class_options"]}
    assert opts[f"{c2.pk}:0"] is True    # first slot complete → disabled
    assert opts[f"{c2.pk}:1"] is False   # second slot still open
    assert resp["row"]["run"]["class_key"] == f"{c2.pk}:1"  # default advanced to it


# ----- auto timing -----

def auto_scenario(bibs=(1, 2)):
    """An active competition whose start order is one counted run for each of the
    given bibs, in bib order."""
    comp = make_active_competition(
        penalties_enabled=True, pylon_penalty=2, task_penalty=10,
        stop_line_penalty=5, max_penalty_per_task=10,
    )
    comp.classes.filter(name="1").update(
        is_running=True, run_position=0, practice_runs=0, counted_runs=1
    )
    cls = comp.classes.get(name="1")
    comp.start_pattern = [{"window": None, "chips": ["counted"]}]
    comp.save(update_fields=["start_pattern"])
    for bib in bibs:
        participant = make_participant(comp.competition_type, bib, comp)
        ClassAssignment.objects.create(participant=participant, competition_class=cls)
    return comp, cls


def test_computed_slots_follow_the_start_order():
    comp, _ = auto_scenario()
    slots = autotiming.computed_slots(comp)
    assert [slot["bib"] for slot in slots] == [1, 2]
    assert [slot["run_label"] for slot in slots] == ["C1", "C1"]
    assert all(key["key"].endswith(":counted:1") for key in slots)


def test_ordered_slots_apply_the_saved_override():
    comp, _ = auto_scenario()
    slots = autotiming.computed_slots(comp)
    comp.auto_timing_order = [slots[1]["key"], slots[0]["key"]]
    comp.save(update_fields=["auto_timing_order"])
    assert [slot["bib"] for slot in autotiming.ordered_slots(comp)] == [2, 1]


def test_times_bind_to_slots_positionally():
    comp, _ = auto_scenario()
    signal_in(comp, 1, "10:00:00.000", running=1)   # first start → slot 0 (bib 1)
    signal_in(comp, 1, "10:00:05.000", running=2)   # second start → slot 1 (bib 2)
    data = autotiming.serialize(comp)
    assert [item["bib"] for item in data["items"][:2]] == [1, 2]
    assert data["items"][0]["started"] is True
    assert data["current_index"] == 1   # the last to start stays current


def test_marshal_state_tracks_the_current_competitor():
    comp, _ = auto_scenario()
    signal_in(comp, 1, "10:00:00.000", running=1)
    state = autotiming.marshal_state(comp, 1)
    assert state["bib"] == 1
    assert state["run_id"] is not None


def submit_penalty(client, run, post=1, pylons=0, tasks=0, stopline=0, detail=None, submitted=True):
    return post_json(client, "timing:marshal-submit", post=post, run_id=run.id,
                     pylon_count=pylons, task_count=tasks, stopline_count=stopline,
                     detail=detail or {}, submitted=submitted)


def test_marshal_submit_shows_counts_and_detail_on_auto(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5", handles_stop_line=True)
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, pylons=3, tasks=1, stopline=1,
                   detail={"tasks": {"2": {"pylons": 3}, "4": {"task": True}}, "stop_line": True})
    box = autotiming.serialize(comp)["items"][0]["marshals"][0]
    assert box["submitted"] is True
    assert (box["pylons"], box["tasks"], box["stop_line"]) == (3, 1, True)
    # detail carries every watched task; the pop-up reads the marked ones.
    marks = {row["task"]: (row["pylons"], row["task_penalty"]) for row in box["detail"]["tasks"]}
    assert marks[2] == (3, False)
    assert marks[4] == (0, True)
    assert box["detail"]["stop_line"] is True


def test_total_time_includes_penalties(client):
    comp, _ = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    sig = signal_in(comp, 1, "10:00:00.000", running=1)
    signal_in(comp, 2, "10:00:10.000", running=1)   # finish → run time 10.000
    run = run_of(sig)
    submit_penalty(client, run, pylons=2)            # 2 × 2s = 4s
    item = autotiming.serialize(comp)["items"][0]
    assert item["run_time"] == "10.000"
    assert item["total_time"] == "14.000"            # run + penalty seconds
    assert item["total_pylons"] == 2


def test_submitted_penalty_is_locked_until_unlocked(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, pylons=1, submitted=True)
    # A locked row refuses further edits from the marshal.
    resp = submit_penalty(client, run, pylons=9, submitted=True)
    assert resp.status_code == 409 and resp.json()["locked"] is True
    assert autotiming.serialize(comp)["items"][0]["marshals"][0]["pylons"] == 1
    # The timekeeper unlocks it; edits are accepted again.
    assert post_json(client, "timing:marshal-unlock", post=1, run_id=run.id).status_code == 200
    submit_penalty(client, run, pylons=9, submitted=True)
    assert autotiming.serialize(comp)["items"][0]["marshals"][0]["pylons"] == 9


def test_lock_all_submits_every_post(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    MarshalPost.objects.create(competition=comp, number=2, tasks="6-8")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, post=1, pylons=2, submitted=False)   # entered, not submitted
    assert post_json(client, "timing:marshal-lock-all", run_id=run.id).status_code == 200
    boxes = {b["number"]: b for b in autotiming.serialize(comp)["items"][0]["marshals"]}
    assert boxes[1]["submitted"] and boxes[2]["submitted"]   # both locked
    assert boxes[1]["pylons"] == 2   # post 1 keeps its count
    assert boxes[2]["pylons"] == 0   # post 2 locked at zero


def test_task_edit_recomputes_and_requires_lock(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    # An unlocked post refuses a timekeeper edit.
    resp = post_json(client, "timing:marshal-task-edit", run_id=run.id, post=1, task=3, pylons=2)
    assert resp.status_code == 409
    # Lock, then edit a task's pylons — the aggregate recomputes.
    submit_penalty(client, run, post=1, pylons=1, detail={"tasks": {"2": {"pylons": 1}}})
    post_json(client, "timing:marshal-task-edit", run_id=run.id, post=1, task=3, pylons=2)
    box = autotiming.serialize(comp)["items"][0]["marshals"][0]
    assert box["pylons"] == 3   # task 2 (1) + task 3 (2)
    marks = {r["task"]: r["pylons"] for r in box["detail"]["tasks"]}
    assert marks[3] == 2
    # Setting a task's pylons to zero drops it.
    post_json(client, "timing:marshal-task-edit", run_id=run.id, post=1, task=2, pylons=0)
    assert autotiming.serialize(comp)["items"][0]["marshals"][0]["pylons"] == 2


def test_task_edit_toggles_stop_line(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-3", handles_stop_line=True)
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, post=1)
    post_json(client, "timing:marshal-task-edit", run_id=run.id, post=1, stop_line=True)
    assert autotiming.serialize(comp)["items"][0]["marshals"][0]["stop_line"] is True


def test_single_post_lock(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    assert post_json(client, "timing:marshal-lock", run_id=run.id, post=1).status_code == 200
    assert autotiming.serialize(comp)["items"][0]["marshals"][0]["submitted"] is True


def test_post_claim_is_exclusive(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    assert post_json(client, "timing:marshal-claim", post=1, token="A").json()["ok"] is True
    other = post_json(client, "timing:marshal-claim", post=1, token="B").json()
    assert other["ok"] is False and other["taken"] is True   # held by A
    assert post_json(client, "timing:marshal-claim", post=1, token="A").json()["ok"] is True  # heartbeat
    assert client.get(reverse("timing:marshal-claims"), {"token": "B"}).json()["taken"] == [1]
    assert client.get(reverse("timing:marshal-claims"), {"token": "A"}).json()["taken"] == []
    post_json(client, "timing:marshal-release", post=1, token="A")
    assert post_json(client, "timing:marshal-claim", post=1, token="B").json()["ok"] is True


def test_stale_claim_is_free(client):
    import datetime as _dt

    from django.utils import timezone

    from apps.competitions.models import CLAIM_TTL
    comp, _ = auto_scenario()
    MarshalPost.objects.create(
        competition=comp, number=1, tasks="1-5", claim_token="A",
        claim_seen=timezone.now() - _dt.timedelta(seconds=CLAIM_TTL + 5),
    )
    # The old claim has gone stale, so another device may take it.
    assert post_json(client, "timing:marshal-claim", post=1, token="B").json()["ok"] is True


def test_timekeeper_adjust_changes_the_total(client):
    comp, _ = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, pylons=2)
    post_json(client, "timing:auto-adjust", run_id=run.id, pylon_adjust=1, task_adjust=2)
    item = autotiming.serialize(comp)["items"][0]
    assert item["total_pylons"] == 3   # 2 from the post + 1 adjust
    assert item["total_tasks"] == 2    # 0 from the post + 2 adjust
    # A negative adjustment can't push the total below zero.
    post_json(client, "timing:auto-adjust", run_id=run.id, pylon_adjust=-9)
    assert autotiming.serialize(comp)["items"][0]["total_pylons"] == 0


def test_non_marshal_penalties_show_in_auto(client):
    # Penalties entered on the run's own counts (Manual timing / non-marshal mode)
    # must show in the Auto view too — the stepper edits the counts directly.
    comp, _ = auto_scenario()  # penalties_by_marshal_posts stays off
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    run.pylon_count, run.task_count, run.stopline_count = 2, 1, 1
    run.save()
    item = autotiming.serialize(comp)["items"][0]
    assert (item["total_pylons"], item["total_tasks"], item["total_stop"]) == (2, 1, 1)
    lines = {line["label"]: line for line in item["penalties"]}
    assert lines["Pylons"]["field"] == "pylon_count"   # stepper edits the count, not an adjust
    assert lines["Stop line"]["total"] == 1


def test_operator_owned_run_shows_its_own_penalties_in_marshal_mode(client):
    # The reported bug: in marshal mode a run the operator gave penalties to on the
    # Manual view (manual_entry) must still show them in Auto timing.
    comp, cls = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    post_json(client, "timing:run-update", run_id=run.id, task_count=1, stopline_count=1)
    run.refresh_from_db()
    assert run.manual_entry is True
    item = autotiming.serialize(comp)["items"][0]
    assert (item["total_tasks"], item["total_stop"]) == (1, 1)
    # And a later sync (marshal posts have nothing) must not wipe the operator's entry.
    autotiming.sync_bindings(comp)
    run.refresh_from_db()
    assert (run.task_count, run.stopline_count) == (1, 1)


def test_marshal_penalties_add_up_on_an_operator_selected_run(client):
    # The reported edge case: on a run the operator manually selected (manual_entry)
    # in marshal mode, penalties added from the Marshal Posts page must add into the
    # total — in the Auto view *and* the Manual view — not just show in the boxes.
    comp, cls = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    post = MarshalPost.objects.create(competition=comp, number=1, tasks="1")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    # Operator selects the participant on the Manual view (owns the run) …
    post_json(client, "timing:run-update", run_id=run.id, bib_number="1")
    run.refresh_from_db()
    assert run.manual_entry is True
    # … then a marshal post records 2 pylons for it.
    submit_penalty(client, run, pylons=2)
    item = autotiming.serialize(comp)["items"][0]
    assert item["total_pylons"] == 2                      # Auto total now counts them
    row = next(r for r in serialize_arrangement(comp)["rows"] if r["run"]["id"] == run.id)
    assert row["run"]["penalty"] == 4                     # Manual total too (2 × 2s)


def test_stop_line_adjust_in_marshal_mode(client):
    comp, _ = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    post = MarshalPost.objects.create(competition=comp, number=1, tasks="1", handles_stop_line=True)
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, stopline=1)   # post recorded a stop-line miss
    post_json(client, "timing:auto-adjust", run_id=run.id, stopline_adjust=1)
    item = autotiming.serialize(comp)["items"][0]
    assert item["total_stop"] == 2   # 1 from the post + 1 timekeeper adjust
    stop_line = next(l for l in item["penalties"] if l["label"] == "Stop line")
    assert stop_line["field"] == "stopline_adjust" and stop_line["base"] == 1


def test_marshal_state_returns_detail_for_resume(client):
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, pylons=2, detail={"tasks": {"3": {"pylons": 2}}, "stop_line": False})
    state = autotiming.marshal_state(comp, 1)
    assert state["run_id"] == run.id
    assert state["penalty"]["submitted"] is True
    assert state["penalty"]["detail"]["tasks"]["3"]["pylons"] == 2


def test_run_group_labels_are_carried(client):
    comp, _ = auto_scenario()
    slots = autotiming.computed_slots(comp)
    assert slots[0]["group_index"] == 0
    assert slots[0]["group_label"] == "1"   # the running class's name


def test_auto_reorder_persists_and_drops_unknown_keys(client):
    comp, _ = auto_scenario()
    slots = autotiming.computed_slots(comp)
    keys = [slots[1]["key"], slots[0]["key"]]
    post_json(client, "timing:auto-reorder", order=["bogus"] + keys)
    comp.refresh_from_db()
    assert comp.auto_timing_order == keys   # unknown key dropped, order kept
    post_json(client, "timing:auto-reset-order")
    comp.refresh_from_db()
    assert comp.auto_timing_order == []


def test_auto_state_endpoint_returns_ordered_items(client):
    comp, _ = auto_scenario()
    signal_in(comp, 1, "10:00:00.000", running=1)
    data = client.get(reverse("timing:auto-state")).json()
    assert data["competition"] is True
    assert data["items"][0]["bib"] == 1


def test_auto_page_needs_an_active_competition(client):
    Competition.objects.update(is_active=False)
    data = client.get(reverse("timing:auto-state")).json()
    assert data["competition"] is False


# ----- current follows latest activity -----

def test_current_follows_latest_finish_not_last_start():
    comp, _ = auto_scenario(bibs=(1, 2))
    # Both start (bib 2 last), then bib 1 finishes — current must jump to bib 1.
    signal_in(comp, 1, "10:00:00.000", running=1)
    signal_in(comp, 1, "10:00:05.000", running=2)
    signal_in(comp, 2, "10:00:35.000", running=1)  # finish closes the oldest open (bib 1)
    data = autotiming.serialize(comp)
    assert data["items"][data["current_index"]]["bib"] == 1
    assert data["items"][data["current_index"]]["finish"]["time"] == "10:00:35.000"


# ----- Auto -> Manual sync (identities persisted onto the runs) -----

def test_sync_bindings_persists_identity_onto_auto_runs():
    comp, cls = auto_scenario(bibs=(1, 2))
    s1 = signal_in(comp, 1, "10:00:00.000", running=1)
    s2 = signal_in(comp, 1, "10:00:05.000", running=2)
    autotiming.sync_bindings(comp)
    r1, r2 = run_of(s1), run_of(s2)
    assert (r1.bib_number, r1.competition_class_id, r1.run_type, r1.run_number) == \
        (1, cls.pk, "counted", 1)
    assert (r2.bib_number, r2.run_type, r2.run_number) == (2, "counted", 1)
    # And now the Manual view shows those bibs/run.
    rows = serialize_arrangement(comp)["rows"]
    bibs = {row["run"]["bib_number"] for row in rows}
    assert bibs == {1, 2}


def test_marshal_penalties_show_in_manual_view_without_caching_counts():
    comp, cls = auto_scenario(bibs=(1,))
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    post = MarshalPost.objects.create(competition=comp, number=1, tasks="1")
    s1 = signal_in(comp, 1, "10:00:00.000", running=1)
    run = run_of(s1)
    from .models import MarshalPenalty
    MarshalPenalty.objects.create(timed_run=run, marshal_post=post, pylon_count=3, task_count=1)
    # The Manual view total reflects the marshal penalties (3×2 + 1×10 = 16s) …
    row = next(r for r in serialize_arrangement(comp)["rows"] if r["run"]["id"] == run.id)
    assert row["run"]["penalty"] == 16
    # … computed live, not cached onto the auto-bound run's own counts.
    run.refresh_from_db()
    assert (run.pylon_count, run.task_count) == (0, 0)


# ----- Manual -> Auto (a pre-entered run claims its slot, times skip it) -----

def test_manual_run_claims_slot_and_new_time_skips_it(client):
    comp, cls = auto_scenario(bibs=(1, 2))
    # Operator keys in bib 1's run on the Manual view (device missed it): bib +
    # run + a typed run time, which completes it.
    run = TimedRun.objects.create(competition=comp)
    post_json(client, "timing:run-update", run_id=run.id, bib_number="1",
              class_key=f"{cls.pk}:0", run_value="counted-1")
    post_json(client, "timing:set-runtime", run_id=run.id, run_time="30.00")
    run.refresh_from_db()
    assert run.manual_entry is True
    # A device start arrives: bib 1 is already complete (its slot is claimed), so
    # the time must skip to bib 2 rather than fill the manual run.
    s = signal_in(comp, 1, "10:00:00.000", running=9)
    data = autotiming.serialize(comp)
    slot0, slot1 = data["items"][0], data["items"][1]
    assert slot0["bib"] == 1 and slot0["run_id"] == run.id      # manual pre-entry, in place
    assert slot1["bib"] == 2 and slot1["run_id"] == run_of(s).id  # device time skipped to bib 2
    run.refresh_from_db()
    assert run.start_signal_id is None   # the manual run was not given the device start


# ----- manual run time + resolved_run_time -----

def test_resolved_run_time_prefers_manual_override():
    comp = make_active_competition()
    run = TimedRun.objects.create(competition=comp, manual_run_time=Decimal("12.500"))
    assert calc.resolved_run_time(run, 2) == Decimal("12.50")


def test_set_runtime_endpoint_sets_manual_run_time(client):
    comp, cls = auto_scenario(bibs=(1,))
    run = TimedRun.objects.create(competition=comp)
    resp = post_json(client, "timing:set-runtime", run_id=run.id, run_time="30.25").json()
    run.refresh_from_db()
    assert run.manual_run_time == Decimal("30.250")
    assert run.manual_entry is True
    assert resp["row"]["run_time_manual"] is True
    # Clearing it removes the override.
    post_json(client, "timing:set-runtime", run_id=run.id, run_time="")
    run.refresh_from_db()
    assert run.manual_run_time is None


@pytest.mark.parametrize("bad", ["99999999", "1e3", "-5", "999999:00", "12.3.4", "abc"])
def test_set_runtime_refuses_a_time_the_column_cannot_hold(client, bad):
    """One over-long run time used to be stored happily and then raise on every
    later read of the row — 500ing both timing views and the dashboard for the
    rest of the event. It has to be refused at the door."""
    comp, cls = auto_scenario(bibs=(1,))
    run = TimedRun.objects.create(competition=comp)
    resp = post_json(client, "timing:set-runtime", run_id=run.id, run_time=bad)
    assert resp.status_code == 400
    run.refresh_from_db()
    assert run.manual_run_time is None
    # The row is still readable, and so are the views that read it.
    assert client.get(reverse("timing:arrangement")).status_code == 200
    assert client.get(reverse("timing:auto-state")).status_code == 200


def test_set_runtime_truncates_to_the_column_precision(client):
    comp, cls = auto_scenario(bibs=(1,))
    run = TimedRun.objects.create(competition=comp)
    post_json(client, "timing:set-runtime", run_id=run.id, run_time="30.259999")
    run.refresh_from_db()
    assert run.manual_run_time == Decimal("30.259")   # truncated, never rounded


def test_penalty_counts_are_bounded(client):
    """PositiveSmallIntegerField tops out at 32767 and SQLite doesn't enforce it,
    so an out-of-range count is latent corruption of the same kind."""
    comp, cls = auto_scenario(bibs=(1,))
    run = TimedRun.objects.create(competition=comp)
    post_json(client, "timing:run-update", run_id=run.id, pylon_count="1000000000")
    run.refresh_from_db()
    assert run.pylon_count == views.MAX_PENALTY_COUNT
    post_json(client, "timing:auto-adjust", run_id=run.id, pylon_adjust=-10 ** 9)
    run.refresh_from_db()
    assert run.pylon_adjust == -views.MAX_PENALTY_COUNT


def test_marshal_submit_counts_are_bounded(client):
    comp, cls = auto_scenario(bibs=(1,))
    post = MarshalPost.objects.create(competition=comp, number=1, tasks="1")
    run = TimedRun.objects.create(competition=comp)
    post_json(client, "timing:marshal-submit", run_id=run.id, post=1,
              pylon_count=10 ** 9, task_count=10 ** 9, stopline_count=10 ** 9)
    from .models import MarshalPenalty
    mp = MarshalPenalty.objects.get(timed_run=run, marshal_post=post)
    assert mp.pylon_count == mp.task_count == mp.stopline_count == views.MAX_PENALTY_COUNT


def test_set_time_endpoint_keys_in_a_time_and_rails_the_device_one(client):
    comp, cls = auto_scenario(bibs=(1,))
    device = signal_in(comp, 1, "10:00:00.000", running=1)
    run = run_of(device)
    resp = post_json(client, "timing:set-time", run_id=run.id, slot="start",
                     time="10:00:02.000").json()
    run.refresh_from_db()
    assert run.start_signal.entered is True
    assert run.start_signal.device_time == _t("10:00:02.000")
    assert run.manual_entry is True
    assert resp["row"]["start"]["entered"] is True
    device.refresh_from_db()
    assert device.ignored is True   # the displaced measured time is kept on the rail


def test_set_time_endpoint_rejects_start_after_finish(client):
    comp, cls = auto_scenario(bibs=(1,))
    start = signal_in(comp, 1, "10:00:00.000", running=1)
    run = run_of(start)
    finish = TimingSignal.objects.create(competition=comp, running_number=1, port=2,
                                         device_time=_t("10:00:10.000"))
    run.finish_signal = finish
    run.save(update_fields=["finish_signal"])
    resp = post_json(client, "timing:set-time", run_id=run.id, slot="start",
                     time="10:00:20.000").json()
    assert resp["rejected"] is True
    run.refresh_from_db()
    assert run.start_signal_id == start.id   # unchanged


def test_dragging_the_original_time_back_over_an_override_restores_it(client):
    comp, cls = auto_scenario(bibs=(1,))
    d_start = signal_in(comp, 1, "10:00:00.000", running=1)
    d_finish = signal_in(comp, 2, "10:00:30.000", running=1)
    run = run_of(d_start)
    # Override the finish: a keyed-in signal takes the slot, the device time is railed.
    post_json(client, "timing:set-time", run_id=run.id, slot="finish", time="10:00:35.000")
    run.refresh_from_db()
    assert run.finish_signal.entered is True
    d_finish.refresh_from_db()
    assert d_finish.ignored is True

    # Drag the original measured time back onto that slot.
    resp = post_json(client, "timing:pair", signal_id=d_finish.id, run_id=run.id, slot="finish").json()
    assert resp["ok"] is True
    run.refresh_from_db()
    assert run.finish_signal_id == d_finish.id        # the measured time is back …
    assert run.finish_signal.entered is False         # … as a normal (not keyed-in) time
    d_finish.refresh_from_db()
    assert d_finish.ignored is False
    # The replaced override is discarded, not left as a stray signal/row.
    assert not TimingSignal.objects.filter(competition=comp, entered=True).exists()

    # Overriding again rails the original so it can be recovered a second time.
    post_json(client, "timing:set-time", run_id=run.id, slot="finish", time="10:00:40.000")
    d_finish.refresh_from_db()
    assert d_finish.ignored is True


def test_keying_a_start_time_onto_an_upcoming_slot_makes_them_current(client):
    comp, cls = auto_scenario(bibs=(1, 2))
    # bib 1 ran; bib 2 hasn't started, so their slot has no run yet.
    signal_in(comp, 1, "10:00:00.000", running=1)
    signal_in(comp, 2, "10:00:30.000", running=1)
    slot2 = autotiming.serialize(comp)["items"][1]
    assert slot2["bib"] == 2 and slot2["run_id"] is None

    # Key a start time onto bib 2's start-order slot (no run_id — only the slot key).
    resp = post_json(client, "timing:set-time", slot="start", time="10:00:40.000",
                     slot_key=slot2["key"]).json()
    assert resp["ok"] is True
    data = autotiming.serialize(comp)
    assert data["items"][1]["run_id"] is not None
    assert data["items"][1]["start"]["entered"] is True
    assert data["current_index"] == 1   # keying a start makes them current
    run2 = TimedRun.objects.get(competition=comp, bib_number=2)
    assert run2.manual_entry is True
    assert (run2.run_type, run2.run_number) == ("counted", 1)


def test_clearing_a_time_on_a_slot_with_no_run_is_a_noop(client):
    comp, cls = auto_scenario(bibs=(1,))
    slot = autotiming.serialize(comp)["items"][0]
    assert slot["run_id"] is None
    resp = post_json(client, "timing:set-time", slot="start", time="", slot_key=slot["key"]).json()
    assert resp["ok"] is True
    assert not TimedRun.objects.filter(competition=comp).exists()   # nothing created


def test_dragging_a_time_onto_an_upcoming_competitor_creates_their_run(client):
    comp, cls = auto_scenario(bibs=(1, 2))
    d_start = signal_in(comp, 1, "10:00:00.000", running=1)
    run1 = run_of(d_start)
    slot2 = autotiming.serialize(comp)["items"][1]
    assert slot2["bib"] == 2 and slot2["run_id"] is None   # bib 2 upcoming, no run

    # Drag bib 1's start onto bib 2's (run-less) start slot.
    resp = post_json(client, "timing:pair", signal_id=d_start.id, slot="start",
                     slot_key=slot2["key"]).json()
    assert resp["ok"] is True
    run2 = TimedRun.objects.get(competition=comp, bib_number=2)
    assert run2.start_signal_id == d_start.id   # the time moved to the next competitor
    run1.refresh_from_db()
    assert run1.start_signal_id is None         # and left the one it came from


def test_rejected_pairing_leaves_the_dragged_time_on_the_rail(client):
    comp, cls = auto_scenario(bibs=(1,))
    start = signal_in(comp, 1, "10:00:10.000", running=1)
    run = run_of(start)
    # An ignored finish that is *before* the start — dropping it on the finish slot
    # must be rejected, and it must stay ignored (not vanish off the rail).
    early = TimingSignal.objects.create(
        competition=comp, running_number=1, port=2, device_time=_t("10:00:05.000"), ignored=True
    )
    resp = post_json(client, "timing:pair", signal_id=early.id, run_id=run.id, slot="finish").json()
    assert resp["rejected"] is True
    early.refresh_from_db()
    assert early.ignored is True
