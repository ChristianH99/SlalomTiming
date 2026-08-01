import datetime
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.competitions.models import Competition, CompetitionClass, CompetitionType
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.timing.models import TimedRun, TimingSignal

from . import logos, resultscalc, views
from .models import ManualTieResolution, ResultColumnSettings, ResultsPdfLayout

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
    # manual_entry: these runs carry a typed identity (bib/class/run), which is
    # what the Manual timing view records. Without it they would be *auto* runs,
    # and Auto timing binds those to the start order positionally — the identity
    # set here would be overwritten by whatever slot they landed in.
    return TimedRun.objects.create(
        competition=competition, start_signal=start, finish_signal=finish,
        bib_number=bib, competition_class=cclass, class_occurrence=occurrence,
        run_type=TimedRun.RunType.COUNTED, run_number=run_number, pylon_count=pylons,
        manual_entry=True,
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
    make_competitor(competition, cclass, 3, status="dnf")  # both runs timed, entry DNF
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    add_run(competition, cclass, 2, 1, 30)  # missing run 2
    add_run(competition, cclass, 3, 1, 30)
    add_run(competition, cclass, 3, 2, 30)

    results = resultscalc.compute_class_results(competition, cclass)
    assert [r.bib for r in results.ranked] == [1]
    # Only bib 2 is still *waiting* on a run. Bib 3's event is over — an entry-level
    # did-not-finish leaves them unclassified, so they sit below the placings with
    # their own state code rather than in the "not yet ranked" block.
    assert [r.bib for r in results.unranked] == [2]
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(3, "dnc")]


# ----- run state codes (DNF / DNC / DNS / DSQ) -----

def mark_run(competition, cclass, bib, run_number, status, occurrence=0,
             run_type=TimedRun.RunType.COUNTED):
    """Close a run with a state code and no time — what a DNS looks like when the
    competitor never started and no signal was ever recorded."""
    return TimedRun.objects.create(
        competition=competition, bib_number=bib, competition_class=cclass,
        class_occurrence=occurrence, run_type=run_type, run_number=run_number,
        status=status, manual_entry=True,
    )


def test_state_code_on_a_practice_run_does_not_affect_the_result():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    cclass.practice_runs = 1
    cclass.save(update_fields=["practice_runs"])
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dsq", run_type=TimedRun.RunType.PRACTICE)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.rank) for r in results.ranked] == [(1, 1)]
    assert not results.status_rows


def test_a_practice_code_beside_a_zero_tally_is_explained(client):
    """The tally counts a *competitor's* outcome, and a code on a
    practice run is not one — so the table legitimately showed "DNS" in a cell
    above a tally reading "DNS: 0", and the two together read as a bug. In
    exactly that case the table now says which of the two it is counting."""
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    cclass.practice_runs = 1
    cclass.save(update_fields=["practice_runs"])
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dns", run_type=TimedRun.RunType.PRACTICE)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)

    response = client.get(reverse("results:class", args=[cclass.pk]))
    summary = response.context["layout"]["summary"]
    # The competitor is ranked: the practice code settled nothing.
    assert summary["statuses"] == [
        {"label": "DNS", "count": 0},
        {"label": "DNC", "count": 0},
        {"label": "DSQ", "count": 0},
    ]
    assert summary["practice_only_code"] is True
    assert b"does not end anybody" in response.content


def test_the_note_stays_off_when_a_competitor_really_is_settled(client):
    """Only the confusing case earns a sentence. A DNS that *did* end somebody's
    event needs no explaining — the tally shows it."""
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    cclass.practice_runs = 1
    cclass.save(update_fields=["practice_runs"])
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dns", run_type=TimedRun.RunType.PRACTICE)
    mark_run(competition, cclass, 1, 1, "dns")
    mark_run(competition, cclass, 1, 2, "dns")

    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert response.context["layout"]["summary"]["practice_only_code"] is False
    assert b"does not end anybody" not in response.content


@pytest.mark.parametrize("status", ["dnf", "dnc", "dns", "dsq"])
def test_aggregate_over_several_runs_is_dnc_when_one_is_marked(status):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    mark_run(competition, cclass, 1, 2, status)

    results = resultscalc.compute_class_results(competition, cclass)
    assert not results.ranked
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dnc")]


@pytest.mark.parametrize("status", ["dnf", "dnc", "dns", "dsq"])
def test_best_run_still_places_on_the_run_that_was_driven(status):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.BEST_RUN)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    mark_run(competition, cclass, 1, 2, status)

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.rank) for r in results.ranked] == [(1, 1)]
    assert results.ranked[0].score == Decimal("30.000")
    assert not results.status_rows


def test_best_run_is_dnc_when_every_counted_run_is_marked():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.BEST_RUN)
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dnf")
    mark_run(competition, cclass, 1, 2, "dns")

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dnc")]


def test_every_counted_run_disqualified_is_dsq():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dsq")
    mark_run(competition, cclass, 1, 2, "dsq")

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dsq")]


def test_every_counted_run_not_started_is_dns():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dns")
    mark_run(competition, cclass, 1, 2, "dns")

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dns")]


def test_disqualified_from_the_whole_event_is_dsq_at_once():
    """The entry-level flag doesn't wait for the runs: it is a decision about the
    competitor, so it settles them even with a counted run still to drive."""
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1, status=EventEntry.Status.DSQ)
    add_run(competition, cclass, 1, 1, 30)

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dsq")]
    assert not results.unranked


def test_a_marked_run_leaves_them_waiting_while_a_run_is_still_to_come():
    """One state code doesn't retire a competitor who still has a run to drive —
    they stay in the not-yet-ranked block until every counted run has settled."""
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    mark_run(competition, cclass, 1, 1, "dnf")

    results = resultscalc.compute_class_results(competition, cclass)
    assert [r.bib for r in results.unranked] == [1]
    assert not results.status_rows


def test_status_rows_are_ordered_dns_then_dnc_then_dsq_then_by_bib():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    for bib in (1, 2, 3, 4):
        make_competitor(competition, cclass, bib)
    for bib, first, second in (
        (1, "dsq", "dsq"),   # DSQ
        (2, "dns", "dns"),   # DNS
        (3, "dnf", "dnf"),   # DNC
        (4, "dns", "dns"),   # DNS, higher bib than 2
    ):
        mark_run(competition, cclass, bib, 1, first)
        mark_run(competition, cclass, bib, 2, second)

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [
        (2, "dns"), (4, "dns"), (3, "dnc"), (1, "dsq"),
    ]


def test_a_state_code_beats_a_stray_second_row_for_the_same_run():
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    mark_run(competition, cclass, 1, 2, "dsq")   # the timekeeper's decision

    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dnc")]


def test_summary_counts_each_state_code(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    for bib in (1, 2, 3):
        make_competitor(competition, cclass, bib)
    mark_run(competition, cclass, 1, 1, "dns")
    mark_run(competition, cclass, 1, 2, "dns")
    mark_run(competition, cclass, 2, 1, "dnf")
    mark_run(competition, cclass, 2, 2, "dnf")
    add_run(competition, cclass, 3, 1, 30)      # still waiting on run 2

    section = views.class_section(competition, cclass)
    summary = section["layout"]["summary"]
    assert summary["starters"] == 3
    assert summary["statuses"] == [
        {"label": "DNS", "count": 1},
        {"label": "DNC", "count": 1},
        {"label": "DSQ", "count": 0},
    ]
    # Nothing here is settled on a *practice* code, so the note that explains
    # the difference stays off. See test_a_practice_code_is_explained_...
    assert summary["practice_only_code"] is False


def test_unranked_run_cells_offer_a_dns_key_and_the_table_renders(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)      # run 2 neither timed nor marked

    section = views.class_section(competition, cclass)
    [row] = section["unranked"]
    assert row["counted"][0]["dns_key"] == ""          # already timed
    assert row["counted"][1]["dns_key"]                 # offered
    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert response.status_code == 200
    assert b"rt-dns" in response.content
    # The not-yet-ranked table drops the Total column, so its rows are one cell
    # shorter than the ranked table's.
    assert b"REGISTERED" not in response.content.upper()


def test_results_dns_button_records_the_run(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    section = views.class_section(competition, cclass)
    key = section["unranked"][0]["counted"][1]["dns_key"]

    response = client.post(
        reverse("timing:run-status"),
        data={"slot_key": key, "status": "dns"},
        content_type="application/json",
    )
    assert response.status_code == 200 and response.json()["ok"]
    results = resultscalc.compute_class_results(competition, cclass)
    assert [(r.bib, r.final_status) for r in results.status_rows] == [(1, "dnc")]


def test_export_all_pdf_covers_every_group(client):
    """A section with ranked rows, state-code rows and still-waiting rows renders
    to PDF — the unranked table has one column fewer, which used to be built from
    the same header row."""
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    for bib in (1, 2, 3):
        make_competitor(competition, cclass, bib)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 31)
    mark_run(competition, cclass, 2, 1, "dns")
    mark_run(competition, cclass, 2, 2, "dns")
    add_run(competition, cclass, 3, 1, 30)

    response = client.get(reverse("results:export-all"))
    assert response.status_code == 200
    assert response.content[:4] == b"%PDF"


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

def test_columns_for_defaults_to_available_then_adds_class_columns():
    _, competition, cclass = make_setup()
    # Type collects club/licence/address/email by default; name + dob always.
    available = ResultColumnSettings.available_keys(competition)
    assert "club" in available and "driver_name" in available and "birthday" in available
    # No rows yet -> the safe default set, NOT everything available: a results
    # table (and the PDF that goes on the notice board) must not carry every
    # competitor's e-mail, phone and home address unless somebody asked for it.
    default = ResultColumnSettings.columns_for(competition, cclass)
    assert set(default) == {"driver_name", "club", "birth_year"} & set(available)
    for private in ("email", "phone", "street", "city", "license", "birthday"):
        assert private not in default
    # A General set of just club; the class *adds* city on top (additive, no override).
    ResultColumnSettings.objects.create(
        competition=competition, competition_class=None, columns=["club"],
    )
    assert ResultColumnSettings.columns_for(competition, cclass) == ["club"]
    ResultColumnSettings.objects.create(
        competition=competition, competition_class=cclass, columns=["city"],
    )
    # Canonical order: club before city.
    assert ResultColumnSettings.columns_for(competition, cclass) == ["club", "city"]


def test_results_settings_page_saves(client):
    _, competition, cclass = make_setup()
    response = client.post(reverse("results:settings"), {
        "general-club": "on",
        "show-overall": "on",
        # A class addition that's already in General is ignored (additive only).
        f"class-{cclass.pk}-club": "on",
        f"class-{cclass.pk}-city": "on",
    })
    assert response.status_code == 302
    assert ResultColumnSettings.general_columns(competition) == ["club"]
    assert ResultColumnSettings.overall_enabled(competition) is True
    assert ResultColumnSettings.class_additions(competition, cclass) == ["city"]


# ----- the PDF layout's own inputs -----

# A 1x1 GIF: small, and a real image, so ImageField and Pillow both accept it.
PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
         b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def test_settings_page_renders_stored_markup_sanitised(client):
    """The other half of the stored-XSS fix. The editor sanitises on save, but a row can also be
    written by an *import*, so the page sanitises again on the way out instead of
    trusting the column with a bare |safe."""
    _, competition, _ = make_setup()
    ResultsPdfLayout.objects.create(competition=competition)
    ResultsPdfLayout.objects.filter(competition=competition).update(
        header_html='<img src=x onerror="alert(document.cookie)"><b>Cup</b>',
        footer_html="<script>alert(1)</script>",
    )
    body = client.get(reverse("results:settings")).content.decode()
    assert "onerror" not in body
    assert "<script>alert(1)</script>" not in body
    assert "<b>Cup</b>" in body     # the markup the editor does allow still renders


def test_an_over_sized_logo_is_refused(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _, competition, _ = make_setup()
    big = SimpleUploadedFile("logo.gif", PIXEL + b"\0" * logos.MAX_LOGO_BYTES,
                             content_type="image/gif")
    response = client.post(reverse("results:settings"),
                           {"general-club": "on", "image_left": big}, follow=True)
    assert "too large" in response.content.decode()
    assert not ResultsPdfLayout.objects.get(competition=competition).image_left


def test_a_logo_that_is_not_an_image_is_refused(client, settings, tmp_path):
    """These fields are assigned straight from request.FILES (no ModelForm), so
    without an explicit check anything at all was written into MEDIA_ROOT."""
    settings.MEDIA_ROOT = tmp_path
    _, competition, _ = make_setup()
    bogus = SimpleUploadedFile("logo.png", b"<html>not an image</html>",
                               content_type="image/png")
    response = client.post(reverse("results:settings"),
                           {"general-club": "on", "image_left": bogus}, follow=True)
    assert "not an image" in response.content.decode()
    assert not ResultsPdfLayout.objects.get(competition=competition).image_left


def test_a_real_logo_still_saves(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _, competition, _ = make_setup()
    good = SimpleUploadedFile("logo.gif", PIXEL, content_type="image/gif")
    client.post(reverse("results:settings"), {"general-club": "on", "image_left": good})
    assert ResultsPdfLayout.objects.get(competition=competition).image_left


def test_overall_page_ranks_across_classes(client):
    ctype, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE, counted_runs=2)
    other = CompetitionClass.objects.create(
        competition=competition, name="T2", is_running=True,
        practice_runs=0, counted_runs=2,
        scoring_method=CompetitionClass.Scoring.AGGREGATE, position=100,
    )
    make_competitor(competition, cclass, 1)
    make_competitor(competition, other, 2)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)   # total 60
    add_run(competition, other, 2, 1, 25)
    add_run(competition, other, 2, 2, 25)    # total 50 -> bib 2 wins overall

    results = resultscalc.compute_overall_results(
        competition, CompetitionClass.Scoring.AGGREGATE, 2
    )
    assert [(r.bib, r.rank, r.class_name) for r in results.ranked] == [
        (2, 1, "T2"), (1, 2, "T1"),
    ]
    response = client.get(reverse(
        "results:overall", args=[CompetitionClass.Scoring.AGGREGATE, 2]
    ))
    assert response.status_code == 200


def test_results_index_lists_classes(client):
    _, competition, cclass = make_setup()
    response = client.get(reverse("results:index"))
    assert response.status_code == 200
    assert b"Class T1" in response.content


def test_results_index_names_a_bib_with_no_competitor(client):
    """A time recorded against a bib nobody is registered under cannot appear in
    a class table — without a competitor there is no class to show it under — so
    it would simply be missing from the one page somebody opens to ask whether
    the event is complete. It is named here instead, with the name box
    highlighted."""
    _, competition, cclass = make_setup()
    run = add_run(competition, cclass, 47, 1, 30)
    TimedRun.objects.filter(pk=run.pk).update(competition_class=None)

    rows = views.unregistered_bibs(competition)
    assert [row["bib"] for row in rows] == [47]
    assert rows[0]["runs"] == [{"label": "C1", "time": "00:30.00", "status": ""}]

    response = client.get(reverse("results:index"))
    assert response.status_code == 200
    assert b"unregistered-name" in response.content
    assert b"#47" in response.content


def test_registering_the_bib_puts_its_runs_in_the_class_table(client):
    """Registering the participant is the whole fix and needs no second action:
    the run joins their class result, and the panel above empties. Computing the
    table is a *read*, so the stored row is left alone until something writes
    (autotiming.sync_bindings) — the screen never waits on that."""
    _, competition, cclass = make_setup(counted_runs=1)
    make_competitor(competition, cclass, 47)
    run = add_run(competition, cclass, 47, 1, 30)
    TimedRun.objects.filter(pk=run.pk).update(competition_class=None)

    assert views.unregistered_bibs(competition) == []
    results = resultscalc.compute_class_results(competition, cclass)
    assert [(c.bib, c.rank) for c in results.ranked] == [(47, 1)]
    run.refresh_from_db()
    assert run.competition_class_id is None      # the read wrote nothing

    resultscalc.sync_identities(competition)     # …the next writer does
    run.refresh_from_db()
    assert run.competition_class_id == cclass.pk


# ----- time formatting -----

def test_format_clock_mm_ss():
    from apps.timing import calc
    assert calc.format_clock(Decimal("30.00"), 2) == "00:30.00"
    assert calc.format_clock(Decimal("62.50"), 2) == "01:02.50"
    assert calc.format_clock(Decimal("5.123"), 3) == "00:05.123"
    assert calc.format_clock(None, 2) == ""


def test_skipped_repeat_entry_shows_gap_to_winner(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1, occurrences=2)
    make_competitor(competition, cclass, 2)
    add_run(competition, cclass, 1, 1, 20, occurrence=1)  # best occurrence -> 40 (winner)
    add_run(competition, cclass, 1, 2, 20, occurrence=1)
    add_run(competition, cclass, 1, 1, 30, occurrence=0)  # skipped repeat -> 60
    add_run(competition, cclass, 1, 2, 30, occurrence=0)
    add_run(competition, cclass, 2, 1, 50)
    add_run(competition, cclass, 2, 2, 50)  # -> 100
    response = client.get(reverse("results:class", args=[cclass.pk]))
    # The skipped repeat (60) is 20s behind the winner (40): its gap is still shown.
    assert b"+00:20.00" in response.content


def test_class_page_shows_clock_times(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)  # total 60 -> 01:00.00
    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert b"01:00.00" in response.content and b"00:30.00" in response.content


def test_regularity_column_shows_difference(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.REGULARITY)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 33)  # spread 3 -> 00:03.00
    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert b"Difference" in response.content and b"00:03.00" in response.content


def test_results_summary_counts(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)                     # ranked
    make_competitor(competition, cclass, 2)                     # incomplete (no runs)
    make_competitor(competition, cclass, 3, status="dns")       # out of the event
    make_competitor(competition, cclass, 4, status="dsq")       # out of the event
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    response = client.get(reverse("results:class", args=[cclass.pk]))
    body = response.content
    assert b"Starters:" in body and b"4" in body
    # "Classified / not classified" told the operator nothing about which outcome
    # they were looking at; the summary names the outcomes themselves.
    assert b"DNS:" in body and b"DNC:" in body and b"DSQ:" in body
    summary = response.context["layout"]["summary"]
    assert summary["starters"] == 4
    assert summary["statuses"] == [
        {"label": "DNS", "count": 1},
        {"label": "DNC", "count": 0},
        {"label": "DSQ", "count": 1},
    ]


def test_best_run_column_heading(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.BEST_RUN)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 40)
    add_run(competition, cclass, 1, 2, 25)  # best 25 -> 00:25.00
    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert b"Best run" in response.content and b"00:25.00" in response.content


# ----- tie states -----

def test_auto_resolved_tie_is_green_not_inspect():
    _, competition, cclass = make_setup(tie_break=CompetitionType.TieBreak.FASTEST_RUN)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # Equal aggregate (60) but different fastest run -> rule resolves the tie.
    add_run(competition, cclass, 1, 1, 20)
    add_run(competition, cclass, 1, 2, 40)
    add_run(competition, cclass, 2, 1, 29)
    add_run(competition, cclass, 2, 2, 31)
    results = resultscalc.compute_class_results(competition, cclass)
    assert [r.tie_state for r in results.ranked] == ["auto", "auto"]
    assert not any(r.inspect for r in results.ranked)


def _tie_setup():
    ctype, competition, cclass = make_setup(tie_break=CompetitionType.TieBreak.FASTEST_RUN)
    make_competitor(competition, cclass, 1)
    make_competitor(competition, cclass, 2)
    # Identical runs -> unbreakable tie (pending) at ranks 1 & 1.
    for bib in (1, 2):
        add_run(competition, cclass, bib, 1, 30)
        add_run(competition, cclass, bib, 2, 30)
    return competition, cclass


def test_unbreakable_tie_pending_until_manual():
    competition, cclass = _tie_setup()
    results = resultscalc.compute_class_results(competition, cclass)
    assert [r.tie_state for r in results.ranked] == ["pending", "pending"]
    assert {r.rank for r in results.ranked} == {1}
    assert all(r.tie_start == 1 and r.tie_size == 2 for r in results.ranked)


def test_tie_resolve_endpoint_orders_and_greens(client):
    competition, cclass = _tie_setup()
    e1 = EventEntry.objects.get(competition=competition, bib_number=1)
    e2 = EventEntry.objects.get(competition=competition, bib_number=2)
    response = client.post(
        reverse("results:tie-resolve"),
        data={"scope": f"class:{cclass.pk}", "members": [
            {"entry_pk": e1.pk, "occurrence": 0, "rank": 1},
            {"entry_pk": e2.pk, "occurrence": 0, "rank": 2},
        ]},
        content_type="application/json",
    )
    assert response.status_code == 200 and response.json()["ok"]
    results = resultscalc.compute_class_results(competition, cclass)
    by_bib = {r.bib: r for r in results.ranked}
    assert by_bib[1].rank == 1 and by_bib[2].rank == 2
    assert by_bib[1].tie_state == "manual" and by_bib[2].tie_state == "manual"


def test_tie_resolve_allows_shared_but_rejects_out_of_order(client):
    competition, cclass = _tie_setup()
    e1 = EventEntry.objects.get(competition=competition, bib_number=1)
    e2 = EventEntry.objects.get(competition=competition, bib_number=2)
    url = reverse("results:tie-resolve")
    scope = f"class:{cclass.pk}"
    # 2,1 is not a legal ranking.
    bad = client.post(url, data={"scope": scope, "members": [
        {"entry_pk": e1.pk, "occurrence": 0, "rank": 2},
        {"entry_pk": e2.pk, "occurrence": 0, "rank": 1},
    ]}, content_type="application/json")
    assert bad.status_code == 400 and not bad.json()["ok"]
    # 1,1 (an intentional shared placing) is allowed.
    ok = client.post(url, data={"scope": scope, "members": [
        {"entry_pk": e1.pk, "occurrence": 0, "rank": 1},
        {"entry_pk": e2.pk, "occurrence": 0, "rank": 1},
    ]}, content_type="application/json")
    assert ok.status_code == 200 and ok.json()["ok"]
    results = resultscalc.compute_class_results(competition, cclass)
    assert {r.rank for r in results.ranked} == {1}
    assert all(r.tie_state == "manual" for r in results.ranked)


def test_class_results_page_renders(client):
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    make_competitor(competition, cclass, 1)
    add_run(competition, cclass, 1, 1, 30)
    add_run(competition, cclass, 1, 2, 30)
    response = client.get(reverse("results:class", args=[cclass.pk]))
    assert response.status_code == 200
    assert b"Class T1" in response.content


# ----- what a results table is allowed to cost -----

def _entered_field(competition, cclass, first_bib, last_bib):
    for bib in range(first_bib, last_bib + 1):
        make_competitor(competition, cclass, bib)
        add_run(competition, cclass, bib, 1, 30 + bib)
        add_run(competition, cclass, bib, 2, 31 + bib)


def _queries_for(client, url):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return ctx.captured_queries


def test_results_table_cost_does_not_grow_with_the_field(client):
    """A results table used to ask the database for each competitor's runs twice
    over — counted, then practice — per competitor, per class: 149 queries for a
    40-strong class, 629 for 200, and "export everything" multiplied that by the
    class count. One read of the event's runs serves the whole table."""
    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    url = reverse("results:class", args=[cclass.pk])
    _entered_field(competition, cclass, 1, 5)
    small = len(_queries_for(client, url))
    _entered_field(competition, cclass, 6, 40)
    large = len(_queries_for(client, url))
    assert large == small, (
        f"results:class costs {small} queries for 5 competitors but {large} for 40"
    )


def test_reading_results_never_writes(client):
    """Rendering a table (or a PDF) is a read. It must not take the write lock the
    timing rig needs — see apps/timing/autotiming.sync_bindings."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _, competition, cclass = make_setup(CompetitionClass.Scoring.AGGREGATE)
    _entered_field(competition, cclass, 1, 5)
    for url in (reverse("results:class", args=[cclass.pk]),
                reverse("results:export-class", args=[cclass.pk])):
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(url).status_code == 200
        writes = [q["sql"] for q in ctx.captured_queries
                  if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
        assert writes == [], f"{url} wrote: {writes}"


# ----- A tie resolution belongs to the score it was made at -----

def _resolve(client, competition, cclass, ranks=(1, 2)):
    e1 = EventEntry.objects.get(competition=competition, bib_number=1)
    e2 = EventEntry.objects.get(competition=competition, bib_number=2)
    return client.post(
        reverse("results:tie-resolve"),
        data={"scope": f"class:{cclass.pk}", "members": [
            {"entry_pk": e1.pk, "occurrence": 0, "rank": ranks[0]},
            {"entry_pk": e2.pk, "occurrence": 0, "rank": ranks[1]},
        ]},
        content_type="application/json",
    )


def test_a_resolution_records_the_score_it_was_made_at(client):
    competition, cclass = _tie_setup()
    _resolve(client, competition, cclass)
    stored = ManualTieResolution.objects.get(competition=competition)
    assert stored.score == Decimal("60.000")


def test_a_resolution_does_not_reapply_at_a_different_score(client):
    # The same two competitors tie again later at another score. That is a tie
    # nobody has looked at, so it must come back as pending rather than inherit
    # the decision made about the old one.
    competition, cclass = _tie_setup()
    _resolve(client, competition, cclass)
    TimedRun.objects.filter(competition=competition, run_number=2).delete()
    for bib in (1, 2):
        add_run(competition, cclass, bib, 2, 40)   # now tied at 70, not 60
    results = resultscalc.compute_class_results(competition, cclass)
    assert [r.tie_state for r in results.ranked] == ["pending", "pending"]


# ----- PDF: no column is narrower than its own heading -----


def _pdf_layout(enabled, counted=3, training=0, heading="Total"):
    return views.build_layout(
        enabled, counted_count=counted, training_count=training,
        include_class=True, score_heading=heading,
        summary={"starters": 0, "classified": 0, "not_classified": 0},
    )


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_a_heading_word_is_never_broken_across_two_lines(language, orientation):
    """The column weights are proportions of the page, so a heading that is short
    in one language ("Bib") and long in another ("Startnr.") used to be split
    mid-word. Every column must fit the longest word of its own heading — in every
    language, orientation and column set."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from django.utils import translation

    from . import pdf

    page = landscape(A4) if orientation == "landscape" else A4
    usable_w = page[0] - 2 * pdf._MARGIN_X
    column_sets = [
        ["driver_name"],
        ["driver_name", "club", "email", "phone", "street", "city", "vehicle",
         "license", "birthday", "birth_year"],
    ]
    # (counted, training) — a run-heavy table squeezes the info columns hardest.
    run_counts = [(1, 0), (3, 0), (4, 3)]
    headings = [views._score_heading(m) for m in CompetitionClass.Scoring]
    cases = [(cols, runs, head)
             for cols in column_sets for runs in run_counts for head in headings]
    with translation.override(language):
        for enabled, (counted, training), heading in cases:
            layout = _pdf_layout(enabled + ["training"], counted=counted,
                                 training=training, heading=str(heading))
            widths = pdf._col_widths(layout, usable_w)
            assert sum(widths) == pytest.approx(usable_w), "the table must still span the page"
            for width, (text, style) in zip(widths, pdf._header_labels(layout)):
                inner = width - 2 * pdf._CELL_HPAD
                for word in str(text).split():
                    got = stringWidth(word, style.fontName, style.fontSize)
                    assert got <= inner + 0.01, (
                        f"{language}/{orientation}: {word!r} needs {got:.1f}pt, "
                        f"column has {inner:.1f}pt"
                    )


def test_saving_a_resolution_sweeps_ones_whose_tie_is_gone(client):
    competition, cclass = _tie_setup()
    _resolve(client, competition, cclass)
    stale = ManualTieResolution.objects.create(
        competition=competition, scope=f"class:{cclass.pk}",
        members=[[9991, 0, 1], [9992, 0, 2]], score=Decimal("12.000"),
    )
    _resolve(client, competition, cclass, ranks=(1, 1))
    assert not ManualTieResolution.objects.filter(pk=stale.pk).exists()
    assert ManualTieResolution.objects.filter(competition=competition).count() == 1


# --- what a class name can do to a download header ---------------------------
#
# A class name is typed by an organiser and ends up in Content-Disposition. Three
# separate things went wrong when that header was an f-string, and each needs its
# own test because each fails differently: a quote splits the header into two
# parameters, a name outside latin-1 cannot be encoded into a header at all, and
# a newline raises BadHeaderError. Django's own content_disposition_header()
# handles all three; these pin that it is still the thing being used.

def test_a_quote_in_a_class_name_cannot_split_the_download_header(client):
    _, competition, cclass = make_setup()
    cclass.name = 'A"; attachment; filename="evil.pdf'
    cclass.save()
    make_competitor(competition, cclass, 1)

    response = client.get(reverse("results:export-class", args=[cclass.pk]))

    assert response.status_code == 200
    header = response["Content-Disposition"]
    # The quote has to arrive backslash-escaped, so the name stays one parameter
    # instead of closing it and opening another of the attacker's choosing.
    inner = header.split('filename="', 1)[1]
    assert '\\"' in inner, f"quote not escaped, header split apart: {header!r}"


def test_a_class_name_outside_latin_1_still_exports(client):
    """A header is latin-1 encoded, so a Cyrillic class name has to reach for
    RFC 5987 `filename*=` rather than raising on the way out."""
    _, competition, cclass = make_setup()
    cclass.name = "Кла"
    cclass.save()
    make_competitor(competition, cclass, 1)

    response = client.get(reverse("results:export-class", args=[cclass.pk]))

    assert response.status_code == 200
    assert "filename*=" in response["Content-Disposition"]


def test_a_newline_in_a_class_name_does_not_500_the_export(client):
    _, competition, cclass = make_setup()
    cclass.name = "Two\nLines"
    cclass.save()
    make_competitor(competition, cclass, 1)

    assert client.get(reverse("results:export-class", args=[cclass.pk])).status_code == 200


def test_contact_details_are_not_published_by_default():
    """With no saved ResultColumnSettings the general columns used to be
    *every* available column, so a sheet pinned to the notice board carried the
    e-mail, phone number and home address of every competitor before anyone had
    chosen anything. A default that publishes is the wrong default."""
    ctype, competition, _ = make_setup()
    ctype.requires_email = True
    ctype.requires_phone = True
    ctype.requires_address = True
    ctype.save()

    columns = ResultColumnSettings.general_columns(competition)

    assert "email" not in columns
    assert "phone" not in columns
    assert "street" not in columns
