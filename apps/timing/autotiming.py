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
                # The unpacked identity of the run this slot stands for, so callers
                # (e.g. results) can bind a positionally-timed run back to its
                # competitor/class/run without re-parsing the packed key.
                "class_pk": class_pk,
                "occurrence": occurrence,
                "run_type": slot.run_type,
                "run_number": slot.run_number,
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
    """Runs that have a start signal, in the order they actually started — by
    *arrival* (received_at/id), not the device's clock (see arrangement.py)."""
    runs = [
        run
        for run in TimedRun.objects.filter(competition=competition)
        .select_related("start_signal", "finish_signal")
        .prefetch_related("marshal_penalties")
        if run.start_signal_id
    ]
    runs.sort(key=lambda run: (run.start_signal.received_at, run.start_signal.id))
    return runs


def _signal_activity(run):
    """The run's most recent signal *arrival* (start or finish), or None. Used to
    pick the current competitor by latest activity rather than start order, so a
    finish arriving for an earlier starter still surfaces that run. Arrival, not
    the device clock, so a device whose clock isn't wall-clock still orders right."""
    times = []
    if run.start_signal_id:
        times.append((run.start_signal.received_at, run.start_signal.id))
    if run.finish_signal_id:
        times.append((run.finish_signal.received_at, run.finish_signal.id))
    return max(times) if times else None


def current_index(runs):
    """Index (into ``runs``) of the run with the most recent timing activity — the
    latest start or finish. This is what the right-hand tiles centre on: when
    runners are timed one at a time it is simply the last to start, but if a finish
    comes in for a runner who started earlier (staggered / batched starts) the
    current tile follows that finish instead of staying stuck on the last starter.
    ``-1`` when nothing has a signal yet."""
    best_i, best = -1, None
    for i, run in enumerate(runs):
        if run is None:
            continue
        activity = _signal_activity(run)
        if activity is not None and (best is None or activity > best):
            best, best_i = activity, i
    return best_i


# ----- binding runs to the start order ------------------------------------

def _run_identity(run):
    """A run's competitor-and-run identity, or None when it hasn't got one — the
    key a slot is matched by."""
    if run.competition_class_id and run.run_type and run.run_number is not None:
        return (run.bib_number, run.competition_class_id, run.class_occurrence,
                run.run_type, run.run_number)
    return None


def _slot_identity(slot):
    return (slot["bib"], slot["class_pk"], slot["occurrence"],
            slot["run_type"], slot["run_number"])


def bind_runs(competition):
    """Align recorded runs to the start order. Returns ``(slots, aligned,
    orphans)`` where ``aligned[i]`` is the run occupying ``slots[i]`` (or None),
    and ``orphans`` are started runs the order has no place for.

    A *manual* run (operator-owned identity) claims the slot its identity matches —
    so a run pre-entered on Manual timing shows up pre-filled in its place and the
    positional binding steps over it. The remaining *auto* runs (started, not
    operator-owned) fill the still-empty slots in start order."""
    slots = ordered_slots(competition)
    all_runs = list(
        TimedRun.objects.filter(competition=competition)
        .select_related("start_signal", "finish_signal")
        .prefetch_related("marshal_penalties")
    )
    aligned = [None] * len(slots)

    slots_by_key = {}
    for i, slot in enumerate(slots):
        slots_by_key.setdefault(_slot_identity(slot), []).append(i)

    for run in all_runs:
        if not run.manual_entry:
            continue
        for i in slots_by_key.get(_run_identity(run), []):
            if aligned[i] is None:
                aligned[i] = run
                break

    placed_ids = {run.id for run in aligned if run is not None}
    auto_started = sorted(
        (r for r in all_runs if r.start_signal_id and r.id not in placed_ids),
        key=lambda r: (r.start_signal.received_at, r.start_signal.id),
    )
    pool = iter(auto_started)
    for i in range(len(slots)):
        if aligned[i] is None:
            aligned[i] = next(pool, None)
    orphans = list(pool)
    return slots, aligned, orphans


def sync_bindings(competition):
    """Persist each auto run's bound-slot identity (bib / class / run) onto the run
    so the Manual timing view reads the same competitor the Auto view derives.
    Penalties are *not* copied — both views compute them the one canonical way from
    the posts + the run's own counts (see penalty_seconds), so nothing to cache.
    Manual (operator-owned) runs are left untouched — their identity is authored."""
    slots, aligned, _ = bind_runs(competition)
    for i, run in enumerate(aligned):
        if run is None or run.manual_entry:
            continue
        slot = slots[i]
        wanted = {
            "bib_number": slot["bib"],
            "competition_class_id": slot["class_pk"],
            "class_occurrence": slot["occurrence"],
            "run_type": slot["run_type"],
            "run_number": slot["run_number"],
        }
        changed = [f for f, v in wanted.items() if getattr(run, f) != v]
        if changed:
            for f in changed:
                setattr(run, f, wanted[f])
            run.save(update_fields=[*changed, "updated_at"])


def current_run(competition):
    """The competitor being timed now — the bound run with the latest timing
    activity — with its slot. ``(None, None)`` before anyone has started."""
    slots, aligned, orphans = bind_runs(competition)
    runs_in_order = aligned + orphans
    index = current_index(runs_in_order)
    if index < 0:
        return None, None
    slot = slots[index] if index < len(slots) else None
    return runs_in_order[index], slot


def serialize(competition):
    """The whole Auto timing state: the ordered items (slot + its bound run), the
    current index, the ignored times, and the marshal posts."""
    sync_bindings(competition)
    ctype = competition.competition_type
    precision = ctype.timing_precision
    marshal_mode = ctype.penalties_enabled and competition.penalties_by_marshal_posts
    slots, aligned, orphans = bind_runs(competition)
    posts = list(competition.marshal_posts.all())
    runs_in_order = aligned + orphans  # index lines up with the items below
    items = [
        _item(precision, ctype, marshal_mode, index,
              slots[index] if index < len(slots) else None,
              runs_in_order[index],
              posts)
        for index in range(len(runs_in_order))
    ]
    return {
        "precision": precision,
        "penalties_enabled": ctype.penalties_enabled,
        "items": items,
        # Centre on the run with the latest timing activity (last start, or a
        # finish that just came in for an earlier starter).
        "current_index": current_index(runs_in_order),
        "ignored": _ignored(competition, precision),
        "posts": [{"number": post.number} for post in posts],
    }


def _item(precision, ctype, marshal_mode, index, slot, run, posts):
    start = run.start_signal if run else None
    finish = run.finish_signal if run else None
    rt = calc.resolved_run_time(run, precision) if run else None
    stored = {mp.marshal_post_id: mp for mp in run.marshal_penalties.all()} if run else {}
    lines = _penalty_lines(run, marshal_mode)
    seconds = _penalty_seconds(lines, ctype)
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
        "run_time_manual": bool(run and run.manual_run_time is not None),
        "total_time": total_time,
        # One line per penalty type: its non-editable base, the run field the Auto
        # stepper edits, its value, and the grand count. Drives the +/- steppers.
        "penalties": lines,
        # Grand counts (for the boxes / callers that just want the totals).
        "total_pylons": lines[0]["total"],
        "total_tasks": lines[1]["total"],
        "total_stop": lines[2]["total"],
        "started": start is not None,
        "finished": finish is not None,
        "marshals": _marshals(run, posts, stored),
        # A run with no matching slot (more starts than the order expects).
        "orphan": run is not None and slot is None,
    }


# Per penalty type: (label, the TimedRun/MarshalPenalty count field, the adjust field).
PENALTY_TYPES = (
    ("Pylons", "pylon_count", "pylon_adjust"),
    ("Task", "task_count", "task_adjust"),
    ("Stop line", "stopline_count", "stopline_adjust"),
)


def _penalty_lines(run, marshal_mode):
    """Per penalty type: the non-editable ``base``, the run ``field`` the Auto
    stepper edits, its ``value``, and the grand ``total`` (``max(0, base+value)``).

    Penalties are additive from their real sources so every surface agrees:
      * marshal mode → base = marshal-post sum **plus** any direct counts the
        operator keyed onto a run they own (a purely auto-bound run's own counts
        are a stale cache and ignored); the Auto stepper nudges an ``*_adjust`` on
        top. So marshal penalties on an operator-selected run still add up.
      * otherwise (non-marshal timing) → base = 0 and the stepper edits the run's
        own ``*_count``, shared with the Manual view."""
    stored = list(run.marshal_penalties.all()) if (run is not None and marshal_mode) else []
    owns = run is not None and run.manual_entry
    lines = []
    for label, count_field, adjust_field in PENALTY_TYPES:
        own = getattr(run, count_field) if run is not None else 0
        if marshal_mode:
            marshal_sum = sum(getattr(mp, count_field) for mp in stored)
            base = marshal_sum + (own if owns else 0)
            field, value = adjust_field, (getattr(run, adjust_field) if run is not None else 0)
        else:
            base = 0
            field, value = count_field, own
        lines.append({
            "label": label, "base": base, "field": field, "value": value,
            "total": max(0, base + value),
        })
    return lines


def _penalty_seconds(lines, ctype):
    if not ctype.penalties_enabled:
        return 0
    amounts = (ctype.pylon_penalty, ctype.task_penalty, ctype.stop_line_penalty)
    return sum(line["total"] * (amount or 0) for line, amount in zip(lines, amounts))


def penalty_seconds(run, competition):
    """The whole penalty seconds a run adds, resolved the one canonical way (marshal
    posts + adjust for a marshal-driven run, else the run's own counts). Shared by
    the Auto view, the Manual view total and the results engine."""
    ctype = competition.competition_type
    marshal_mode = ctype.penalties_enabled and competition.penalties_by_marshal_posts
    return _penalty_seconds(_penalty_lines(run, marshal_mode), ctype)


def penalty_counts(run, competition):
    """The run's grand ``(pylons, tasks, stop_line)`` penalty counts, resolved the
    same canonical way as ``penalty_seconds`` — for callers that show the tallies
    rather than the seconds (e.g. the Dashboard's current-competitor chips)."""
    ctype = competition.competition_type
    marshal_mode = ctype.penalties_enabled and competition.penalties_by_marshal_posts
    lines = _penalty_lines(run, marshal_mode)
    return lines[0]["total"], lines[1]["total"], lines[2]["total"]


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
            "manual": signal.is_manual, "entered": signal.entered}


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
