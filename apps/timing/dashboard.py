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
run is **done** once a recorded ``TimedRun`` with a matching identity has a resolved
time, so both the pattern-driven (Auto timing) and free-order (Manual timing) ways
of recording a run feed the same tally.

``serialize`` is called on page load and re-run on every WebSocket nudge (the
shared ``timing_live`` group), so the overview updates as times land.
"""

from apps.competitions import startpattern
from apps.participants.models import EventEntry

from . import autotiming, calc
from .models import TimedRun


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
    classes = _classes(competition, started_ids, finished_ids)
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
    signal, or a resolved time) and those *finished* (a resolved time). Keyed by
    identity so they match the expected runs regardless of how they were timed."""
    started, finished = set(), set()
    for run in runs:
        if run.competition_class_id is None or not run.run_type or run.run_number is None:
            continue
        ident = (run.bib_number, run.competition_class_id, run.class_occurrence,
                 run.run_type, run.run_number)
        done = calc.resolved_run_time(run, precision) is not None
        if run.start_signal_id or done:
            started.add(ident)
        if done:
            finished.add(ident)
    return started, finished


def _expected_ids(cclass, starters):
    """Every run each of a class's starters owes — one identity per practice and
    counted run the class grants. This is the class's expected-run denominator,
    known from the entries and the class alone (no start pattern needed)."""
    idents = []
    for starter in starters:
        _, class_pk, occurrence = starter.key
        for run_type, count in (
            (TimedRun.RunType.PRACTICE, cclass.practice_runs or 0),
            (TimedRun.RunType.COUNTED, cclass.counted_runs or 0),
        ):
            for number in range(1, count + 1):
                idents.append((starter.bib, class_pk, occurrence, run_type, number))
    return idents


def _classes(competition, started_ids, finished_ids):
    """Per running class: its expected-run tally and a done / running / not-started
    state. Classes are listed in run order; a running class with no registered
    starters still appears (as not-started with no runs)."""
    starters_by_class = competition.starters_by_class()
    ordered = [cc for group in competition.run_groups() for cc in group]
    classes = []
    for cc in ordered:
        expected = _expected_ids(cc, starters_by_class.get(cc.pk, []))
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
        "run_time": calc.format_precision(rt, precision),
        "total_time": calc.format_precision(rt + penalty, precision) if finished else "",
        "penalty": competition.competition_type.format_time(penalty),
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


# Non-finishing entry statuses, surfaced as a single "did not run" count.
DNX_STATUSES = (EventEntry.Status.DNS, EventEntry.Status.DNF, EventEntry.Status.DSQ)


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
