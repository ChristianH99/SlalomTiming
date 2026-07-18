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
    role = signal.role(settings)
    if role == TimingSignal.Role.START:
        TimedRun.objects.create(competition=signal.competition, start_signal=signal)
    elif role == TimingSignal.Role.FINISH:
        open_run = _oldest_open_run(signal)
        if open_run is not None:
            open_run.finish_signal = signal
            open_run.save(update_fields=["finish_signal", "updated_at"])
        else:
            TimedRun.objects.create(competition=signal.competition, finish_signal=signal)


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
    """Remove a signal from whatever run holds it (e.g. when it is ignored),
    deleting the run if that leaves it empty."""
    run = _run_holding(signal)
    if run is None:
        return
    if run.start_signal_id == signal.id:
        run.start_signal = None
    if run.finish_signal_id == signal.id:
        run.finish_signal = None
    if run.start_signal_id is None and run.finish_signal_id is None:
        run.delete()
    else:
        run.save()


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
    so fresh times appear at the top without scrolling."""
    runs = list(
        TimedRun.objects.filter(competition=competition).select_related(
            "start_signal", "finish_signal", "competition_class"
        )
    )
    runs.sort(key=_anchor_key, reverse=True)
    return runs


def _anchor_key(run):
    signal = run.start_signal or run.finish_signal
    return (signal.device_time, signal.received_at, signal.id)
