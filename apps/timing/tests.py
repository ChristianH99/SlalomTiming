import datetime
import json
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.urls import resolve, reverse

from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant

from . import arrangement, autotiming, calc, views
from .connectors import TimingPulse, get_connector
from .connectors.simulator import SimulatorConnector
from .models import MarshalPenalty, TimedRun, TimingEvent, TimingSettings, TimingSignal
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
    # A new competition is seeded with the default pattern; clear it, because this
    # is the free-order case — nobody has built a start order for the event.
    comp.start_pattern = []
    comp.save(update_fields=["start_pattern"])
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


def _progress_setup(practice=0, counted=2):
    """A competition with one class and one starter, for the progress tallies."""
    comp = make_active_competition()
    cclass = comp.classes.create(name="A", is_running=True, run_position=1,
                                 practice_runs=practice, counted_runs=counted)
    participant = make_participant(comp.competition_type, 1, comp)
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)
    return comp, cclass


def test_a_run_closed_with_a_state_code_counts_as_done(client):
    """A DNF is as final as a time — leaving it outstanding would peg the class at
    50 % with nothing left anyone could record."""
    comp, cclass = _progress_setup()
    TimedRun.objects.create(
        competition=comp, bib_number=1, competition_class=cclass,
        run_type=TimedRun.RunType.COUNTED, run_number=1,
        manual_run_time=Decimal("12.34"), manual_entry=True,
    )
    TimedRun.objects.create(
        competition=comp, bib_number=1, competition_class=cclass,
        run_type=TimedRun.RunType.COUNTED, run_number=2,
        status=TimedRun.Status.DNF, manual_entry=True,
    )
    data = client.get(reverse("timing:dashboard-state")).json()
    assert data["progress"] == {"expected": 2, "finished": 2, "percent": 100}
    klass = next(c for c in data["classes"] if c["name"] == "A")
    assert klass["status"] == "done"


def test_a_disqualified_competitors_undriven_runs_leave_the_total(client):
    """Their event is over, so the runs they will now never take stop being
    outstanding work — but the one they did drive stays counted on both sides."""
    comp, cclass = _progress_setup()
    TimedRun.objects.create(
        competition=comp, bib_number=1, competition_class=cclass,
        run_type=TimedRun.RunType.COUNTED, run_number=1,
        manual_run_time=Decimal("12.34"), manual_entry=True,
    )
    before = client.get(reverse("timing:dashboard-state")).json()["progress"]
    assert before == {"expected": 2, "finished": 1, "percent": 50}

    EventEntry.objects.filter(competition=comp, bib_number=1).update(
        status=EventEntry.Status.DSQ
    )
    after = client.get(reverse("timing:dashboard-state")).json()["progress"]
    assert after == {"expected": 1, "finished": 1, "percent": 100}


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
    assert rows[1]["run_time"] == "00:12.500"   # mm:ss.xxx, the one time notation


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


def test_run_status_closes_and_clears_a_run(client):
    comp = make_active_competition()
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    resp = post_json(client, "timing:run-status", run_id=run.id, status="dnf").json()
    assert resp["ok"] and resp["status"] == "dnf"
    run.refresh_from_db()
    # A state code is a statement about *this* competitor's run, so it takes the
    # row over: the positional binding must not hand it to the next starter.
    assert run.status == "dnf" and run.manual_entry
    assert post_json(client, "timing:run-status", run_id=run.id, status="").json()["ok"]
    run.refresh_from_db()
    assert run.status == ""


def test_run_status_refuses_a_code_it_does_not_know(client):
    comp = make_active_competition()
    run = run_of(signal_in(comp, 1, "10:00:00.000"))
    assert post_json(client, "timing:run-status", run_id=run.id, status="oops").status_code == 400
    run.refresh_from_db()
    assert run.status == ""


def test_run_status_makes_the_run_for_a_competitor_who_never_started(client):
    """DNS is exactly the case where nothing was ever recorded, so the endpoint
    has to build the row from the competitor's place in the start order."""
    comp = make_active_competition()
    comp.classes.filter(name="1").update(is_running=True, practice_runs=0, counted_runs=1)
    cclass = comp.classes.get(name="1")
    participant = Participant.objects.create(
        competition_type=comp.competition_type, first_name="Ada", last_name="Lovelace",
        date_of_birth=datetime.date(2010, 1, 1),
    )
    entry = EventEntry.objects.create(participant=participant, competition=comp, bib_number=7)
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)
    slot_key = autotiming.slot_key(entry.pk, cclass.pk, 0, TimedRun.RunType.COUNTED, 1)

    assert post_json(client, "timing:run-status", slot_key=slot_key, status="dns").json()["ok"]
    run = TimedRun.objects.get(competition=comp, bib_number=7)
    assert run.status == "dns" and run.start_signal_id is None
    # Asking twice reuses the row rather than opening a second one for the same run.
    post_json(client, "timing:run-status", slot_key=slot_key, status="dns")
    assert TimedRun.objects.filter(competition=comp, bib_number=7).count() == 1


def test_a_run_closed_with_a_state_code_does_not_absorb_the_next_time(client):
    """A DNS row is not a placeholder waiting for a starter — handing it the next
    competitor's time would silently rewrite the outcome."""
    comp = make_active_competition()
    marked = TimedRun.objects.create(competition=comp, manual_entry=True, status="dns")
    signal = signal_in(comp, 1, "10:00:00.000")
    marked.refresh_from_db()
    assert marked.start_signal_id is None
    assert TimedRun.objects.get(start_signal=signal).id != marked.id


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
    assert item["run_time"] == "00:10.000"
    assert item["total_time"] == "00:14.000"         # run + penalty seconds
    assert item["total_pylons"] == 2


def test_every_view_writes_a_run_time_the_same_way(client):
    """One quantity, one notation. The Manual view, the Auto view and the
    Dashboard used to render plain seconds while results rendered mm:ss.xxx, so
    the same run read three ways depending on which screen you were looking at
. They all go through calc.format_clock now; this is the pin."""
    comp, _ = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    sig = signal_in(comp, 1, "10:00:00.000", running=1)
    signal_in(comp, 2, "10:01:03.250", running=1)   # 63.250 s — over a minute
    submit_penalty(client, run_of(sig), pylons=1)   # 1 × 2 s

    manual = serialize_arrangement(comp)["rows"][0]["run"]
    auto = autotiming.serialize(comp)["items"][0]
    current = client.get(reverse("timing:dashboard-state")).json()["current"]

    assert auto["run_time"] == current["run_time"] == "01:03.250"
    assert manual["total"] == auto["total_time"] == current["total_time"] == "01:05.250"
    # A penalty is a different quantity, so it is written differently — but also
    # only one way, and in the whole seconds it is actually measured in.
    assert manual["penalty_text"] == current["penalty"] == "+2 s"


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


# ----- a claim is what authorises a marshal's write -----

def marshal_client(*page_keys):
    """A signed-in client holding only the given pages — a marshal's phone, which is
    *not* the timekeeper. The shared `client` fixture is a superuser and holds
    everything, which is why these paths looked fine."""
    from django.contrib.auth.models import Group, User
    from django.test import Client

    from apps.accounts.models import RoleAccess

    name = "marshal-" + "-".join(page_keys)
    user = User.objects.create_user(username=name, password="pw")
    group = Group.objects.create(name=name + "-role")
    RoleAccess.objects.create(group=group, pages=list(page_keys))
    user.groups.add(group)
    phone = Client()
    phone.force_login(user)
    return phone


def test_marshal_submit_needs_the_claim_for_that_post(client):
    """A device that claimed post 1 could write
    post 2's penalties, because no write path looked at claim_token."""
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    post2 = MarshalPost.objects.create(competition=comp, number=2, tasks="6-8")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))

    phone = marshal_client("marshal_posts")
    assert post_json(phone, "timing:marshal-claim", post=1, token="aaaa").json()["ok"] is True

    # Post 2 is not this device's — with the wrong token, or with none at all.
    refused = submit_penalty(phone, run, post=2, pylons=3)
    assert refused.status_code == 403
    assert post_json(phone, "timing:marshal-submit", post=2, run_id=run.id,
                     pylon_count=3, token="aaaa").status_code == 403
    assert not MarshalPenalty.objects.filter(marshal_post=post2).exists()

    # Its own post, with its own token, still works.
    ok = post_json(phone, "timing:marshal-submit", post=1, run_id=run.id,
                   pylon_count=3, submitted=True, detail={}, token="aaaa")
    assert ok.status_code == 200
    assert MarshalPenalty.objects.get(marshal_post__number=1).pylon_count == 3


def test_marshal_cannot_rewrite_a_long_finished_run(client):
    """A tap the network swallowed is retried, so a delivery may arrive a few
    starters late — but not for a run from the far side of the event."""
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    first = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    for n in range(2, views.MARSHAL_RUN_WINDOW + 3):
        latest = run_of(signal_in(comp, 1, f"10:{n:02d}:00.000", running=n))

    phone = marshal_client("marshal_posts")
    post_json(phone, "timing:marshal-claim", post=1, token="aaaa")
    stale = post_json(phone, "timing:marshal-submit", post=1, run_id=first.id,
                      pylon_count=5, token="aaaa")
    assert stale.status_code == 403
    fresh = post_json(phone, "timing:marshal-submit", post=1, run_id=latest.id,
                      pylon_count=5, submitted=True, detail={}, token="aaaa")
    assert fresh.status_code == 200

    # The timekeeper is not bounded by the window — they fix up earlier runs.
    assert submit_penalty(client, first, post=1, pylons=5).status_code == 200


@pytest.mark.parametrize("endpoint,body", [
    ("timing:marshal-unlock", {"post": 1}),
    ("timing:marshal-lock", {"post": 1}),
    ("timing:marshal-lock-all", {}),
    ("timing:marshal-task-edit", {"post": 1, "task": 2, "pylons": 4}),
])
def test_lock_unlock_and_task_edit_are_timekeeper_only(client, endpoint, body):
    """These endpoints are shared by both pages (the access gate grants them to
    either), so only the view can tell that they are the timekeeper's."""
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    submit_penalty(client, run, post=1, pylons=1, submitted=True)

    phone = marshal_client("marshal_posts")
    post_json(phone, "timing:marshal-claim", post=1, token="aaaa")
    assert post_json(phone, endpoint, run_id=run.id, **body).status_code == 403
    # Nothing moved: still locked, still one pylon.
    penalty = MarshalPenalty.objects.get(marshal_post__number=1)
    assert penalty.submitted is True and penalty.pylon_count == 1
    # And the timekeeper can still do it.
    assert post_json(client, endpoint, run_id=run.id, **body).status_code == 200


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
    # Manual view (manual_entry) must still show them in Auto timing. Ownership
    # comes from selecting the competitor — a penalty on its own never takes a run
    # over (see test_a_penalty_edit_does_not_take_over_a_run).
    comp, cls = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    post_json(client, "timing:run-update", run_id=run.id, bib_number="1")
    post_json(client, "timing:run-update", run_id=run.id, task_count=1, stopline_count=1)
    run.refresh_from_db()
    assert run.manual_entry is True
    item = autotiming.serialize(comp)["items"][0]
    assert (item["total_tasks"], item["total_stop"]) == (1, 1)
    # And a later sync (marshal posts have nothing) must not wipe the operator's entry.
    autotiming.sync_bindings(comp)
    run.refresh_from_db()
    assert (run.task_count, run.stopline_count) == (1, 1)


def test_a_penalty_edit_does_not_take_over_a_run(client):
    # Nudging a penalty stepper used to mark the run operator-owned, which pins it
    # to whichever slot it was showing and stops the positional binding moving it —
    # a side effect nobody expects from a "+" button. Only an identity edit owns.
    comp, _ = auto_scenario()
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    post_json(client, "timing:run-update", run_id=run.id, pylon_count=2)
    run.refresh_from_db()
    assert run.manual_entry is False
    assert run.pylon_count == 2


def test_marshal_mode_disables_the_steppers_that_do_nothing(client):
    # In marshal mode an auto-bound run's own counts are ignored (the posts own the
    # number), so the Manual view says so instead of accepting a value silently.
    comp, _ = auto_scenario()
    comp.penalties_by_marshal_posts = True
    comp.save(update_fields=["penalties_by_marshal_posts"])
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    row = next(r for r in serialize_arrangement(comp)["rows"] if r["run"]["id"] == run.id)
    assert row["run"]["penalties_editable"] is False
    # The operator selecting the competitor takes the run over — then they do count.
    post_json(client, "timing:run-update", run_id=run.id, bib_number="1")
    row = next(r for r in serialize_arrangement(comp)["rows"] if r["run"]["id"] == run.id)
    assert row["run"]["penalties_editable"] is True


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


# --- Live connection: the socket the operator is trusting -------------------
# Every live view used to open its own socket that neither showed
# its state nor re-fetched after an outage, so a drop left a frozen screen and
# the signals that arrived meanwhile stayed invisible. The lifecycle now lives in
# static/js/live_socket.js; these pin the parts the server owns.

LIVE_VIEW_TEMPLATES = (
    "timing/live.html",
    "timing/auto.html",
    "competitions/marshal_posts.html",
)


@pytest.mark.parametrize("name", LIVE_VIEW_TEMPLATES)
def test_live_views_show_the_connection_state(name):
    """A view that follows the event live must carry the connection indicator —
    a frozen screen is indistinguishable from a live one without it."""
    from pathlib import Path

    from django.conf import settings

    source = (Path(settings.BASE_DIR) / "templates" / name).read_text(encoding="utf-8")
    assert "timing/_live_connection.html" in source
    assert "js/live_socket.js" in source


def test_every_live_view_shares_one_socket_implementation():
    """Nobody re-opens a raw socket: the reconnect, the banner and the re-fetch
    after an outage only exist in live_socket.js, so a view that rolls its own
    quietly loses all three."""
    from pathlib import Path

    from django.conf import settings

    js_dir = Path(settings.BASE_DIR) / "static" / "js"
    for name in ("timing_live.js", "auto_timing.js", "marshal_posts.js",
                 "dashboard_overview.js"):
        source = (js_dir / name).read_text(encoding="utf-8")
        assert "new WebSocket(" not in source, f"{name} opens its own socket"
        assert "window.liveSocket(" in source, f"{name} doesn't use the shared socket"


def _live_listener(django_user_model, username):
    """A user allowed to open a live socket.

    The socket is gated on holding a page a live view is rendered on —
    a login on its own no longer gets you the event's nudges — so a test user
    needs a role, or to be a superuser.
    """
    return django_user_model.objects.create_superuser(
        username=username, email="", password="x"
    )


def _live_socket_scenario(user, messages):
    """Drive TimingLiveConsumer through a list of client messages, returning what
    it sent back."""
    from channels.testing import WebsocketCommunicator

    from .consumers import TimingLiveConsumer

    async def run():
        comm = WebsocketCommunicator(TimingLiveConsumer.as_asgi(), "/ws/timing/live/")
        comm.scope["user"] = user
        connected, _ = await comm.connect()
        assert connected
        replies = []
        for message in messages:
            await comm.send_json_to(message)
            if await comm.receive_nothing(timeout=0.2):
                replies.append(None)
            else:
                replies.append(await comm.receive_json_from())
        await comm.disconnect()
        return replies

    return async_to_sync(run)()


def test_live_socket_answers_a_heartbeat(django_user_model):
    """A socket can die without a close frame (a phone leaving Wi-Fi gets no TCP
    FIN), so the client pings and treats silence as a dead link. Without a reply
    it would tear down a perfectly good connection every 30 s."""
    user = _live_listener(django_user_model, "pinger")
    assert _live_socket_scenario(user, [{"action": "ping"}]) == [{"event": "pong"}]


def test_live_socket_ignores_anything_else_it_is_sent(django_user_model):
    """The base consumer's receive_json raises, which would drop the socket — and
    the operator's screen with it."""
    user = _live_listener(django_user_model, "babbler")
    assert _live_socket_scenario(user, [{"action": "nonsense"}, {"action": "ping"}]) == \
        [None, {"event": "pong"}]


def test_a_changed_event_reaches_the_open_views_by_name(django_user_model):
    """The other half of that: a plain refresh would have every open view quietly
    re-render as a different event, so the name travels with the nudge."""
    from channels.testing import WebsocketCommunicator

    from .consumers import TimingLiveConsumer
    from .services import notify_competition_changed

    user = _live_listener(django_user_model, "watcher")

    async def run():
        comm = WebsocketCommunicator(TimingLiveConsumer.as_asgi(), "/ws/timing/live/")
        comm.scope["user"] = user
        connected, _ = await comm.connect()
        assert connected
        await sync_to_async(notify_competition_changed)("Autumn Slalom")
        message = await comm.receive_json_from()
        await comm.disconnect()
        return message

    assert async_to_sync(run)() == {"event": "competition", "name": "Autumn Slalom"}


# --- the device connection survives a restart ------------------------
# The reader thread dies with the process, so a restart used to leave the CP540
# still selected on the settings page with nothing reading it and nobody told.
# TimingSettings.reader_enabled is the bit that outlives the process; asgi.py
# acts on it through cp540.autostart().

@pytest.fixture
def no_real_socket(monkeypatch):
    """Record what the reader was asked to do instead of opening a TCP socket."""
    from . import cp540

    calls = []
    monkeypatch.setattr(cp540.reader, "start", lambda ip, port: calls.append((ip, port)))
    monkeypatch.setattr(cp540.reader, "stop", lambda: calls.append("stop"))
    return calls


def _settings_post(client, **overrides):
    data = {"device": "cp540", "start_channel": "1", "finish_channel": "2",
            "ip_address": "192.168.1.50", "port": "7000"}
    data.update(overrides)
    return client.post(reverse("timing:settings"), data)


def test_connect_is_remembered_across_a_restart(client, no_real_socket):
    _settings_post(client, action="connect")
    assert TimingSettings.load().reader_enabled is True


def test_disconnect_is_remembered_too(client, no_real_socket):
    _settings_post(client, action="connect")
    _settings_post(client, action="disconnect")
    assert TimingSettings.load().reader_enabled is False


def test_selecting_another_device_clears_the_connection(client, no_real_socket):
    """Only one source may write at a time — a restart must not resurrect a
    reader for a device that is no longer selected."""
    _settings_post(client, action="connect")
    _settings_post(client, device="simulator", action="save")
    assert TimingSettings.load().reader_enabled is False


def test_autostart_reconnects_a_device_left_connected(no_real_socket):
    from . import cp540

    TimingSettings.objects.create(device="cp540", ip_address="10.0.0.4", port=7100,
                                  reader_enabled=True)
    assert cp540.autostart() is True
    assert no_real_socket == [("10.0.0.4", 7100)]


@pytest.mark.parametrize("fields", [
    {"device": "cp540", "reader_enabled": False},    # operator had disconnected
    {"device": "simulator", "reader_enabled": True},  # another device selected since
    {"device": "cp540", "reader_enabled": True, "ip_address": None},   # nowhere to dial
])
def test_autostart_leaves_everything_else_alone(no_real_socket, fields):
    from . import cp540

    TimingSettings.objects.create(**{"ip_address": "10.0.0.4", "port": 7100, **fields})
    assert cp540.autostart() is False
    assert no_real_socket == []


def test_autostart_never_stops_the_server_coming_up(monkeypatch):
    """A database that isn't migrated yet means no autostart — not a server that
    refuses to boot."""
    from . import cp540
    from .models import TimingSettings as Model

    monkeypatch.setattr(Model, "load", classmethod(lambda cls: 1 / 0))
    assert cp540.autostart() is False


# ----- what the live endpoints are allowed to cost -----
#
# These three are re-fetched by *every open browser* on *every* incoming time, so
# their cost is paid once per competitor crossing a beam per screen. They each used
# to grow with the field — the Manual timing table did a query per row and per
# dropdown option (293 queries at 40 starters, 1413 at 200) — which is invisible
# until an event is big enough to matter. Pinned here as the two properties that
# actually hold: the cost does not grow with the field, and a read does not write.

LIVE_ENDPOINTS = ("timing:arrangement", "timing:auto-state", "timing:dashboard-state")


def _field_of(competition, cclass, first_bib, last_bib):
    """Register bibs first_bib..last_bib and time a full run for each."""
    base = datetime.datetime(2026, 5, 1, 10, 0, 0)
    for bib in range(first_bib, last_bib + 1):
        participant = make_participant(competition.competition_type, bib, competition)
        ClassAssignment.objects.create(participant=participant, competition_class=cclass)
        offset = bib * 60
        signal_in(competition, 1, (base + datetime.timedelta(seconds=offset)).strftime("%H:%M:%S.%f")[:-3], running=bib)
        signal_in(competition, 2, (base + datetime.timedelta(seconds=offset + 35)).strftime("%H:%M:%S.%f")[:-3], running=bib)


def _query_count(client, name):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(reverse(name))
    assert response.status_code == 200
    return ctx.captured_queries


def _query_count_url(client, url):
    """_query_count for a URL that needs arguments."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return ctx.captured_queries


def _writes(queries):
    return [
        q["sql"] for q in queries
        if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
    ]


def _competition_with_field(size):
    comp = make_active_competition()
    cclass = comp.classes.get(name="1")
    cclass.is_running, cclass.run_position = True, 1
    cclass.practice_runs, cclass.counted_runs = 1, 2
    cclass.save()
    comp.start_pattern = [{"window": None, "chips": ["practice", "counted"]}]
    comp.save(update_fields=["start_pattern"])
    _field_of(comp, cclass, 1, size)
    return comp, cclass


@pytest.mark.parametrize("name", LIVE_ENDPOINTS)
def test_live_endpoint_cost_does_not_grow_with_the_field(client, name):
    comp, cclass = _competition_with_field(5)
    small = len(_query_count(client, name))
    _field_of(comp, cclass, 6, 40)          # same event, eight times the field
    large = len(_query_count(client, name))
    assert large == small, (
        f"{name} costs {small} queries for 5 starters but {large} for 40 — it is "
        f"scaling with the field again"
    )


@pytest.mark.parametrize("name", LIVE_ENDPOINTS)
def test_live_endpoints_never_write(client, name):
    """A GET must not take the write lock the timing rig needs to record a time,
    and several browsers refreshing on the same nudge must not race each other on
    the same rows. The start-order binding is applied in memory when rendering;
    only the write paths persist it (autotiming.sync_bindings)."""
    comp, cclass = _competition_with_field(6)
    # Strip the identity ingest persisted, so binding has real work to do and a
    # tempting reason to save it.
    TimedRun.objects.filter(competition=comp).update(
        bib_number=None, competition_class=None, run_type="", run_number=None)
    assert _writes(_query_count(client, name)) == []


def test_a_reading_view_still_shows_the_bound_competitor(client):
    """The other half of the rule above: not writing must not mean not binding."""
    comp, cclass = _competition_with_field(4)
    TimedRun.objects.filter(competition=comp).update(
        bib_number=None, competition_class=None, run_type="", run_number=None)
    rows = client.get(reverse("timing:arrangement")).json()["rows"]
    assert [r["run"]["bib_number"] for r in rows if r["run"]["bib_number"]]


# ----- The single-barrier phase, shown rather than guessed -----

def test_a_two_channel_rig_has_no_phase_to_show():
    comp = make_active_competition()
    assert serialize_arrangement(comp)["barrier"] is None
    assert autotiming.serialize(comp)["barrier"] is None


def test_one_light_barrier_says_what_the_next_pulse_will_be():
    # The role alternates in software, so one spurious pulse inverts it for the rest
    # of the event. Both timing pages show the phase; this is what they render.
    comp = make_active_competition()
    settings = TimingSettings.load()
    settings.start_channel = settings.finish_channel = 1
    settings.save()
    assert serialize_arrangement(comp)["barrier"]["next_role"] == "start"
    signal_in(comp, 1, "10:00:00.000", running=1)          # a run is now open
    assert serialize_arrangement(comp)["barrier"]["next_role"] == "finish"
    assert autotiming.serialize(comp)["barrier"]["next_role"] == "finish"
    signal_in(comp, 1, "10:00:30.000", running=2)          # closed again
    assert serialize_arrangement(comp)["barrier"]["next_role"] == "start"


def test_channel_outside_the_devices_inputs_is_refused(client):
    # Every supported device has inputs 1–4; 7 could never match a signal, so the
    # finish channel silently meant "nothing is ever a finish".
    response = client.post(reverse("timing:settings"), {
        "device": "simulator", "start_channel": "1", "finish_channel": "7", "ip_address": "",
    })
    assert response.status_code == 200
    assert "finish_channel" in response.context["form"].errors
    response = client.post(reverse("timing:settings"), {
        "device": "simulator", "start_channel": "0", "finish_channel": "2", "ip_address": "",
    })
    assert "start_channel" in response.context["form"].errors


# ----- An empty start order says which piece of setup is missing -----

def test_an_empty_start_order_names_what_is_missing(client):
    """Why the *order* came out empty. "No pattern" is deliberately not one of
    these — that is the page not applying at all, and it is answered before any
    of this runs (see test_auto_timing_without_a_pattern_asks_for_one)."""
    comp = make_active_competition()          # classes seeded, none running
    comp.start_pattern = [{"window": None, "chips": ["counted"]}]
    comp.save(update_fields=["start_pattern"])
    assert autotiming.serialize(comp)["empty_reason"] == "classes"

    comp.classes.filter(name="1").update(is_running=True, run_position=0)
    assert autotiming.serialize(comp)["empty_reason"] == "starters"

    comp.classes.filter(name="1").update(practice_runs=0, counted_runs=0)
    make_participant(comp.competition_type, 1, comp)
    ClassAssignment.objects.create(
        participant=Participant.objects.get(first_name="Bib"),
        competition_class=comp.classes.get(name="1"))
    assert autotiming.serialize(comp)["empty_reason"] == "runs"


def test_a_start_order_with_starters_reports_no_reason():
    comp, _ = auto_scenario()
    data = autotiming.serialize(comp)
    assert data["items"] and data["empty_reason"] == ""


# --- The one open write endpoint ------------------------------------
# timing:signal is csrf_exempt because a physical device carries no token. That
# also made it reachable cross-site by a logged-in operator's browser, with
# nothing but the session cookie's SameSite default in the way — a browser's
# choice, not ours. A device is identified by its token; a session is CSRF-checked.

class TestSignalDoor:
    def test_a_session_post_without_a_csrf_token_is_refused(self, django_user_model):
        from django.test import Client

        TimingSettings.load()
        admin = django_user_model.objects.create_superuser(
            username="op-nocsrf", email="", password="x")
        client = Client(enforce_csrf_checks=True)
        client.force_login(admin)
        response = client.post(
            reverse("timing:signal"),
            data=json.dumps({"running_number": 1, "port": 1, "time": "10:00:00.000"}),
            content_type="application/json",
        )
        assert response.status_code == 403
        assert not TimingSignal.objects.exists()

    def test_a_device_token_needs_no_csrf_token(self, settings, django_user_model):
        from django.test import Client

        settings.TIMING_DEVICE_TOKEN = "s3cret-device-token"
        TimingSettings.load()
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("timing:signal"),
            data=json.dumps({"running_number": 1, "port": 1, "time": "10:00:00.000"}),
            content_type="application/json",
            headers={"x-device-token": "s3cret-device-token"},
        )
        assert response.status_code == 200
        assert TimingSignal.objects.count() == 1

    def test_a_wrong_device_token_is_refused(self, settings):
        from django.test import Client

        settings.TIMING_DEVICE_TOKEN = "s3cret-device-token"
        settings.DEBUG = False
        TimingSettings.load()
        response = Client().post(
            reverse("timing:signal"),
            data=json.dumps({"running_number": 1, "port": 1, "time": "10:00:00.000"}),
            content_type="application/json",
            headers={"x-device-token": "wrong"},
        )
        assert response.status_code == 401
        assert not TimingSignal.objects.exists()


# --- A marshal's phone writes into a JSONField ------------------------

def test_a_marshal_detail_blob_is_bounded_and_reshaped(client):
    """`detail` arrives as free JSON from a phone, lands in a JSONField and is
    then re-downloaded by every open Auto timing page on every nudge."""
    from apps.competitions.models import MarshalPost

    ctype = CompetitionType.objects.create(
        name="Marshalled", penalties_enabled=True, pylon_penalty=5,
        task_penalty=10, stop_line_penalty=20,
    )
    competition = Competition.objects.create(
        competition_type=ctype, name="Race", date=datetime.date(2026, 5, 1),
        is_active=True, penalties_by_marshal_posts=True,
    )
    MarshalPost.objects.create(competition=competition, number=1, tasks="1")
    run = TimedRun.objects.create(competition=competition)

    huge = {
        "tasks": {str(i): {"pylons": 99999} for i in range(5000)},
        "stop_line": True,
        "junk": "x" * 100000,
    }
    client.post(reverse("timing:marshal-submit"),
                data=json.dumps({"run_id": run.id, "post": 1, "detail": huge}),
                content_type="application/json")

    stored = MarshalPenalty.objects.get(timed_run=run).detail
    assert set(stored) == {"tasks", "stop_line"}          # unknown keys dropped
    assert len(stored["tasks"]) <= 200                     # bounded
    assert all(cell["pylons"] <= 999 for cell in stored["tasks"].values())


# --- The live socket carries event news, so it needs a page ----------

def test_a_login_alone_does_not_open_the_live_socket(django_user_model):
    """The consumers checked is_authenticated and nothing else, so any
    account at all could listen in on a running event."""
    from channels.testing import WebsocketCommunicator

    from .consumers import TimingLiveConsumer

    user = django_user_model.objects.create_user(username="outsider", password="x")

    async def run():
        comm = WebsocketCommunicator(TimingLiveConsumer.as_asgi(), "/ws/timing/live/")
        comm.scope["user"] = user
        connected, _ = await comm.connect()
        if connected:
            await comm.disconnect()
        return connected

    assert async_to_sync(run)() is False


# --- Auto timing is the start order, so it needs one ------------------
# With no start pattern the page used to render its whole apparatus around an
# empty order — and once times existed, one flame-bordered "unattributed time"
# alarm per run, because every recorded run is an orphan when there are no slots
# to bind it to. Seventy alarms and nothing saying why. It now says why.

def _competition_without_a_pattern():
    ctype = CompetitionType.objects.create(name="Patternless")
    competition = Competition.objects.create(
        competition_type=ctype, name="R", date=datetime.date(2026, 5, 1),
        is_active=True,
    )
    cclass = competition.classes.first()
    cclass.is_running, cclass.run_position = True, 0
    cclass.save()
    return competition, cclass


def test_auto_timing_without_a_pattern_asks_for_one(client):
    competition, _ = _competition_without_a_pattern()
    assert competition.start_pattern_blocks() == []
    response = client.get(reverse("timing:auto"))
    assert response.status_code == 200
    assert response.context["needs_pattern"] is True
    body = response.content.decode()
    assert reverse("competitions:runorder") in body
    # …and none of the timing apparatus is on the page.
    for marker in ('id="auto-tiles"', 'id="auto-order-list"', 'id="ignored-box"',
                   'js/auto_timing.js'):
        assert marker not in body, f"{marker} is still rendered without a pattern"


def test_auto_timing_without_a_pattern_shows_no_alarms_for_recorded_runs(client):
    """The state the real database was found in: runs recorded, no pattern. Every
    one of them is an orphan, and the page used to render each as an alarm."""
    competition, _ = _competition_without_a_pattern()
    TimingSettings.load()
    for i in range(3):
        TimedRun.objects.create(
            competition=competition,
            start_signal=TimingSignal.objects.create(
                competition=competition, running_number=i + 1, port=1,
                device_time=datetime.time(10, i, 0)),
        )
    body = client.get(reverse("timing:auto")).content.decode()
    assert "auto-orphan" not in body
    assert reverse("competitions:runorder") in body


def test_a_pattern_brings_the_timing_page_back(client):
    competition, _ = _competition_without_a_pattern()
    competition.start_pattern = [{"window": None, "chips": ["counted"]}]
    competition.save(update_fields=["start_pattern"])
    response = client.get(reverse("timing:auto"))
    assert response.context["needs_pattern"] is False
    assert 'id="auto-tiles"' in response.content.decode()


def test_the_other_screens_do_not_need_a_pattern(client):
    """Manual timing, the dashboard and the results read the entries and their
    classes, never the pattern. Losing the default must not touch them."""
    competition, cclass = _competition_without_a_pattern()
    participant = make_participant(competition.competition_type, 1, competition)
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)
    for name in ("timing:manual", "timing:dashboard", "timing:arrangement",
                 "timing:dashboard-state", "results:index"):
        assert client.get(reverse(name)).status_code == 200, name
    state = client.get(reverse("timing:dashboard-state")).json()
    # The runs this competitor owes are known from their class alone.
    assert state["progress"]["expected"] == (cclass.practice_runs + cclass.counted_runs)


# --- a run that crosses midnight -------------------------------------

class TestMidnight:
    """Both device times are clock *times*, not instants, so 23:59:59 → 00:00:02
    subtracts to −86 397 s. The run simply had no time, and nobody was told which
    of the two nights it happened on. The same wrap happens on a CP540 whose
    internal clock rolls at 24 h."""

    def test_a_run_across_midnight_is_measured(self):
        assert calc.run_time(datetime.time(23, 59, 59),
                             datetime.time(0, 0, 2), 3) == Decimal("3.000")

    def test_a_run_across_midnight_keeps_its_fractions(self):
        assert calc.run_time(datetime.time(23, 59, 58, 500000),
                             datetime.time(0, 0, 1, 250000), 3) == Decimal("2.750")

    def test_a_pairing_too_far_apart_is_still_refused(self):
        """A finish from this morning on an afternoon start is a wrong pairing,
        not a wrap — it must not come back as twenty-three hours."""
        assert calc.run_time(datetime.time(15, 0, 0), datetime.time(9, 0, 0), 3) is None

    def test_an_ordinary_run_is_unaffected(self):
        assert calc.run_time(datetime.time(10, 0, 0),
                             datetime.time(10, 0, 42, 270000), 3) == Decimal("42.270")


# --- format_clock truncates, like everything else --------------------

class TestClockTruncates:
    """The app's rule is "as fast as the device fully resolved, never faster".
    format_clock used `f"{x:.3f}"`, which rounds — invisible on an already-
    truncated value, which is why it survived, but it is also handed sums and
    differences (run + penalty, gap to the winner)."""

    @pytest.mark.parametrize("value,precision,expected", [
        ("12.9996", 3, "00:12.999"),
        ("12.3456", 3, "00:12.345"),
        ("0.9999", 3, "00:00.999"),
        ("59.9999", 2, "00:59.99"),
        ("75.5", 2, "01:15.50"),
        ("0", 3, "00:00.000"),
        ("-3.4567", 3, "-00:03.456"),
    ])
    def test_it_never_rounds_up(self, value, precision, expected):
        assert calc.format_clock(Decimal(value), precision) == expected


# --- a recorded time may not vanish from under its run ---------------

def test_deleting_a_signal_a_run_uses_is_refused():
    """SET_NULL silently blanked the run's time: the row stayed, the measurement
    went, and nothing said so."""
    from django.db.models import RestrictedError

    ctype = CompetitionType.objects.create(name="Protected")
    competition = Competition.objects.create(
        competition_type=ctype, name="R", date=datetime.date(2026, 5, 1), is_active=True)
    signal = TimingSignal.objects.create(
        competition=competition, running_number=1, port=1,
        device_time=datetime.time(10, 0, 0))
    run = TimedRun.objects.create(competition=competition, start_signal=signal)
    with pytest.raises(RestrictedError):
        signal.delete()
    run.refresh_from_db()
    assert run.start_signal_id == signal.id


def test_deleting_the_whole_competition_still_works():
    """RESTRICT rather than PROTECT precisely so this case survives: both tables
    cascade from the competition, and that has to keep working."""
    ctype = CompetitionType.objects.create(name="Cascade")
    competition = Competition.objects.create(
        competition_type=ctype, name="R", date=datetime.date(2026, 5, 1))
    signal = TimingSignal.objects.create(
        competition=competition, running_number=1, port=1,
        device_time=datetime.time(10, 0, 0))
    TimedRun.objects.create(competition=competition, start_signal=signal)
    competition.delete()
    assert not TimingSignal.objects.filter(pk=signal.pk).exists()


# --- a reorder may not repeat a slot --------------------------------

def test_a_reorder_drops_repeats(client):
    """A key twice over puts one competitor in two places; ordered_slots resolves
    that by silently dropping the second, so the saved order isn't what was sent."""
    competition, _cclass = _competition_with_field(3)
    keys = [slot["key"] for slot in autotiming.computed_slots(competition)]
    assert len(keys) >= 2
    client.post(reverse("timing:auto-reorder"),
                data=json.dumps({"order": [keys[1], keys[0], keys[1], "not-a-key"]}),
                content_type="application/json")
    competition.refresh_from_db()
    assert competition.auto_timing_order == [keys[1], keys[0]]


# --- the rule held for *manual* assignment only ----------------------
# AgeAssignment resolves a participant's class by walking the running classes, and
# it asked for them inside the loop over the field: 34 / 64 / 114 queries at 20 /
# 50 / 100 starters, against a flat 15 for manual. Re-paid by every open browser
# on every incoming time. The test above only ever exercised manual assignment,
# which is why nothing noticed.

def _age_based_field(size):
    comp = make_active_competition()
    comp.assignment_method = "age"
    comp.start_pattern = [{"window": None, "chips": ["practice", "counted"]}]
    comp.save(update_fields=["assignment_method", "start_pattern"])
    cclass = comp.classes.get(name="1")
    cclass.is_running, cclass.run_position = True, 1
    cclass.practice_runs, cclass.counted_runs = 1, 2
    cclass.age_from, cclass.age_to = 0, 99
    cclass.save()
    for bib in range(1, size + 1):
        make_participant(comp.competition_type, bib, comp)
    return comp


@pytest.mark.parametrize("name", LIVE_ENDPOINTS)
def test_live_endpoint_cost_is_flat_under_age_assignment(client, name):
    _age_based_field(5)
    small = len(_query_count(client, name))
    comp = Competition.get_current()
    for bib in range(6, 46):
        make_participant(comp.competition_type, bib, comp)
    large = len(_query_count(client, name))
    # Must not *grow*. Not "must be equal": a five-starter event takes a couple of
    # conditional branches a full one doesn't, so the small case can legitimately
    # cost slightly more. What matters is that nine times the field is not nine
    # times the queries — it used to be 34 / 64 / 114 at 20 / 50 / 100.
    assert large <= small, (
        f"{name} costs {small} queries for 5 age-assigned starters and {large} "
        f"for 45 — the class lookup is back inside the loop over the field"
    )


def test_resolving_a_whole_field_reads_the_classes_once(django_assert_num_queries):
    """Where the cost actually was: starters_by_class walks the field and asked
    the assignment method for each participant's classes, and the age method
    answered by re-reading the running classes every time."""
    comp = _age_based_field(30)
    comp = Competition.objects.get(pk=comp.pk)
    with django_assert_num_queries(3):     # classes, entries, assignments prefetch
        comp.starters_by_class()


# --- "export everything" re-read the event once per class ------------

def test_export_all_does_not_re_read_the_event_per_class(client):
    """RunIndex exists precisely so an event's runs are read once. export-all
    built one per section, along with the entries, the starters and the column
    vocabulary: 42 queries for one class and 137 for six."""
    comp = make_active_competition()
    comp.start_pattern = [{"window": None, "chips": ["practice", "counted"]}]
    comp.save(update_fields=["start_pattern"])
    for i, name in enumerate(["1", "2", "3", "4", "5", "6"]):
        cc = comp.classes.get(name=name)
        cc.is_running, cc.run_position = True, i
        cc.practice_runs, cc.counted_runs = 1, 2
        cc.save()
    first = comp.classes.get(name="1")
    for bib in range(1, 11):
        participant = make_participant(comp.competition_type, bib, comp)
        ClassAssignment.objects.create(participant=participant, competition_class=first)

    one = len(_query_count_url(
        client, reverse("results:export-class", args=[first.pk])))
    everything = len(_query_count_url(client, reverse("results:export-all")))
    # Six classes plus the Overall tables, for well under twice one class.
    assert everything < one * 2, (
        f"one class costs {one} queries and all six cost {everything} — the "
        f"event is being re-read per table"
    )


# --- the live path does not want the device log ----------------------

def test_the_device_link_check_does_not_copy_the_log():
    """link_state is asked on every live refresh of both timing pages, by every
    open browser. It used to call snapshot(), which copies the whole 400-entry
    ring buffer to read two fields out of it."""
    from apps.timing import cp540

    for i in range(50):
        cp540.reader._add_log(f"line {i}")
    state = cp540.reader.state()
    assert "log" not in state
    assert "status" in state and "error" in state
    assert len(cp540.reader.snapshot()["log"]) == 50   # still there for the settings page


def test_the_device_log_survives_being_read_while_written():
    """`list(deque)` raises RuntimeError if the deque is mutated while it
    iterates, and the reader thread appends whenever the rig fires."""
    import threading

    from apps.timing import cp540

    stop = threading.Event()

    def writer():
        while not stop.is_set():
            cp540.reader._add_log("from the rig")

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            cp540.reader.snapshot()          # would raise without the lock
    finally:
        stop.set()
        thread.join(timeout=2)


# --- reconcile scaled its own SQL with the event ---------------------

def test_reconcile_does_not_send_every_placed_signal_back(client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    """It pulled every placed signal id into Python and sent them all again as an
    IN-list — two ids per run, so 1200 parameters at 600 runs — on the timing
    rig's own thread, for every incoming signal."""
    comp, _cclass = _competition_with_field(20)
    settings_row = TimingSettings.load()
    with CaptureQueriesContext(connection) as ctx:
        arrangement.reconcile(comp, settings_row)
    for query in ctx.captured_queries:
        assert query["sql"].count("%s") < 50 and query["sql"].count("?") < 50, (
            "reconcile is building an IN-list that grows with the event"
        )


# --- the marshal boxes are sent once, not once per slot --------------
# Every field of a post's box is derived from the *post* until somebody records
# something against the run: the number, the tasks it watches, whether it judges
# the stop line. So for a field of 200 it was the same object written out 600
# times — at four posts watching six tasks each, 958 KiB of a 1.34 MiB payload,
# re-downloaded by every open browser on every incoming time.
#
# The wire changed; the screen must not have. These tests are about that: what the
# page reconstructs (`item.marshals || state.posts`, see auto_timing.js) has to
# equal what the old payload put in `item.marshals`, item for item, in every
# configuration.

def _marshal_competition(posts_spec, starters=4):
    """A marshal-mode competition. `posts_spec` is [(number, tasks, stop_line)]."""
    comp = make_active_competition()
    comp.penalties_by_marshal_posts = True
    comp.start_pattern = [{"window": None, "chips": ["practice", "counted"]}]
    comp.save(update_fields=["penalties_by_marshal_posts", "start_pattern"])
    cclass = comp.classes.get(name="1")
    cclass.is_running, cclass.run_position = True, 1
    cclass.practice_runs, cclass.counted_runs = 1, 1
    cclass.save()
    for bib in range(1, starters + 1):
        participant = make_participant(comp.competition_type, bib, comp)
        ClassAssignment.objects.create(participant=participant, competition_class=cclass)
    for number, tasks, stop_line in posts_spec:
        MarshalPost.objects.create(competition=comp, number=number, tasks=tasks,
                                   handles_stop_line=stop_line)
    return comp, cclass


def _as_the_page_sees_it(state):
    """What auto_timing.js renders per item: its own boxes, or the shared blank."""
    return [item["marshals"] or state["posts"] for item in state["items"]]


def _boxes_the_old_way(competition, state):
    """What `marshals` held before the trim: _marshals() for every item."""
    posts = list(competition.marshal_posts.all())
    runs = {r.id: r for r in autotiming.all_runs(competition)}
    out = []
    for item in state["items"]:
        run = runs.get(item["run_id"])
        stored = ({mp.marshal_post_id: mp for mp in run.marshal_penalties.all()}
                  if run else {})
        out.append(autotiming._marshals(run, posts, stored))
    return out


@pytest.mark.parametrize("posts_spec", [
    [],                                              # no posts at all
    [(1, "1-3", True)],                              # one post, stop line
    [(1, "1-3", True), (2, "4-6", False)],           # two, different tasks
    [(1, "", True), (2, "1", False), (3, "2-9", False)],   # one watching nothing
])
def test_the_page_reconstructs_exactly_the_boxes_it_used_to_be_sent(client, posts_spec):
    comp, _cclass = _marshal_competition(posts_spec)
    signal_in(comp, 1, "10:00:00.000", running=1)
    signal_in(comp, 2, "10:00:42.000", running=2)
    signal_in(comp, 1, "10:01:00.000", running=3)

    state = autotiming.serialize(comp)
    assert _as_the_page_sees_it(state) == _boxes_the_old_way(comp, state)


def test_the_reconstruction_holds_once_marshals_have_recorded_things(client):
    """The interesting half: some runs have penalties, some don't, one post has
    submitted and another hasn't, and there is per-task detail to resume from."""
    comp, _cclass = _marshal_competition([(1, "1-3", True), (2, "4-6", False)])
    for pair in range(3):
        signal_in(comp, 1, f"10:0{pair}:00.000", running=pair * 2 + 1)
        signal_in(comp, 2, f"10:0{pair}:42.000", running=pair * 2 + 2)

    runs = list(TimedRun.objects.filter(competition=comp).order_by("id"))
    assert len(runs) >= 3
    posts = list(comp.marshal_posts.all())
    # One run judged by both posts, one by a single post, the rest untouched.
    MarshalPenalty.objects.create(
        timed_run=runs[0], marshal_post=posts[0], pylon_count=2, task_count=1,
        stopline_count=1, submitted=True,
        detail={"tasks": {"1": {"pylons": 2}, "2": {"task": True}}, "stop_line": True})
    MarshalPenalty.objects.create(
        timed_run=runs[0], marshal_post=posts[1], pylon_count=0, submitted=False,
        detail={"tasks": {}, "stop_line": False})
    MarshalPenalty.objects.create(
        timed_run=runs[1], marshal_post=posts[1], pylon_count=3, submitted=True,
        detail={"tasks": {"5": {"pylons": 3}}, "stop_line": False})

    state = autotiming.serialize(comp)
    assert _as_the_page_sees_it(state) == _boxes_the_old_way(comp, state)

    # …and the runs that were judged carry their own boxes rather than the blank.
    by_run = {item["run_id"]: item for item in state["items"] if item["run_id"]}
    assert by_run[runs[0].id]["marshals"] is not None
    assert by_run[runs[0].id]["marshals"][0]["pylons"] == 2
    assert by_run[runs[0].id]["marshals"][0]["submitted"] is True
    assert by_run[runs[1].id]["marshals"][1]["pylons"] == 3
    # Whatever else got a run, nothing was recorded against it, so it sends null.
    judged = {runs[0].id, runs[1].id}
    untouched = [i for rid, i in by_run.items() if rid not in judged]
    assert untouched, "the scenario needs a run nobody judged"
    assert all(item["marshals"] is None for item in untouched)


def test_a_partly_judged_run_still_ships_every_post(client):
    """Post 1 recorded something, post 2 didn't: the run must still carry a box
    for *both*, or the second post's box would silently disappear from the tile."""
    comp, _cclass = _marshal_competition([(1, "1-3", True), (2, "4-6", False)])
    signal_in(comp, 1, "10:00:00.000", running=1)
    run = TimedRun.objects.filter(competition=comp).first()
    MarshalPenalty.objects.create(
        timed_run=run, marshal_post=comp.marshal_posts.first(), pylon_count=1)

    state = autotiming.serialize(comp)
    boxes = next(i["marshals"] for i in state["items"] if i["run_id"] == run.id)
    assert [b["number"] for b in boxes] == [1, 2]
    assert boxes[0]["entered"] is True and boxes[1]["entered"] is False


def test_the_blank_template_is_a_real_box_not_a_stub(client):
    """`posts` used to be [{number}] and is now the whole blank box, because the
    page renders it. It must carry the tasks each post watches and its stop-line
    flag, or an upcoming competitor's tile would come out empty."""
    comp, _cclass = _marshal_competition([(1, "1-3", True), (2, "4-6", False)])
    state = autotiming.serialize(comp)
    first, second = state["posts"]
    assert first["number"] == 1 and second["number"] == 2
    assert [row["task"] for row in first["detail"]["tasks"]] == [1, 2, 3]
    assert [row["task"] for row in second["detail"]["tasks"]] == [4, 5, 6]
    assert first["detail"]["handles_stop_line"] is True
    assert second["detail"]["handles_stop_line"] is False
    assert first["pylons"] == 0 and first["entered"] is False


def test_with_no_posts_there_is_nothing_to_render(client):
    """The page guards on `state.posts.length`, so the empty case has to stay
    empty rather than becoming a list of nothing."""
    comp, _cclass = _marshal_competition([])
    signal_in(comp, 1, "10:00:00.000", running=1)
    state = autotiming.serialize(comp)
    assert state["posts"] == []
    assert all(item["marshals"] is None for item in state["items"])


def test_the_payload_no_longer_grows_with_the_number_of_posts(client):
    """What the trim was for: `marshals` was 94% of a 1.34 MiB payload at four
    posts, and every open browser re-downloaded it on every incoming time."""
    import json as _json

    from django.core.serializers.json import DjangoJSONEncoder

    sizes = {}
    for count in (0, 4):
        # The type is PROTECTed by its competitions *and* its participants, and
        # its name is unique — so the second pass needs all three gone.
        Competition.objects.all().delete()
        Participant.objects.all().delete()
        CompetitionType.objects.all().delete()
        spec = [(n, "1-6", n == 1) for n in range(1, count + 1)]
        comp, _cclass = _marshal_competition(spec, starters=25)
        for bib in range(1, 11):
            signal_in(comp, 1, f"10:{bib:02d}:00.000", running=bib * 2 - 1)
            signal_in(comp, 2, f"10:{bib:02d}:42.000", running=bib * 2)
        # The penalty labels are lazy translation proxies, as they are on the wire.
        sizes[count] = len(_json.dumps(autotiming.serialize(comp),
                                       cls=DjangoJSONEncoder))
    assert sizes[4] < sizes[0] * 1.1, (
        f"four posts cost {sizes[4]} bytes against {sizes[0]} with none — the "
        f"boxes are being written out per item again"
    )


# --- a login is not authorisation ------------------------------------
#
# The reason this reads as a sweep rather than a case:
# the shared `client` fixture is a **superuser**, so almost every view test in
# this suite proves nothing whatever about access control. Four endpoints are
# reachable from two pages at once (the marshal endpoints), and one is reachable
# from outside the gate entirely (timing:signal), which means each has to decide
# for itself who is calling. Each of those decisions needs a test that a
# *scoped* role is refused — and it needs to be a sweep, because the failure
# mode is the fifth endpoint somebody adds next to the four.

# Endpoint -> a payload that would otherwise do something. Every one of these is
# a timekeeper's action offered on a page a marshal can also open.
TIMEKEEPER_ONLY = {
    "timing:marshal-unlock": {"post": 1, "run_id": None},
    "timing:marshal-lock": {"post": 1, "run_id": None},
    "timing:marshal-lock-all": {"run_id": None},
    "timing:marshal-task-edit": {"post": 1, "run_id": None, "task": 1, "pylons": 2},
}


@pytest.mark.parametrize("endpoint", sorted(TIMEKEEPER_ONLY))
def test_a_marshal_cannot_do_a_timekeepers_job(endpoint):
    """Marshal Posts and Timing grant the same URLs, so the access gate cannot
    tell the two roles apart — these views draw the line themselves."""
    comp, _ = auto_scenario()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-5")
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))

    body = dict(TIMEKEEPER_ONLY[endpoint], run_id=run.id)
    phone = marshal_client("marshal_posts")
    assert post_json(phone, endpoint, **body).status_code == 403, endpoint

    # …and the timekeeper, holding the Timing page, is not refused.
    desk = marshal_client("timing")
    assert post_json(desk, endpoint, **body).status_code != 403, endpoint


def test_every_timekeeper_endpoint_is_covered_by_the_sweep():
    """The list above is only worth having if it is the whole list. Every view
    that calls _timekeeper_required has to appear in it, so adding a fifth
    without a test fails here rather than in a year."""
    import inspect

    from apps.timing import views

    guarded = {
        name for name, fn in vars(views).items()
        if inspect.isfunction(fn) and fn.__module__ == views.__name__
        and name != "_timekeeper_required"          # the guard itself
        and "_timekeeper_required" in inspect.getsource(fn)
    }
    # url name -> view function name, for the ones the sweep covers.
    covered = {resolve(reverse(name)).func.__name__ for name in TIMEKEEPER_ONLY}
    assert guarded <= covered, f"not swept: {sorted(guarded - covered)}"


def test_the_open_signal_endpoint_still_asks_who_is_calling():
    """timing:signal is the one ungated URL (a device cannot log in), so it
    authorises itself: a *session*-authenticated caller must hold the Timing
    page. A signed-in user without it is refused, not served."""
    comp, _ = auto_scenario()
    settings_row = TimingSettings.load()
    settings_row.device = TimingSettings.Device.SIMULATOR
    settings_row.save(update_fields=["device"])

    body = {"running_number": 1, "port": 1, "time": "10:00:00.000"}
    phone = marshal_client("marshal_posts")
    assert post_json(phone, "timing:signal", **body).status_code == 403
    assert not TimingSignal.objects.exists()

    desk = marshal_client("timing")
    assert post_json(desk, "timing:signal", **body).status_code == 200
    assert TimingSignal.objects.count() == 1


# --- no start pattern, but runs already recorded ----------------------

def test_recorded_runs_survive_a_competition_with_no_start_pattern(client):
    """The state the real database is in, and the one nobody had a test for.

    A pattern is optional: an event can be timed entirely on the Manual
    view, and Auto timing then replaces itself with a sentence. What must not
    happen is that the *runs* become unreachable — they are the event. So with
    times recorded and no pattern at all, the Manual view, the results and the
    Dashboard all still have to answer.
    """
    comp, cls = auto_scenario()
    comp.start_pattern = []
    comp.save(update_fields=["start_pattern"])
    run = run_of(signal_in(comp, 1, "10:00:00.000", running=1))
    signal_in(comp, 1, "10:00:42.500", running=2)

    # Auto timing says what is missing and renders none of its apparatus…
    auto = client.get(reverse("timing:auto"))
    assert auto.context["needs_pattern"] is True
    assert b'id="autotiming"' not in auto.content

    # …while everything that reads the runs is unaffected.
    assert client.get(reverse("timing:manual")).status_code == 200
    arrangement = client.get(reverse("timing:arrangement")).json()
    assert any(row.get("run", {}).get("id") == run.id for row in arrangement["rows"])
    assert client.get(reverse("timing:dashboard")).status_code == 200
    assert client.get(reverse("results:class", args=[cls.pk])).status_code == 200


def test_a_pattern_can_be_added_after_times_are_recorded(client):
    """The other half: an operator who starts on Manual and switches to Auto
    mid-event. The runs already recorded must bind to the new order, not be
    stranded beside it."""
    comp, _ = auto_scenario()
    comp.start_pattern = []
    comp.save(update_fields=["start_pattern"])
    signal_in(comp, 1, "10:00:00.000", running=1)

    comp.start_pattern = [{"window": None, "chips": ["counted"]}]
    comp.save(update_fields=["start_pattern"])

    state = client.get(reverse("timing:auto-state")).json()
    assert state["items"], "the start order came back empty"
    assert any(item.get("run_id") for item in state["items"]), (
        "a run recorded before the pattern existed did not bind to it"
    )


# --- the gate itself, from a role that should not get through ----------------
#
# The sweep above covers the endpoints two pages share. These two cover the other
# shape: a page key that grants nothing here at all, and the one URL that sits
# outside the gate entirely.

def test_a_results_only_role_cannot_read_the_live_arrangement():
    comp, _ = auto_scenario()
    reader = marshal_client("results")
    assert reader.get(reverse("timing:arrangement")).status_code == 403
    assert reader.get(reverse("timing:auto-state")).status_code == 403


def test_a_participants_only_role_cannot_inject_a_timing_signal():
    """timing:signal is pages.OPEN — a device cannot log in, so the gate lets the
    URL through and the view authorises the caller itself. A session that holds
    only the Participants page is a person, not a device, and must be refused."""
    comp, _ = auto_scenario()
    TimingSettings.load()
    desk = marshal_client("participants")

    response = post_json(desk, "timing:signal",
                         running_number=1, port=1, time="10:00:00.000")

    assert response.status_code == 403
    assert not TimingSignal.objects.filter(running_number=1, port=1).exists()
