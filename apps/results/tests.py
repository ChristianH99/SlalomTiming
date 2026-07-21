import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.competitions.models import Competition, CompetitionClass, CompetitionType
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.timing.models import TimedRun, TimingSignal

from . import resultscalc
from .models import ResultColumnSettings

pytestmark = pytest.mark.django_db

BASE = datetime.datetime(2026, 5, 1, 10, 0, 0)


def make_setup(scoring=CompetitionClass.Scoring.AGGREGATE, counted_runs=2,
               penalties_enabled=False, tie_break=CompetitionType.TieBreak.FASTEST_RUN,
               marshal_posts=False):
    ctype = CompetitionType.objects.create(
        name="Motorcycle", penalties_enabled=penalties_enabled,
        pylon_penalty=5, task_penalty=10, stop_line_penalty=10, max_penalty_per_task=20,
        tie_break=tie_break,
    )
    competition = Competition.objects.create(
        competition_type=ctype, name="Race", date=datetime.date(2026, 5, 1),
        is_active=True, penalties_by_marshal_posts=marshal_posts,
    )
    # Competition.save seeds default classes; use a dedicated running one.
    cclass = CompetitionClass.objects.create(
        competition=competition, name="T1", is_running=True,
        practice_runs=0, counted_runs=counted_runs, scoring_method=scoring, position=99,
    )
    return ctype, competition, cclass


def make_competitor(competition, cclass, bib, first="Racer", occurrences=1, status="registered"):
    participant = Participant.objects.create(
        competition_type=competition.competition_type,
        first_name=first, last_name=f"B{bib}", date_of_birth=datetime.date(2010, 1, 1),
        club=f"Club {bib}",
    )
    EventEntry.objects.create(
        participant=participant, competition=competition, bib_number=bib, status=status,
    )
    for _ in range(occurrences):
        ClassAssignment.objects.create(participant=participant, competition_class=cclass)
    return participant


def add_run(competition, cclass, bib, run_number, seconds, occurrence=0, pylons=0):
    start = TimingSignal.objects.create(
        competition=competition, running_number=bib, port=1, device_time=BASE.time(),
    )
    finish_time = (BASE + datetime.timedelta(seconds=seconds)).time()
    finish = TimingSignal.objects.create(
        competition=competition, running_number=bib, port=2, device_time=finish_time,
    )
    return TimedRun.objects.create(
        competition=competition, start_signal=start, finish_signal=finish,
        bib_number=bib, competition_class=cclass, class_occurrence=occurrence,
        run_type=TimedRun.RunType.COUNTED, run_number=run_number, pylon_count=pylons,
    )


def ranks(results):
    return [(r.bib, r.rank, r.inspect, r.skipped) for r in results.ranked]


# ----- scoring methods -----

def test_aggregate_ranks_by_summed_totals():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # bib 1: 30 + 32 = 62 ; bib 2: 31 + 30 = 61 -> bib 2 wins
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 32)
    add_run(competition, cclass, 2, 1, 31)
    add_run(competition, cclass, 2, 2, 30)

    results = resultscalc.compute_class_results(competition, cclass)
    assert ranks(results) == [(2, 1, False, False), (1, 2, False, False)]
    assert results.ranked[0].score == Decimal("61.00")


def test_best_run_ranks_after_one_completed_run():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.BEST_RUN, counted_runs=2)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # bib 1 has only run 1 recorded; bib 2 has both. Both are rankable for best-run.
    add_run(competition, cclass, 1, 1, 24)
    add_run(competition, cclass, 2, 1, 26)
    add_run(competition, cclass, 2, 2, 25)

    results = resultscalc.compute_class_results(competition, cclass)
    assert ranks(results) == [(1, 1, False, False), (2, 2, False, False)]
    assert results.ranked[0].score == Decimal("24.00")  # its one run
    assert not results.unranked


def test_best_run_uses_fastest_counted_run():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.BEST_RUN)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # best of bib 1 = 25 ; best of bib 2 = 26 -> bib 1 wins
    add_run(competition, cclass, 1, 1, 40)
    add_run(competition, cclass, 1, 2, 25)
    add_run(competition, cclass, 2, 1, 26)
    add_run(competition, cclass, 2, 2, 27)

    results = resultscalc.compute_class_results(competition, cclass)
    assert ranks(results) == [(1, 1, False, False), (2, 2, False, False)]
    assert results.ranked[0].score == Decimal("25.00")


def test_regularity_smallest_difference_wins():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.REGULARITY)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # bib 1 spread |30-33| = 3 ; bib 2 spread |40-41| = 1 -> bib 2 wins
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 33)
    add_run(competition, cclass, 2, 1, 40)
    add_run(competition, cclass, 2, 2, 41)

    results = resultscalc.compute_class_results(competition, cclass)
    assert ranks(results) == [(2, 1, False, False), (1, 2, False, False)]
    assert results.ranked[0].score == Decimal("1.00")


# ----- penalties folded into totals -----

def test_penalties_added_to_total():
    _, competition, cclass = make_setup(
        CompetitionClass.Scoring.AGGREGATE, penalties_enabled=True
    )
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # bib 1 raw 30+30=60, +2 pylons*5s = 70 ; bib 2 raw 33+33=66, no penalty -> bib 2 wins
    add_run(competition, cclass, 1, 1, 30, pylons=1)
    add_run(competition, cclass, 1, 2, 30, pylons=1)
    add_run(competition, cclass, 2, 1, 33)
    add_run(competition, cclass, 2, 2, 33)

    results = resultscalc.compute_class_results(competition, cclass)
    assert ranks(results) == [(2, 1, False, False), (1, 2, False, False)]
    assert results.ranked[0].score == Decimal("66.00")
    assert results.ranked[1].score == Decimal("70.00")


# ----- multiple entries -----

def test_repeat_entry_only_best_ranked():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1, occurrences=2)  # entered twice
    make_competitor(competition, cclass, 2)
    # occurrence 0: 30+30=60 ; occurrence 1: 20+20=40 (better) ; bib 2: 50+50=100
    add_run(competition, cclass, 1, 1, 30, occurrence=0)
    add_run(competition, cclass, 1, 2, 30, occurrence=0)
    add_run(competition, cclass, 1, 1, 20, occurrence=1)
    add_run(competition, cclass, 1, 2, 20, occurrence=1)
    add_run(competition, cclass, 2, 1, 50)
    add_run(competition, cclass, 2, 2, 50)

    results = resultscalc.compute_class_results(competition, cclass)
    # Best occurrence of bib 1 is rank 1; the other occurrence is shown but skipped
    # (no rank); bib 2 keeps a contiguous rank 2.
    by_rank = [(r.bib, r.occurrence, r.rank, r.skipped) for r in results.ranked]
    assert (1, 1, 1, False) in by_rank
    assert (2, 0, 2, False) in by_rank
    skipped = [r for r in results.ranked if r.skipped]
    assert len(skipped) == 1 and skipped[0].bib == 1 and skipped[0].rank is None


# ----- ties -----

def test_tie_separated_by_fastest_run():
    # Equal aggregate but different fastest run -> tie-break resolves, no inspect.
    _, competition, cclass = make_setup(
        CompetitionClass.Scoring.AGGREGATE, tie_break=CompetitionType.TieBreak.FASTEST_RUN
    )
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    add_run(competition, cclass, 1, 1, 20)  # fastest 20
    add_run(competition, cclass, 1, 2, 40)  # sum 60
    add_run(competition, cclass, 2, 1, 29)  # fastest 29
    add_run(competition, cclass, 2, 2, 31)  # sum 60

    results = resultscalc.compute_class_results(competition, cclass)
    assert ranks(results) == [(1, 1, False, False), (2, 2, False, False)]


def test_unbreakable_tie_flagged_for_inspection():
    _, competition, cclass = make_setup(
        CompetitionClass.Scoring.AGGREGATE, tie_break=CompetitionType.TieBreak.FASTEST_RUN
    )
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # Identical runs -> same sum AND same fastest run -> inspection.
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    add_run(competition, cclass, 2, 1, 30)
    add_run(competition, cclass, 2, 2, 30)

    results = resultscalc.compute_class_results(competition, cclass)
    assert all(r.inspect for r in results.ranked)
    assert {r.rank for r in results.ranked} == {1}  # both share rank 1


def test_manual_tie_break_always_flags():
    _, competition, cclass = make_setup(
        CompetitionClass.Scoring.AGGREGATE, tie_break=CompetitionType.TieBreak.MANUAL
    )
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # Same sum, different fastest run — Manual still can't auto-separate.
    add_run(competition, cclass, 1, 1, 20)
    add_run(competition, cclass, 1, 2, 40)
    add_run(competition, cclass, 2, 1, 29)
    add_run(competition, cclass, 2, 2, 31)

    results = resultscalc.compute_class_results(competition, cclass)
    assert all(r.inspect for r in results.ranked)


# ----- incomplete -----

def test_incomplete_listed_unranked():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)  # only one counted run recorded
    make_competitor(competition, cclass, 3, status="dnf")  # complete but DNF
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    add_run(competition, cclass, 2, 1, 30)  # missing run 2
    add_run(competition, cclass, 3, 1, 30)
    add_run(competition, cclass, 3, 2, 30)

    results = resultscalc.compute_class_results(competition, cclass)
    assert [r.bib for r in results.ranked] == [1]
    assert {r.bib for r in results.unranked} == {2, 3}


# ----- auto-timing identity sync -----

def test_sync_identities_binds_positional_runs(monkeypatch):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.BEST_RUN)
    make_competitor(competition, cclass, 7)
    # A run captured with no operator bib (as Auto timing leaves it).
    start = TimingSignal.objects.create(
        competition=competition, running_number=1, port=1, device_time=BASE.time(),
    )
    finish = TimingSignal.objects.create(
        competition=competition, running_number=1, port=2,
        device_time=(BASE + datetime.timedelta(seconds=22)).time(),
    )
    run = TimedRun.objects.create(
        competition=competition, start_signal=start, finish_signal=finish,
    )
    # Stub the ordered slots so the first started run maps to bib 7's counted run 1.
    slot = {
        "bib": 7, "class_pk": cclass.pk, "occurrence": 0,
        "run_type": TimedRun.RunType.COUNTED, "run_number": 1,
    }
    monkeypatch.setattr(resultscalc.autotiming, "ordered_slots", lambda comp: [slot])

    resultscalc.sync_identities(competition)
    run.refresh_from_db()
    assert run.bib_number == 7
    assert run.competition_class_id == cclass.pk
    assert run.run_type == TimedRun.RunType.COUNTED and run.run_number == 1


# ----- config model + pages -----

def test_columns_for_defaults_to_available_then_honours_override():
    _, competition, cclass = make_setup()
    # Type collects club/licence/address/email by default (requires_* defaults).
    available = ResultColumnSettings.available_keys(competition)
    assert "requires_club" in available
    # No rows yet -> general defaults to all available.
    assert set(ResultColumnSettings.columns_for(competition, cclass)) == set(available)
    # A class override that drops everything but club.
    ResultColumnSettings.objects.create(
        competition=competition, competition_class=cclass,
        columns=["requires_club"], inherit_general=False,
    )
    assert ResultColumnSettings.columns_for(competition, cclass) == ["requires_club"]


def test_results_settings_page_saves(client):
    _, competition, cclass = make_setup()
    response = client.post(reverse("results:settings"), {
        "general-requires_club": "on",
        f"class-{cclass.pk}-inherit": "on",
    })
    assert response.status_code == 302
    assert ResultColumnSettings.general_columns(competition) == ["requires_club"]


def test_class_results_page_renders(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert response.status_code == 200
    assert b"Class T1" in response.content
