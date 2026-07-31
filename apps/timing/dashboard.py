"""Organiser overview: a live, read-only status view of the whole event.

Unlike the Manual / Auto timing screens (which are operator surfaces), this is a
glance-able summary for whoever is running the event: how far along the day is,
which classes are done / running / still to come, who is on course right now, and
the headline counts (participants, runs, non-starters, penalties).

The **expected** run total is derived from the registered starters and their
classes — every entry-in-a-class owes its class's practice + counted runs — *not*
from the start pattern. So the progress figures are meaningful even when there is
no start pattern at all (competitors just turning up at the line in any order):
the number of runs to come is known the moment bibs and classes are assigned. A
run is **done** once a recorded ``TimedRun`` with a matching identity is *settled*
— it has a resolved time, or it was closed with a state code (DNS/DNF/DNC/DSQ; see
apps/timing/runstatus.py) — so both the pattern-driven (Auto timing) and
free-order (Manual timing) ways of recording a run feed the same tally.

The **denominator** shrinks for a competitor whose event is over (their entry is
DSQ, DNS or DNF): the runs they never drove and were never marked are not
outstanding work, so they come off the total rather than holding the bar below
100 % for the rest of the day. Runs of theirs that *were* settled stay counted on
both sides — they happened.

``serialize`` is called on page load and re-run on every WebSocket nudge (the
shared ``timing_live`` group), so the overview updates as times land.
"""

from apps.competitions import startpattern
from apps.participants.models import EventEntry

from . import autotiming, calc
from .models import TimedRun

# Entry statuses that end a competitor's event. Two things read this: the "did not
# run" headline count, and the expected-run denominator, which stops counting the
# runs these competitors will now never take (see _expected_ids).
DNX_STATUSES = (EventEntry.Status.DNS, EventEntry.Status.DNF, EventEntry.Status.DSQ)


def serialize(competition):
    """The whole organiser overview as a JSON-able dict: headline stats, overall
    run progress, per-class status, and the competitor on course now."""
    ctype = competition.competition_type
    precision = ctype.timing_precision
    # Fold Auto timing's positional identities onto the runs first, so a
    # pattern-bound run carries the bib/class/run its recorded-run tally is
    # matched by. In memory, on the copies this overview is built from: it is a
    # read, and the projector it is left open on must not be taking the write
    # lock the timing rig needs (see autotiming.sync_bindings).
    runs = autotiming.all_runs(competition)
    autotiming.apply_bindings(competition, runs)

    started_ids, finished_ids = _recorded_run_ids(runs, precision)
    classes = _classes(competition, started_ids, finished_ids, _retired(competition))
    expected = sum(c["total"] for c in classes)
    finished = sum(c["finished"] for c in classes)

    current = _current(competition, precision, runs)
    stats = _stats(competition, classes, expected, finished, ctype)

    return {
        "penalties_enabled": ctype.penalties_enabled,
        "precision": precision,
        "progress": {
            "expected": expected,
            "finished": finished,
            "percent": _percent(finished, expected),
        },
        "classes": classes,
        "current": current,
        "stats": stats,
    }


def _percent(part, whole):
    return round(part / whole * 100) if whole else 0


def _recorded_run_ids(runs, precision):
    """Two sets of run identities ``(bib, class_pk, occurrence, run_type,
    run_number)`` over the competition's recorded runs: those *started* (a start
    signal, or a settled outcome) and those *finished*. Keyed by identity so they
    match the expected runs regardless of how they were timed.

    A run is finished when it is **settled**: it produced a time, or the timekeeper
    closed it with a state code. A DNF is as final as a time — the competitor is
    off the course either way, and leaving it outstanding would keep the class at
    99 % with nothing left to record."""
    started, finished = set(), set()
    for run in runs:
        if run.competition_class_id is None or not run.run_type or run.run_number is None:
            continue
        ident = (run.bib_number, run.competition_class_id, run.class_occurrence,
                 run.run_type, run.run_number)
        settled = calc.resolved_run_time(run, precision) is not None or bool(run.status)
        if run.start_signal_id or settled:
            started.add(ident)
        if settled:
            finished.add(ident)
    return started, finished


def _retired(competition):
    """Entry pks whose event is over (DSQ / DNS / DNF on the entry itself — the
    whole-event disqualification set on the participant list writes DSQ here).
    Their unrecorded runs are not outstanding work; see ``_expected_ids``."""
    return {
        entry.pk
        for entry in competition.entries.all()
        if entry.status in DNX_STATUSES
    }


def _expected_ids(cclass, starters, retired, finished_ids):
    """Every run each of a class's starters owes — one identity per practice and
    counted run the class grants. This is the class's expected-run denominator,
    known from the entries and the class alone (no start pattern needed).

    A retired competitor's *unsettled* runs are left out: they are never going to
    be driven or marked, so counting them would peg the class below 100 % for the
    rest of the event. The ones they did drive (or that were marked) stay in, on
    both sides of the tally — those runs happened."""
    idents = []
    for starter in starters:
        entry_pk, class_pk, occurrence = starter.key
        for run_type, count in (
            (TimedRun.RunType.PRACTICE, cclass.practice_runs or 0),
            (TimedRun.RunType.COUNTED, cclass.counted_runs or 0),
        ):
            for number in range(1, count + 1):
                ident = (starter.bib, class_pk, occurrence, run_type, number)
                if entry_pk in retired and ident not in finished_ids:
                    continue
                idents.append(ident)
    return idents


def _classes(competition, started_ids, finished_ids, retired):
    """Per running class: its expected-run tally and a done / running / not-started
    state. Classes are listed in run order; a running class with no registered
    starters still appears (as not-started with no runs)."""
    # One read for both: starters_by_class walks the running classes and so does
    # run_groups, and this is on an endpoint every open browser re-fetches.
    running = competition._running_classes_ordered()
    starters_by_class = competition.starters_by_class(running=running)
    ordered = [cc for group in competition.run_groups(running=running) for cc in group]
    classes = []
    for cc in ordered:
        expected = _expected_ids(cc, starters_by_class.get(cc.pk, []), retired, finished_ids)
        total = len(expected)
        done = sum(1 for ident in expected if ident in finished_ids)
        started = sum(1 for ident in expected if ident in started_ids)
        if total and done >= total:
            status = "done"
        elif started:
            status = "running"
        else:
            status = "not_started"
        classes.append({
            "name": cc.name,
            "scoring": cc.get_scoring_method_display(),
            "total": total,
            "finished": done,
            "status": status,
            "percent": _percent(done, total),
        })
    return classes


def _current(competition, precision, runs):
    """The competitor on course now (the run with the latest timing activity), or
    None before anyone has started. Its identity is read from the run itself, so it
    works whether the run was bound by the start order or typed on Manual timing.
    Times and penalties are only meaningful once the run has finished."""
    run, _slot = autotiming.current_run(competition, runs)
    if run is None:
        return None
    entry = _entry(competition, run.bib_number)
    participant = entry.participant if entry else None
    rt = calc.resolved_run_time(run, precision)
    finished = rt is not None
    penalty = autotiming.penalty_seconds(run, competition)
    pylons, tasks, stop = autotiming.penalty_counts(run, competition)
    return {
        "bib": run.bib_number,
        "name": str(participant) if participant else "",
        "class_name": run.competition_class.name if run.competition_class_id else "",
        "run_label": _run_label(run),
        "club": (participant.club or "") if participant else "",
        "started": run.start_signal_id is not None,
        "finished": finished,
        "run_time": calc.format_clock(rt, precision),
        "total_time": calc.format_clock(rt + penalty, precision) if finished else "",
        "penalty": calc.format_penalty(penalty),
        "pylons": pylons,
        "tasks": tasks,
        "stop": stop,
    }


def _run_label(run):
    if run.run_type and run.run_number:
        return startpattern.RUN_TYPE_SHORT[run.run_type] + str(run.run_number)
    return ""


def _entry(competition, bib):
    if not bib:
        return None
    return (
        EventEntry.objects.select_related("participant")
        .filter(competition=competition, bib_number=bib)
        .first()
    )


def _stats(competition, classes, expected, finished, ctype):
    """Headline counts for the tile row."""
    entries = list(competition.entries.all())
    dnx = sum(1 for e in entries if e.status in DNX_STATUSES)
    classes_done = sum(1 for c in classes if c["status"] == "done")
    marshal_posts = competition.marshal_posts.count() if (
        ctype.penalties_enabled and competition.penalties_by_marshal_posts
    ) else 0
    return {
        "participants": len(entries),
        "classes_total": len(classes),
        "classes_done": classes_done,
        "runs_expected": expected,
        "runs_finished": finished,
        "runs_remaining": max(0, expected - finished),
        "did_not_run": dnx,
        "marshal_posts": marshal_posts,
    }
