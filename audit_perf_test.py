"""Audit §5 measurements — prints query counts and payload sizes.

Not assertions: numbers, so a fix can be shown to have moved them. Run with -s.
"""
import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.competitions.models import Competition, CompetitionClass, CompetitionType
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.timing.models import TimingSettings
from apps.timing.ingest import record_signal

pytestmark = pytest.mark.django_db

PATTERN = [{"window": None, "chips": ["practice", "counted", "counted"]}]


def build(starters, assignment="manual", classes=1, name="Perf"):
    ctype = CompetitionType.objects.create(
        name=f"{name}{starters}{assignment}{classes}", penalties_enabled=True,
        pylon_penalty=5, task_penalty=10, stop_line_penalty=20)
    comp = Competition.objects.create(
        competition_type=ctype, name="R", date=datetime.date(2026, 7, 1),
        is_active=True, assignment_method=assignment, start_pattern=PATTERN)
    made = []
    for i in range(classes):
        cc = comp.classes.all()[i]
        cc.is_running, cc.run_position = True, i
        cc.practice_runs, cc.counted_runs = 1, 2
        cc.age_from, cc.age_to = 0, 99
        cc.save()
        made.append(cc)
    for i in range(1, starters + 1):
        p = Participant.objects.create(
            competition_type=ctype, first_name=f"F{i}", last_name=f"L{i}",
            date_of_birth=datetime.date(2010, 1, 1))
        EventEntry.objects.create(participant=p, competition=comp, bib_number=i)
        if assignment == "manual":
            ClassAssignment.objects.create(
                participant=p, competition_class=made[i % len(made)])
    return comp, made


def drive(comp, count):
    s = TimingSettings.load()
    s.start_channel, s.finish_channel = 1, 2
    s.save()
    t = datetime.datetime(2026, 7, 1, 10, 0, 0)
    for i in range(count):
        record_signal(i * 2 + 1, 1, False, t.time())
        t += datetime.timedelta(seconds=40)
        record_signal(i * 2 + 2, 2, False, t.time())
        t += datetime.timedelta(seconds=5)


def _count(client, url):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    return len(ctx), len(response.content)


# ---- PRF-1: does the live cost grow with the field? ------------------------

@pytest.mark.parametrize("assignment", ["manual", "age"])
@pytest.mark.parametrize("n", [20, 50, 100])
def test_live_cost(client, assignment, n):
    build(n, assignment=assignment)
    q, _ = _count(client, reverse("timing:auto-state"))
    print(f"\nPRF-1  auto-state  {assignment:6} starters={n:4d}  queries={q}")


# ---- PRF-2: export-all re-reads the runs per class -------------------------

@pytest.mark.parametrize("classes", [1, 3, 6])
def test_export_all(client, classes):
    comp, _ = build(30, classes=classes)
    drive(comp, 10)
    q, size = _count(client, reverse("results:export-all"))
    print(f"\nPRF-2  export-all  classes={classes}  queries={q}")


# ---- PRF-7: what a live refresh ships --------------------------------------

def test_payloads(client):
    comp, _ = build(200)
    drive(comp, 40)
    for name in ("timing:auto-state", "timing:arrangement", "timing:dashboard-state"):
        q, size = _count(client, reverse(name))
        print(f"\nPRF-7  {name:26} queries={q:4d}  payload={size / 1024:8.1f} KiB")
