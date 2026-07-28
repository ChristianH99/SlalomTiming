"""Audit matrix — exercise the app across the configuration space an organiser
can actually reach: precisions, scoring methods, penalty modes, assignment
methods, barrier setups, languages and participant-set sizes.

NOT part of the shipped suite. A failure here is a configuration that is legal
to set up and does not work.
"""
import datetime
import itertools
import json

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.results import resultscalc
from apps.timing import autotiming
from apps.timing.ingest import record_signal
from apps.timing.models import TimedRun, TimingSettings, TimingSignal

pytestmark = pytest.mark.django_db


def build(precision=2, penalties=True, marshal=False, scoring="aggregate",
          assignment="manual", tie_break="fastest_run", starters=4,
          practice=1, counted=2, posts=0):
    ctype = CompetitionType.objects.create(
        name=f"T{precision}{scoring}{assignment}{tie_break}{penalties}{marshal}",
        penalties_enabled=penalties, pylon_penalty=5, task_penalty=10,
        stop_line_penalty=20, max_penalty_per_task=30,
        timing_precision=precision, tie_break=tie_break,
    )
    comp = Competition.objects.create(
        competition_type=ctype, name="Matrix", date=datetime.date(2026, 7, 1),
        is_active=True, assignment_method=assignment,
        penalties_by_marshal_posts=marshal,
        # A competition has no start pattern by default (§3, INT-1) and Auto
        # timing then asks for one instead of showing an order. The matrix is
        # about a *configured* event, so it configures one.
        start_pattern=[{"window": None, "chips": ["practice", "counted", "counted"]}],
    )
    cc = comp.classes.get(name="1")
    cc.is_running = True
    cc.run_position = 0
    cc.practice_runs = practice
    cc.counted_runs = counted
    cc.scoring_method = scoring
    cc.age_from, cc.age_to = 0, 99
    cc.save()
    for i in range(1, starters + 1):
        p = Participant.objects.create(
            competition_type=ctype, first_name=f"F{i}", last_name=f"L{i}",
            date_of_birth=datetime.date(2000, 1, 1), club=f"Club {i}",
            email=f"f{i}@example.com", phone_number="+49 170 1", vehicle="VW",
            license_number=f"L{i}", address_street="Str 1",
            address_zip_code="10000", address_city="Ort",
        )
        EventEntry.objects.create(participant=p, competition=comp, bib_number=i)
        if assignment == "manual":
            ClassAssignment.objects.create(participant=p, competition_class=cc)
    for n in range(1, posts + 1):
        MarshalPost.objects.create(competition=comp, number=n, tasks=str(n),
                                   handles_stop_line=(n == 1))
    return comp, cc


def drive(comp, start_channel=1, finish_channel=2, count=8, gap=10):
    """Fire `count` start/finish pairs through the ingest door."""
    s = TimingSettings.load()
    s.start_channel, s.finish_channel = start_channel, finish_channel
    s.device = TimingSettings.Device.SIMULATOR
    s.save()
    t = datetime.datetime(2026, 7, 1, 10, 0, 0)
    for i in range(count):
        record_signal(i * 2 + 1, start_channel, False, t.time())
        t += datetime.timedelta(seconds=gap)
        record_signal(i * 2 + 2, finish_channel, False, t.time())
        t += datetime.timedelta(seconds=5)


LIVE_PAGES = ["timing:dashboard", "timing:manual", "timing:auto",
              "competitions:marshal-posts", "results:index"]
LIVE_JSON = ["timing:dashboard-state", "timing:arrangement", "timing:auto-state"]


# ------------------------------------------------------------------ matrix

@pytest.mark.parametrize("precision", [1, 2, 3])
@pytest.mark.parametrize("scoring", ["aggregate", "best_run", "regularity"])
def test_every_precision_and_scoring_renders_and_ranks(client, precision, scoring):
    comp, cc = build(precision=precision, scoring=scoring, starters=3, counted=2)
    drive(comp, count=6)
    for name in LIVE_PAGES:
        assert client.get(reverse(name)).status_code == 200, name
    for name in LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name
    assert client.get(reverse("results:class", args=[cc.pk])).status_code == 200
    assert client.get(reverse("results:export-class", args=[cc.pk])).status_code == 200
    assert client.get(reverse("results:export-all")).status_code == 200


@pytest.mark.parametrize("penalties,marshal,posts", [
    (True, False, 0), (True, True, 3), (False, False, 0), (False, True, 2),
])
def test_every_penalty_mode_renders(client, penalties, marshal, posts):
    comp, cc = build(penalties=penalties, marshal=marshal, posts=posts, starters=3)
    drive(comp, count=4)
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name
    assert client.get(reverse("results:export-all")).status_code == 200


@pytest.mark.parametrize("assignment", ["manual", "age"])
@pytest.mark.parametrize("starters", [0, 1, 25])
def test_every_assignment_and_field_size_renders(client, assignment, starters):
    comp, cc = build(assignment=assignment, starters=starters)
    drive(comp, count=min(starters, 3))
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, f"{name} {assignment} {starters}"


@pytest.mark.parametrize("lang", ["de", "en"])
def test_every_page_renders_in_every_language(client, lang, settings):
    settings.LANGUAGE_CODE = lang
    comp, cc = build(marshal=True, posts=2, starters=3)
    drive(comp, count=4)
    with translation.override(lang):
        for name in LIVE_PAGES + LIVE_JSON + [
            "competitions:list", "competitions:general", "competitions:classes",
            "competitions:runorder", "competitions:penalties", "competitions:type-list",
            "participants:list", "participants:add", "results:settings",
            "transfer:export", "transfer:import", "timing:settings", "timing:simulator",
        ]:
            assert client.get(reverse(name)).status_code == 200, f"{name} [{lang}]"
        assert client.get(reverse("results:class", args=[cc.pk])).status_code == 200
        assert client.get(reverse("results:export-all")).status_code == 200


@pytest.mark.parametrize("device", ["simulator", "cp540"])
def test_both_devices_render_the_timing_pages(client, device):
    comp, cc = build(starters=2)
    s = TimingSettings.load()
    s.device = device
    s.save()
    for name in ["timing:manual", "timing:auto", "timing:settings",
                 "timing:arrangement", "timing:auto-state"]:
        assert client.get(reverse(name)).status_code == 200, f"{name} [{device}]"


def test_a_single_light_barrier_alternates_start_and_finish(client):
    comp, cc = build(starters=2)
    drive(comp, start_channel=1, finish_channel=1, count=3)
    runs = list(TimedRun.objects.filter(competition=comp))
    # 3 "pairs" fired on one channel = 6 pulses = 3 complete runs.
    complete = [r for r in runs if r.start_signal_id and r.finish_signal_id]
    assert len(complete) == 3, f"{len(runs)} runs, {len(complete)} complete"
    state = client.get(reverse("timing:auto-state")).json()
    assert state["barrier"] is not None
    assert state["ignored_split"] is False


def test_a_single_barrier_recovers_its_phase_after_a_stray_pulse(client):
    """One stray pulse inverts the phase for the rest of the event; the documented
    remedy is to ignore the stray time, which should put it back."""
    comp, cc = build(starters=2)
    s = TimingSettings.load()
    s.start_channel = s.finish_channel = 1
    s.save()
    t = datetime.datetime(2026, 7, 1, 10, 0, 0)
    stray = record_signal(99, 1, False, t.time())          # stray: opens a run
    before = client.get(reverse("timing:auto-state")).json()["barrier"]["next_role"]
    assert before == "finish"
    client.post(reverse("timing:ignore"),
                data=json.dumps({"signal_id": stray.id, "ignored": True}),
                content_type="application/json")
    after = client.get(reverse("timing:auto-state")).json()["barrier"]["next_role"]
    assert after == "start", "ignoring the stray time did not restore the phase"


# ------------------------------------------------------------- edge cases

def test_a_class_with_zero_counted_runs_says_so_and_does_not_crash(client):
    comp, cc = build(counted=0, starters=2)
    assert cc.scoring_warning()
    assert client.get(reverse("results:class", args=[cc.pk])).status_code == 200
    assert client.get(reverse("results:export-class", args=[cc.pk])).status_code == 200


def test_a_regularity_class_over_one_run_says_so(client):
    comp, cc = build(scoring="regularity", counted=1, starters=2)
    assert cc.scoring_warning()
    assert client.get(reverse("results:class", args=[cc.pk])).status_code == 200


def test_a_competition_with_no_running_class_renders_every_page(client):
    comp, cc = build(starters=2)
    cc.is_running = False
    cc.save()
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name


def test_a_participant_with_no_date_of_birth_under_age_assignment(client):
    """date_of_birth is mandatory at the DB level, but an age-assigned competitor
    outside every class range simply has no class — the pages must still work."""
    comp, cc = build(assignment="age", starters=0)
    cc.age_from, cc.age_to = 5, 6
    cc.save()
    p = Participant.objects.create(
        competition_type=comp.competition_type, first_name="Old", last_name="Timer",
        date_of_birth=datetime.date(1950, 1, 1))
    EventEntry.objects.create(participant=p, competition=comp, bib_number=1)
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name


def test_a_far_future_and_far_past_date_of_birth_are_refused(client):
    """Nothing validates the range, so a typo puts a 3000-born competitor in the
    field and gives every age-based class a negative age."""
    comp, cc = build(starters=0)
    resp = client.post(reverse("participants:add"), {
        "first_name": "Time", "last_name": "Traveller",
        "date_of_birth": "3000-01-01", "club": "X", "license_number": "1",
        "email": "a@b.de", "address_street": "S", "address_zip_code": "1",
        "address_city": "C",
    })
    assert not Participant.objects.filter(last_name="Traveller").exists(), \
        "a date of birth in the year 3000 was accepted"


def test_a_bib_of_zero_or_negative_is_refused(client):
    comp, cc = build(starters=1)
    p = Participant.objects.get(first_name="F1")
    resp = client.post(reverse("participants:set-bib"),
                       data=json.dumps({"participant": p.pk, "bib": "-5"}),
                       content_type="application/json")
    assert resp.json().get("ok") is False


def test_more_starts_than_the_order_expects_are_surfaced_not_lost(client):
    comp, cc = build(starters=1, practice=0, counted=1)
    drive(comp, count=3)                       # 3 runs, order has 1 slot
    state = client.get(reverse("timing:auto-state")).json()
    orphans = [i for i in state["items"] if i["orphan"]]
    assert len(orphans) == 2, f"{len(orphans)} orphans surfaced of 2 extra runs"


def test_an_operator_typed_run_time_beyond_a_day_is_refused(client):
    comp, cc = build(starters=1)
    run = TimedRun.objects.create(competition=comp)
    resp = client.post(reverse("timing:set-runtime"),
                       data=json.dumps({"run_id": run.id, "run_time": "999999"}),
                       content_type="application/json")
    run.refresh_from_db()
    assert run.manual_run_time is None


def test_deleting_a_class_mid_event_leaves_the_pages_working(client):
    comp, cc = build(starters=3)
    drive(comp, count=4)
    cc.delete()
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name


def test_deleting_a_participant_mid_event_leaves_the_pages_working(client):
    comp, cc = build(starters=3)
    drive(comp, count=4)
    Participant.objects.filter(first_name="F1").delete()
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name
    assert client.get(reverse("results:export-all")).status_code == 200


def test_switching_the_active_competition_mid_event_leaves_the_pages_working(client):
    comp, cc = build(starters=2)
    drive(comp, count=2)
    other = Competition.objects.create(
        competition_type=comp.competition_type, name="Other",
        date=datetime.date(2026, 8, 1))
    client.post(reverse("competitions:select", args=[other.pk]), {"confirm_switch": "1"})
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name


def test_a_competitor_entered_in_two_classes_appears_once_per_class(client):
    comp, cc = build(starters=2)
    comp.allow_multiple_classes = True
    comp.save()
    cc2 = comp.classes.get(name="2")
    cc2.is_running = True
    cc2.run_position = 1
    cc2.save()
    p = Participant.objects.get(first_name="F1")
    ClassAssignment.objects.create(participant=p, competition_class=cc2)
    slots = autotiming.computed_slots(comp)
    keys = [s["key"] for s in slots]
    assert len(keys) == len(set(keys)), "duplicate slot keys in the start order"
    for name in LIVE_PAGES + LIVE_JSON:
        assert client.get(reverse(name)).status_code == 200, name


def test_two_hundred_starters_render_within_a_sane_query_count(client, django_assert_max_num_queries):
    comp, cc = build(starters=200)
    drive(comp, count=20)
    with django_assert_max_num_queries(40):
        assert client.get(reverse("timing:arrangement")).status_code == 200
    with django_assert_max_num_queries(40):
        assert client.get(reverse("timing:auto-state")).status_code == 200
    with django_assert_max_num_queries(40):
        assert client.get(reverse("timing:dashboard-state")).status_code == 200


def test_export_all_does_not_reread_every_run_once_per_class(client, django_assert_max_num_queries):
    comp, cc = build(starters=30)
    for name in ["2", "3", "4", "5", "6"]:
        other = comp.classes.get(name=name)
        other.is_running = True
        other.run_position = 1
        other.save()
    drive(comp, count=10)
    with django_assert_max_num_queries(80):
        assert client.get(reverse("results:export-all")).status_code == 200
