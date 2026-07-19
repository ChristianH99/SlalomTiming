"""Auto timing: drive the live view from the known start order instead of typed
bibs.

The start order comes from ``Competition.start_lists()`` (run order × start
pattern) as a flat list of *slots* — one competitor's one run, e.g. bib 2's
second counted run. Times are still captured as ``TimingSignal`` → ``TimedRun``
by the ordinary arrangement (a start opens a run, a finish closes the oldest open
one); auto timing binds them to slots **positionally**: the n-th started run is
the n-th slot in the order. So no bib is typed — identity is the order, and the
operator only reorders the list (persisted as ``auto_timing_order``) or ignores a
wrong time.

The competitor being timed now (the last one to have started) is the *current*
run; it's what the previous/current/next tiles centre on and what the marshal
posts judge. Each post's running penalty for that run is a ``MarshalPenalty``.
"""

from apps.competitions import startpattern
from apps.participants.models import EventEntry

from . import calc
from .models import TimedRun


def slot_key(entry_pk, class_pk, occurrence, run_type, run_number):
    """A stable id for one competitor's one run — survives re-computation so a
    saved manual order still lines up after registrations change."""
    return f"{entry_pk}:{class_pk}:{occurrence}:{run_type}:{run_number}"


def computed_slots(competition):
    """The start order as a flat list of slot dicts (run order × start pattern).
    Each slot carries its run-group index and label (the classes starting
    together, e.g. "5, 6") so the list can show a divider between runs."""
    slots = []
    for group_index, (run_group, slot_list) in enumerate(competition.start_lists()):
        group_label = ", ".join(cc.name for cc in run_group)
        for slot in slot_list:
            entry_pk, class_pk, occurrence = slot.starter.key
            slots.append({
                "key": slot_key(entry_pk, class_pk, occurrence, slot.run_type, slot.run_number),
                "entry_pk": entry_pk,
                "bib": slot.starter.bib,
                "name": slot.starter.name,
                "class_name": slot.starter.class_name,
                "run_label": startpattern.RUN_TYPE_SHORT[slot.run_type] + str(slot.run_number),
                "group_index": group_index,
                "group_label": group_label,
            })
    return slots


def ordered_slots(competition):
    """The start order honouring a saved manual override: slots named in the
    override come first in that order; any not named (newly registered) follow in
    computed order. Keys in the override that no longer exist are dropped."""
    slots = computed_slots(competition)
    override = competition.auto_timing_order or []
    if not override:
        return slots
    by_key = {slot["key"]: slot for slot in slots}
    ordered = [by_key.pop(key) for key in override if key in by_key]
    ordered += [slot for slot in slots if slot["key"] in by_key]
    return ordered


def started_runs(competition):
    """Runs that have a start signal, in the order they actually started."""
    runs = [
        run
        for run in TimedRun.objects.filter(competition=competition)
        .select_related("start_signal", "finish_signal")
        .prefetch_related("marshal_penalties")
        if run.start_signal_id
    ]
    runs.sort(key=lambda run: (run.start_signal.device_time, run.start_signal.received_at, run.id))
    return runs


def current_run(competition):
    """The competitor being timed now — the last to have started — with the slot
    it maps to. ``(None, None)`` before anyone has started."""
    runs = started_runs(competition)
    if not runs:
        return None, None
    index = len(runs) - 1
    slots = ordered_slots(competition)
    slot = slots[index] if index < len(slots) else None
    return runs[-1], slot


def serialize(competition):
    """The whole Auto timing state: the ordered items (slot + its bound run), the
    current index, the ignored times, and the marshal posts."""
    ctype = competition.competition_type
    precision = ctype.timing_precision
    slots = ordered_slots(competition)
    runs = started_runs(competition)
    posts = list(competition.marshal_posts.all())
    count = max(len(slots), len(runs))
    items = [
        _item(precision, ctype, index,
              slots[index] if index < len(slots) else None,
              runs[index] if index < len(runs) else None,
              posts)
        for index in range(count)
    ]
    return {
        "precision": precision,
        "penalties_enabled": ctype.penalties_enabled,
        "items": items,
        # The just-started run stays centred until the next one starts.
        "current_index": len(runs) - 1,
        "ignored": _ignored(competition, precision),
        "posts": [{"number": post.number} for post in posts],
    }


def _item(precision, ctype, index, slot, run, posts):
    start = run.start_signal if run else None
    finish = run.finish_signal if run else None
    rt = calc.run_time(
        start.device_time if start else None,
        finish.device_time if finish else None,
        precision,
    )
    stored = {mp.marshal_post_id: mp for mp in run.marshal_penalties.all()} if run else {}
    pylons, tasks, stop, seconds = _totals(run, stored, ctype)
    total_time = calc.format_precision(rt + seconds, precision) if rt is not None else ""
    return {
        "index": index,
        "key": slot["key"] if slot else None,
        "bib": slot["bib"] if slot else None,
        "name": slot["name"] if slot else "",
        "class_name": slot["class_name"] if slot else "",
        "run_label": slot["run_label"] if slot else "",
        "group_index": slot["group_index"] if slot else None,
        "group_label": slot["group_label"] if slot else "",
        "run_id": run.id if run else None,
        "start": _signal(start, precision),
        "finish": _signal(finish, precision),
        "run_time": calc.format_precision(rt, precision),
        "total_time": total_time,
        # Grand totals (marshal posts + timekeeper adjustment) and the raw
        # adjustment, so the timekeeper's +/- can read and change it.
        "total_pylons": pylons,
        "total_tasks": tasks,
        "total_stop": stop,
        "pylon_adjust": run.pylon_adjust if run else 0,
        "task_adjust": run.task_adjust if run else 0,
        "started": start is not None,
        "finished": finish is not None,
        "marshals": _marshals(run, posts, stored),
        # A run with no matching slot (more starts than the order expects).
        "orphan": run is not None and slot is None,
    }


def _totals(run, stored, ctype):
    """Grand pylon/task/stop-line counts for a run: the marshal posts summed, then
    the timekeeper's signed adjustment applied to pylons/tasks (clamped at zero).
    Returns the counts plus the penalty seconds they add."""
    pylons = sum(mp.pylon_count for mp in stored.values())
    tasks = sum(mp.task_count for mp in stored.values())
    stop = sum(mp.stopline_count for mp in stored.values())
    if run is not None:
        pylons = max(0, pylons + run.pylon_adjust)
        tasks = max(0, tasks + run.task_adjust)
    seconds = 0
    if ctype.penalties_enabled:
        seconds = (
            pylons * (ctype.pylon_penalty or 0)
            + tasks * (ctype.task_penalty or 0)
            + stop * (ctype.stop_line_penalty or 0)
        )
    return pylons, tasks, stop, seconds


def _marshals(run, posts, stored):
    """One box per post for this run: its pylon/task counts, whether it hit the
    stop line, its submitted (locked) state, and the per-task breakdown the
    timekeeper's pop-up shows. Posts with nothing entered read as zero."""
    boxes = []
    for post in posts:
        mp = stored.get(post.id)
        boxes.append({
            "number": post.number,
            "pylons": mp.pylon_count if mp else 0,
            "tasks": mp.task_count if mp else 0,
            "stop_line": bool(mp and mp.stopline_count),
            "submitted": bool(mp and mp.submitted),
            "entered": mp is not None,
            "detail": _detail_rows(post, mp),
        })
    return boxes


def _detail_rows(post, mp):
    """Every task the post watches with its recorded penalty, for the pop-up."""
    detail = (mp.detail if mp else None) or {}
    tasks = detail.get("tasks", {}) if isinstance(detail, dict) else {}
    rows = []
    for number in post.task_numbers():
        cell = tasks.get(str(number)) if isinstance(tasks, dict) else None
        cell = cell if isinstance(cell, dict) else {}
        rows.append({
            "task": number,
            "pylons": int(cell.get("pylons", 0) or 0),
            "task_penalty": bool(cell.get("task")),
        })
    return {
        "tasks": rows,
        "handles_stop_line": post.handles_stop_line,
        "stop_line": bool(detail.get("stop_line")) if isinstance(detail, dict) else False,
    }


def _signal(signal, precision):
    if signal is None:
        return None
    return {"id": signal.id, "time": format_device_time(signal.device_time, precision),
            "manual": signal.is_manual}


def _ignored(competition, precision):
    from .models import TimingSettings

    settings = TimingSettings.load()
    return [
        {
            "id": signal.id,
            "role": signal.role(settings) or "",
            "time": format_device_time(signal.device_time, precision),
            "manual": signal.is_manual,
        }
        for signal in competition.timing_signals.filter(ignored=True).order_by("-received_at")
    ]


def format_device_time(t, precision):
    """hh:mm:ss with the fractional second truncated to the device precision."""
    if t is None:
        return ""
    frac = t.microsecond // (10 ** (6 - precision))
    return f"{t:%H:%M:%S}." + str(frac).zfill(precision)


def marshal_state(competition, post_number):
    """What a marshal post's page needs: the current competitor (bib/name/club),
    the run id to attach penalties to, and this post's already-stored entry so a
    reloading marshal doesn't lose the submitted flag."""
    run, slot = current_run(competition)
    if run is None or slot is None:
        return {"run_id": None}
    club = ""
    entry = (
        EventEntry.objects.select_related("participant")
        .filter(competition=competition, pk=slot["entry_pk"])
        .first()
    )
    if entry is not None:
        club = entry.participant.club or ""
    # detail lets the marshal's board resume its exact per-task state after an
    # unlock; submitted == locked.
    penalty = {"detail": {}, "submitted": False}
    post = competition.marshal_posts.filter(number=post_number).first()
    if post is not None:
        mp = run.marshal_penalties.filter(marshal_post=post).first()
        if mp is not None:
            penalty = {"detail": mp.detail or {}, "submitted": mp.submitted}
    return {
        "run_id": run.id,
        "bib": slot["bib"],
        "name": slot["name"],
        "club": club,
        "penalty": penalty,
    }
