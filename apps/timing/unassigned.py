"""Runs recorded against a bib nobody is registered under.

Somebody turns up at the line wearing 47 and the paperwork has not caught up.
The timekeeper has to record the run anyway — that is the whole point of a
timing system — so ``TimedRun.bib_number`` is a loose integer and always has
been: the Manual view keeps a bib it does not recognise and flags it.

What it could not do was say *which* run it was. The Run dropdown is built from
the class, and with no participant there is no class, so the run went down as
"bib 47, some time, no idea which run" — which nothing can score and nothing can
match up later. This module is the missing half:

  * ``run_choices`` — what to offer instead. The union of the runs the *running
    classes* grant: one practice option per practice run any class grants, one
    counted option per counted run any class grants. Events usually run the same
    counts across every class, so the list is exactly right; where they differ it
    is deliberately generous (Class 1 has a practice run and Class 2 does not →
    P1, C1, C2 are all offered), because an option too many is a choice and an
    option too few is a time that cannot be recorded.

  * ``apply`` — "this bib exists now". As soon as an entry carries the bib, the
    run's class can be filled in and the time joins that competitor's results.
    It is folded on in memory and left for ``autotiming.sync_bindings`` to write,
    because it is the same question the start-order binding answers ("who does
    this run belong to?") and so obeys the same rule: a reader binds what it is
    about to render, a writer persists. A GET that writes takes the lock the
    timing rig's own thread needs to record a time.

  * ``unregistered`` — the ones still waiting for a name, for the Results page to
    put in front of the timekeeper. A recorded time that belongs to nobody is not
    an error to hide; it is work outstanding, and the only place anybody looks for
    it is the results.
"""

from apps.participants.models import EventEntry

from .models import TimedRun


def run_choices(competition, running=None):
    """``[(run_type, number), …]`` — every run any running class grants, in the
    order the Run dropdown shows them (practice, then counted).

    Not the *intersection*: a class that grants no practice run simply never uses
    the practice options. Offering the union means the timekeeper can always
    record what actually happened, and the class that finally owns the run
    decides whether it counts."""
    classes = competition._running_classes_ordered() if running is None else running
    practice = max((cc.practice_runs or 0) for cc in classes) if classes else 0
    counted = max((cc.counted_runs or 0) for cc in classes) if classes else 0
    return (
        [(TimedRun.RunType.PRACTICE, n) for n in range(1, practice + 1)]
        + [(TimedRun.RunType.COUNTED, n) for n in range(1, counted + 1)]
    )


def recorded_for_bib(runs, bib, exclude=None):
    """The ``(run_type, number)`` pairs already recorded against ``bib`` on rows
    other than ``exclude`` — the class is deliberately not part of the key.

    A bib with no participant has no class either, so "has this bib already done
    its C1?" can only be asked of the bib. Once the competitor is registered the
    ordinary per-class rule takes over (``views._RowContext.recorded_runs``)."""
    return {
        (run.run_type, run.run_number)
        for run in runs
        if run.bib_number == bib and run.run_type and run.run_number is not None
        and (exclude is None or run.pk != exclude.pk)
    }


def _candidates(runs):
    """Runs carrying a bib but no class — the ones this module is about."""
    return [r for r in runs if r.bib_number and r.competition_class_id is None]


def apply(competition, runs):
    """Fold a class onto every candidate run whose bib is now registered, **in
    memory**, and return ``[(run, changed field names)]`` in the shape
    ``autotiming.sync_bindings`` saves.

    Which class: the first of the participant's class slots that has not already
    got this run recorded, falling back to their first. That is the same choice
    the Manual view makes when a *known* bib is typed (``views._default_class_slot``)
    — the run ends up where it would have if the paperwork had been in first —
    and the timekeeper can move it afterwards, because once the bib resolves the
    Class dropdown is there.

    Costs nothing on an ordinary event: with no candidate runs it reads nothing.
    """
    candidates = _candidates(runs)
    if not candidates:
        return []
    entries = _entries_by_bib(competition, {run.bib_number for run in candidates})
    if not entries:
        return []
    # The running classes once, not once per candidate: age-based assignment
    # resolves a competitor's class by walking them, so asking inside the loop is
    # a query per run (the rule the whole Performance section is about).
    running = competition._running_classes_ordered()
    slots = {
        bib: _class_slots(competition, entry, running)
        for bib, entry in entries.items()
    }
    dirty = []
    for run in candidates:
        slot = _slot_for(slots.get(run.bib_number) or [], run, runs)
        if slot is None:
            continue
        cclass, occurrence = slot
        run.competition_class = cclass
        run.class_occurrence = occurrence
        dirty.append((run, ["competition_class_id", "class_occurrence"]))
    return dirty


def _own_query(competition):
    """The candidate runs, read narrowly. Every entry point here is on a page that
    normally has none of these, so the query asks for them rather than for the
    event's runs — and prefetches the marshal penalties, since the results panel
    resolves each one's total."""
    return list(
        TimedRun.objects.filter(
            competition=competition,
            competition_class__isnull=True,
            bib_number__isnull=False,
        ).prefetch_related("marshal_penalties")
    )


def _entries_by_bib(competition, bibs):
    if competition.is_archived:
        return {
            entry.bib_number: entry
            for entry in competition.entry_rows()
            if entry.bib_number in bibs
        }
    return {
        entry.bib_number: entry
        for entry in EventEntry.objects.filter(competition=competition, bib_number__in=bibs)
        .select_related("participant")
        .prefetch_related("participant__class_assignments__competition_class")
    }


def _class_slots(competition, entry, running):
    """A competitor's class slots as ``[(class, occurrence)]`` — one per entry in
    a class, so a class entered twice yields two."""
    slots, seen = [], {}
    for cclass in competition.classes_for_participant(entry.participant, running=running):
        occurrence = seen.get(cclass.pk, 0)
        seen[cclass.pk] = occurrence + 1
        slots.append((cclass, occurrence))
    return slots


def _slot_for(slots, run, runs):
    """``(class, occurrence)`` for a now-registered bib's run, or None when the
    participant is in no class at all (an entry with no class assignment yet —
    common on a Manual-assignment event, and a reason to leave the run alone
    rather than invent a class for it)."""
    if not slots:
        return None
    taken = {
        (other.competition_class_id, other.class_occurrence)
        for other in runs
        if other.pk != run.pk and other.bib_number == run.bib_number
        and other.run_type == run.run_type and other.run_number == run.run_number
    }
    for cclass, occurrence in slots:
        if (cclass.pk, occurrence) not in taken:
            return (cclass, occurrence)
    return slots[0]


def unregistered(competition, runs=None):
    """The bibs that have runs recorded against them and no competitor, newest
    bib last: ``[{"bib": …, "runs": [TimedRun, …]}, …]``.

    The Results page shows these, because that is the page somebody opens to ask
    "is the event complete?" and these are exactly the times that are not in it.
    A run whose bib *is* registered never appears here even if its class is still
    blank — ``apply`` has already given it one."""
    runs = _own_query(competition) if runs is None else runs
    candidates = _candidates(runs)
    if not candidates:
        return []
    entries = _entries_by_bib(competition, {run.bib_number for run in candidates})
    by_bib = {}
    for run in candidates:
        if run.bib_number in entries:
            continue
        by_bib.setdefault(run.bib_number, []).append(run)
    return [
        {"bib": bib, "runs": sorted(by_bib[bib], key=_run_order)}
        for bib in sorted(by_bib)
    ]


def _run_order(run):
    """Practice runs before counted ones, then by number; unnamed runs last."""
    return (
        run.run_type != TimedRun.RunType.PRACTICE,
        run.run_number is None,
        run.run_number or 0,
        run.pk,
    )
