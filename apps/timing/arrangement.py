"""Placing timing signals into runs.

Pairing is *causal*: a finish can only join a start that came before it. When a
finish arrives it closes the oldest still-open start; if none is open it stands
on its own line with a blank start. A start never adopts a finish that arrived
before it — those orphan finishes keep their own lines. The operator can override
a pairing by dragging a time onto a run's slot (``assign``), which is rejected if
it would put a start after its finish.

Each signal is referenced by at most one run (OneToOne on both slots), so a time
is never used twice. Ignoring a signal removes it from its run.
"""

from .models import TimedRun, TimingSignal


def ingest(signal, settings):
    """Place a newly arrived (or restored) start/finish signal into a run."""
    role = effective_role(signal, settings)
    if role == TimingSignal.Role.START:
        # Fill the oldest pre-entered placeholder (a row with a bib/run but no
        # times yet) so times populate those bottom-first; else open a new run. A
        # row that already carries a hand-typed run time is complete, not awaiting
        # a measurement, so it is skipped.
        placeholder = (
            TimedRun.objects.filter(
                competition=signal.competition,
                start_signal__isnull=True,
                finish_signal__isnull=True,
                manual_run_time__isnull=True,
            )
            .order_by("id")
            .first()
        )
        if placeholder is not None:
            placeholder.start_signal = signal
            placeholder.save(update_fields=["start_signal", "updated_at"])
        else:
            TimedRun.objects.create(competition=signal.competition, start_signal=signal)
    elif role == TimingSignal.Role.FINISH:
        open_run = _oldest_open_run(signal)
        if open_run is not None:
            open_run.finish_signal = signal
            open_run.save(update_fields=["finish_signal", "updated_at"])
        else:
            TimedRun.objects.create(competition=signal.competition, finish_signal=signal)


def effective_role(signal, settings):
    """The role a signal plays. With distinct start/finish channels it is fixed by
    port. With a single light barrier (start_channel == finish_channel) the one
    channel alternates: a pulse closes the open run if there is one, otherwise it
    opens a new run — so start, finish, start, finish… on the same channel."""
    if settings.start_channel != settings.finish_channel:
        return signal.role(settings)
    if signal.port != settings.start_channel:
        return None
    has_open_run = TimedRun.objects.filter(
        competition=signal.competition,
        start_signal__isnull=False,
        finish_signal__isnull=True,
    ).exists()
    return TimingSignal.Role.FINISH if has_open_run else TimingSignal.Role.START


def _oldest_open_run(finish_signal):
    """The open run (has a start, no finish) whose start is earliest and no later
    than this finish — the one this finish closes."""
    candidates = [
        run
        for run in TimedRun.objects.filter(
            competition=finish_signal.competition,
            start_signal__isnull=False,
            finish_signal__isnull=True,
        ).select_related("start_signal")
        if run.start_signal.device_time <= finish_signal.device_time
    ]
    candidates.sort(key=lambda run: (run.start_signal.device_time, run.start_signal_id))
    return candidates[0] if candidates else None


def detach(signal):
    """Remove a signal from whatever run holds it (e.g. when it is ignored). If
    that leaves the run empty, delete it — unless it still carries operator entry
    (a bib, a run, or penalties), in which case it stays as a placeholder so a
    pre-entered starter isn't lost when a wrong time is ignored."""
    run = _run_holding(signal)
    if run is None:
        return
    if run.start_signal_id == signal.id:
        run.start_signal = None
    if run.finish_signal_id == signal.id:
        run.finish_signal = None
    if run.start_signal_id is None and run.finish_signal_id is None and _is_blank(run):
        run.delete()
    else:
        run.save()


def _is_blank(run):
    return not (
        run.bib_number or run.run_type or run.manual_run_time is not None
        or run.pylon_count or run.task_count or run.stopline_count
    )


def _run_holding(signal):
    return (
        TimedRun.objects.filter(start_signal=signal).first()
        or TimedRun.objects.filter(finish_signal=signal).first()
    )


def assign(signal, target_run, slot):
    """Drag ``signal`` into ``target_run``'s ``slot`` ('start' or 'finish').
    Returns False (caller shows an error wiggle) if it would put a start after
    its finish or a finish before its start. Whatever occupied the slot, and the
    row the signal came from, are preserved by giving displaced signals their own
    rows — nothing is lost."""
    if slot == "start" and target_run.finish_signal:
        if signal.device_time > target_run.finish_signal.device_time:
            return False
    if slot == "finish" and target_run.start_signal:
        if signal.device_time < target_run.start_signal.device_time:
            return False

    occupant_id = target_run.start_signal_id if slot == "start" else target_run.finish_signal_id
    if occupant_id == signal.id:
        return True  # already there

    detach(signal)  # may delete the signal's old (now-empty) row
    target_run = TimedRun.objects.filter(pk=target_run.pk).first()
    if target_run is None:  # its only content was the dragged signal — recreate
        target_run = TimedRun(competition=signal.competition)

    if occupant_id and occupant_id != signal.id:
        occupant = TimingSignal.objects.filter(pk=occupant_id).first()
        if occupant is not None:
            if slot == "start":
                TimedRun.objects.create(competition=occupant.competition, start_signal=occupant)
            else:
                TimedRun.objects.create(competition=occupant.competition, finish_signal=occupant)

    if slot == "start":
        target_run.start_signal = signal
    else:
        target_run.finish_signal = signal
    target_run.save()
    return True


def rows(competition):
    """Every run for the competition, newest first (by the run's earliest time),
    so fresh times appear at the top without scrolling. Placeholders (no times
    yet) sort above all timed rows, newest-added first."""
    runs = list(
        TimedRun.objects.filter(competition=competition).select_related(
            "start_signal", "finish_signal", "competition_class"
        )
    )
    runs.sort(key=_sort_key, reverse=True)
    return runs


def _sort_key(run):
    signal = run.start_signal or run.finish_signal
    if signal is None:
        # Placeholder: bucket 1 (above timed rows), newest-added (created_at) first.
        return (1, run.created_at, run.id)
    # Timed: bucket 0, newest time first.
    return (0, signal.device_time, signal.received_at, signal.id)
