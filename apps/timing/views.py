import datetime
import json
from collections import Counter

from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView, UpdateView

from apps.competitions.models import Competition, CompetitionClass
from apps.participants.models import EventEntry

from . import arrangement, autotiming, calc, cp540, dashboard
from .forms import TimingSettingsForm
from .ingest import record_signal
from .models import MarshalPenalty, TimedRun, TimingSettings, TimingSignal
from .services import notify_live


def broadcast_live():
    """Nudge every open live-timing view to re-fetch the arrangement. Works from a
    request handler or a background thread (see services.notify_live)."""
    notify_live()


class DashboardView(TemplateView):
    """The organiser overview: a live, read-only status view of the whole event
    (overall run progress, per-class state, the competitor on course, headline
    counts). Read-only and derived from the same data the timing views use."""

    template_name = "timing/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        context["competition"] = competition
        if competition is not None:
            context["overview"] = dashboard.serialize(competition)
        return context


def dashboard_state(request):
    """The organiser overview as JSON — fetched on load and on every WebSocket
    nudge so the dashboard updates live as times land."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"competition": False})
    data = dashboard.serialize(competition)
    data["competition"] = True
    return JsonResponse(data)


class TimingSettingsView(UpdateView):
    """The single timing-rig settings row. 'Save' persists the settings; the
    device-specific 'Start' control opens the simulator, or connects/disconnects
    the CP540 reader thread (apps/timing/cp540.py)."""

    form_class = TimingSettingsForm
    template_name = "timing/settings.html"
    success_url = reverse_lazy("timing:settings")

    def get_object(self, queryset=None):
        return TimingSettings.load()

    def form_valid(self, form):
        response = super().form_valid(form)
        action = self.request.POST.get("action")
        settings = self.object
        # Only one source may feed the database at a time: selecting any device
        # other than the CP540 stops its reader.
        if settings.device != TimingSettings.Device.CP540:
            cp540.reader.stop()

        if action == "connect" and settings.device == TimingSettings.Device.CP540:
            cp540.reader.start(settings.ip_address, settings.port)
            messages.info(
                self.request,
                f"Connecting to the Tag Heuer CP540 at {settings.ip_address}:{settings.port}…",
            )
        elif action == "disconnect":
            cp540.reader.stop()
            messages.info(self.request, "Disconnected from the timing device.")
        else:
            messages.success(self.request, "Timing settings saved.")
        return response


def cp540_status(request):
    """Live CP540 connection state + recent raw lines, polled by the settings
    page so the operator can see the device stream and debug the link."""
    return JsonResponse(cp540.reader.snapshot())


@require_POST
def timing_input_lock(request):
    """Toggle the timing input lock (the red switch on the Auto/Manual pages): while
    on, every incoming time is sent straight to the ignore list. Persisted on the
    timing rig so it applies to every device and both views."""
    settings = TimingSettings.load()
    settings.ignore_incoming = bool(_json_body(request).get("locked"))
    settings.save(update_fields=["ignore_incoming"])
    broadcast_live()
    return JsonResponse({"ok": True, "locked": settings.ignore_incoming})


class SimulatorView(TemplateView):
    """Standalone timing-device emulator, opened in its own tab — no app shell."""

    template_name = "timing/simulator.html"


class TimingLiveView(TemplateView):
    """The operator's live timing view for the active competition: incoming start
    and finish times paired into runs, with bib/class/run and penalty entry."""

    template_name = "timing/live.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        context["competition"] = competition
        if competition is not None:
            context["arrangement"] = serialize_arrangement(competition)
        return context


class AutoTimingView(TemplateView):
    """The order-driven live view: the start order down the left, and the
    previous/current/next competitors with their times and each marshal post's
    running penalty on the right."""

    template_name = "timing/auto.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        context["competition"] = competition
        if competition is not None:
            context["auto"] = autotiming.serialize(competition)
        return context


def auto_arrangement(request):
    """Auto timing state as JSON — fetched on load and on every WebSocket nudge."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"competition": False})
    data = autotiming.serialize(competition)
    data["competition"] = True
    return JsonResponse(data)


@require_POST
def auto_reorder(request):
    """Persist a manual start-order override: a list of slot keys, kept only for
    the keys that currently exist."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    posted = payload.get("order")
    if not isinstance(posted, list):
        return JsonResponse({"ok": False, "error": "order must be a list."}, status=400)
    known = {slot["key"] for slot in autotiming.computed_slots(competition)}
    competition.auto_timing_order = [key for key in posted if key in known]
    competition.save(update_fields=["auto_timing_order"])
    broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def auto_reset_order(request):
    """Drop the manual override so the order re-derives from run order + pattern."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    competition.auto_timing_order = []
    competition.save(update_fields=["auto_timing_order"])
    broadcast_live()
    return JsonResponse({"ok": True})


# ----- marshal posts <-> auto timing link -----

def marshal_state(request):
    """The current competitor (and this post's stored penalty) for a marshal
    post's page. ``?post=N`` names the post."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"run_id": None})
    try:
        post_number = int(request.GET.get("post"))
    except (TypeError, ValueError):
        return JsonResponse({"run_id": None})
    return JsonResponse(autotiming.marshal_state(competition, post_number))


@require_POST
def marshal_submit(request):
    """A marshal post's penalty for the run it's judging: the aggregate counts, the
    per-task breakdown, and whether it's submitted. Upserts one row per (run,post).
    A submitted (locked) row is refused — only a timekeeper unlock reopens it."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    post = competition.marshal_posts.filter(number=payload.get("post")).first()
    if run is None or post is None:
        return JsonResponse({"ok": False, "error": "Unknown run or post."}, status=404)
    existing = MarshalPenalty.objects.filter(timed_run=run, marshal_post=post).first()
    if existing is not None and existing.submitted:
        return JsonResponse({"ok": False, "locked": True}, status=409)
    detail = payload.get("detail")
    MarshalPenalty.objects.update_or_create(
        timed_run=run,
        marshal_post=post,
        defaults={
            "pylon_count": _as_count(payload.get("pylon_count")),
            "task_count": _as_count(payload.get("task_count")),
            "stopline_count": _as_count(payload.get("stopline_count")),
            "detail": detail if isinstance(detail, dict) else {},
            "submitted": bool(payload.get("submitted")),
        },
    )
    broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def marshal_unlock(request):
    """Timekeeper action: reopen a submitted marshal penalty so the marshal can
    edit and re-submit it. Identified by the run and post number."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    post = competition.marshal_posts.filter(number=payload.get("post")).first()
    if run is None or post is None:
        return JsonResponse({"ok": False, "error": "Unknown run or post."}, status=404)
    mp = MarshalPenalty.objects.filter(timed_run=run, marshal_post=post).first()
    if mp is not None and mp.submitted:
        mp.submitted = False
        mp.save(update_fields=["submitted", "updated_at"])
        broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def marshal_lock_all(request):
    """Timekeeper action: lock (submit) every post for a run at once. A post that
    hasn't entered anything gets a zero row locked, so the whole run reads green."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    if run is None:
        return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)
    for post in competition.marshal_posts.all():
        MarshalPenalty.objects.update_or_create(
            timed_run=run, marshal_post=post, defaults={"submitted": True},
        )
    broadcast_live()
    return JsonResponse({"ok": True})


def _resolve_run_and_post(competition, payload):
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    post = competition.marshal_posts.filter(number=payload.get("post")).first()
    return run, post


@require_POST
def marshal_lock(request):
    """Timekeeper locks (submits) a single post at its current values."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    run, post = _resolve_run_and_post(competition, _json_body(request))
    if run is None or post is None:
        return JsonResponse({"ok": False, "error": "Unknown run or post."}, status=404)
    MarshalPenalty.objects.update_or_create(
        timed_run=run, marshal_post=post, defaults={"submitted": True},
    )
    broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def marshal_task_edit(request):
    """Timekeeper edits one task's pylons (or the stop line) on a *locked* post,
    then the aggregate counts are recomputed from the per-task detail."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run, post = _resolve_run_and_post(competition, payload)
    if run is None or post is None:
        return JsonResponse({"ok": False, "error": "Unknown run or post."}, status=404)
    mp = MarshalPenalty.objects.filter(timed_run=run, marshal_post=post).first()
    if mp is None or not mp.submitted:
        return JsonResponse({"ok": False, "error": "Post isn't locked."}, status=409)
    detail = mp.detail if isinstance(mp.detail, dict) else {}
    tasks = detail.get("tasks") if isinstance(detail.get("tasks"), dict) else {}
    if "task" in payload:
        try:
            key = str(int(payload["task"]))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Bad task."}, status=400)
        pylons = _as_count(payload.get("pylons"))
        if pylons > 0:
            tasks[key] = {"pylons": pylons}   # editing pylons clears any task penalty
        else:
            tasks.pop(key, None)
    if "stop_line" in payload:
        detail["stop_line"] = bool(payload.get("stop_line"))
    detail["tasks"] = tasks
    mp.detail = detail
    mp.pylon_count = sum(int((v or {}).get("pylons", 0) or 0) for v in tasks.values())
    mp.task_count = sum(1 for v in tasks.values() if isinstance(v, dict) and v.get("task"))
    mp.stopline_count = 1 if detail.get("stop_line") else 0
    mp.save()
    broadcast_live()
    return JsonResponse({"ok": True})


# ----- exclusive post claims (one device edits a post at a time) -----

@require_POST
def marshal_claim(request):
    """Claim a post (or refresh the claim) for a device token. Refused if a
    different, still-live device already holds it."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    token = str(payload.get("token") or "")[:64]
    post = competition.marshal_posts.filter(number=payload.get("post")).first()
    if post is None or not token:
        return JsonResponse({"ok": False, "error": "Unknown post."}, status=404)
    now = timezone.now()
    if post.claimed_by_other(token, now):
        return JsonResponse({"ok": False, "taken": True})
    post.claim_token = token
    post.claim_seen = now
    post.save(update_fields=["claim_token", "claim_seen"])
    return JsonResponse({"ok": True})


@require_POST
def marshal_release(request):
    """Release a post claim (on change-post or when the page closes)."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": True})
    payload = _json_body(request)
    token = str(payload.get("token") or "")
    post = competition.marshal_posts.filter(number=payload.get("post")).first()
    if post is not None and post.claim_token and post.claim_token == token:
        post.claim_token = ""
        post.claim_seen = None
        post.save(update_fields=["claim_token", "claim_seen"])
    return JsonResponse({"ok": True})


def marshal_claims(request):
    """Post numbers currently held by *another* device — the dropdown greys these."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"taken": []})
    token = request.GET.get("token") or ""
    now = timezone.now()
    taken = [p.number for p in competition.marshal_posts.all() if p.claimed_by_other(token, now)]
    return JsonResponse({"taken": taken})


AUTO_ADJUST_SIGNED = ("pylon_adjust", "task_adjust", "stopline_adjust")
AUTO_ADJUST_COUNTS = ("pylon_count", "task_count", "stopline_count")


@require_POST
def auto_penalty_adjust(request):
    """Timekeeper's manual +/- to a run's penalties from the Auto view. In marshal
    mode a non-owned run's stepper nudges a signed ``*_adjust`` on top of the post
    totals; otherwise it sets the run's own ``*_count`` directly (shared with the
    Manual view)."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    if run is None:
        return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)
    fields = []
    for field in AUTO_ADJUST_SIGNED:
        if field in payload:
            setattr(run, field, _as_signed(payload.get(field)))
            fields.append(field)
    for field in AUTO_ADJUST_COUNTS:
        if field in payload:
            setattr(run, field, _as_count(payload.get(field)))
            fields.append(field)
    if fields:
        run.save(update_fields=[*fields, "updated_at"])
        broadcast_live()
    return JsonResponse({"ok": True})


# ----- signal ingestion (called by the simulator / device) -----

@csrf_exempt  # a physical timing device posts here and can't carry a CSRF token
@require_POST
def timing_signal(request):
    """Receive one raw timing signal (running number, port, time), record it
    stamped with the active competition, and place it into the run arrangement.

    This is the simulator's door. Only one source feeds the database at a time, so
    it is refused unless the simulator is the selected device — otherwise a stray
    simulator tab left open could inject times while the CP540 is live."""
    if TimingSettings.load().device != TimingSettings.Device.SIMULATOR:
        return JsonResponse(
            {"ok": False, "error": "The timing device isn’t the simulator; these signals are ignored."},
            status=409,
        )

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Malformed request."}, status=400)

    try:
        running_number = int(payload["running_number"])
        port = int(payload["port"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "running_number and port are required integers."}, status=400)

    if running_number < 1 or not (1 <= port <= 4):
        return JsonResponse({"ok": False, "error": "running_number ≥ 1 and port 1–4 required."}, status=400)

    device_time = _parse_device_time(payload.get("time"))
    if device_time is None:
        return JsonResponse({"ok": False, "error": "time must be hh:mm:ss.mmm."}, status=400)

    signal = record_signal(
        running_number, port, bool(payload.get("is_manual")), device_time,
        source=str(payload.get("source", "simulator")),
    )
    # signal is None when the DB was busy and the time went to the recovery file;
    # it wasn't lost, so still report success.
    return JsonResponse({"ok": True, "id": signal.id if signal else None, "captured": signal is None})


def _parse_device_time(raw):
    """Parse an "hh:mm:ss.mmm" (or "hh:mm:ss") string into a time, or None."""
    if not isinstance(raw, str):
        return None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


# ----- live arrangement: read + mutate -----

def timing_arrangement(request):
    """The current arrangement as JSON — fetched on load and whenever a WebSocket
    nudge says signals changed."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"competition": False, "rows": [], "ignored": []})
    data = serialize_arrangement(competition)
    data["competition"] = True
    return JsonResponse(data)


@require_POST
def timing_run_update(request):
    """Set bib / class / run / penalties on a run."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    if run is None:
        return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)

    if "bib_number" in payload:
        # A new bib re-resolves the class (clearing the bib clears the class), and
        # resets the run so it re-derives for the new participant. The default
        # class is the participant's first slot they haven't yet completed.
        run.bib_number = _as_positive_int(payload.get("bib_number"))
        run.run_type, run.run_number = "", None
        entry = _resolve_entry(competition, run.bib_number)
        slot = _default_class_slot(competition, entry.participant if entry else None, run)
        run.competition_class, run.class_occurrence = slot if slot else (None, 0)
    if "class_key" in payload:
        run.competition_class, run.class_occurrence = _parse_class_key(
            payload.get("class_key"), competition
        )
        run.run_type, run.run_number = "", None  # class changed → re-derive the run
    if "run_value" in payload:
        run.run_type, run.run_number = _parse_run_value(payload.get("run_value"))
    # Once a class is set (auto or manual) and the run wasn't set explicitly here,
    # default the run to the next not-yet-done one, in order P then C.
    if "run_value" not in payload and run.competition_class and not run.run_type:
        nxt = _next_undone_run(competition, run)
        if nxt:
            run.run_type, run.run_number = nxt
    for field in ("pylon_count", "task_count", "stopline_count"):
        if field in payload:
            setattr(run, field, _as_count(payload.get(field)))
    # The operator now owns this run's identity: it claims its slot in the Auto
    # timing order and the auto binding won't reassign it.
    run.manual_entry = True
    run.save()
    broadcast_live()
    return JsonResponse({"ok": True, "row": _serialize_run(run, competition, competition.competition_type)})


@require_POST
def timing_ignore(request):
    """Mark a time as a wrong measurement (or restore it). Ignoring removes it
    from its run; restoring re-inserts it causally."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    signal = TimingSignal.objects.filter(id=payload.get("signal_id"), competition=competition).first()
    if signal is None:
        return JsonResponse({"ok": False, "error": "Unknown signal."}, status=404)
    signal.ignored = bool(payload.get("ignored"))
    signal.save(update_fields=["ignored"])
    if signal.ignored:
        arrangement.detach(signal)
    else:
        arrangement.ingest(signal, TimingSettings.load())
    broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def timing_pair(request):
    """Drag a time into a run's start/finish slot. Rejects (so the client wiggles)
    a pairing that would put a start after its finish. Dropping onto an upcoming
    Auto competitor with no run yet (only a slot_key) makes their run first, so a
    time can be moved from the current competitor onto the next one."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    signal = TimingSignal.objects.filter(id=payload.get("signal_id"), competition=competition).first()
    slot = payload.get("slot")
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    if run is None and payload.get("slot_key"):
        run = _run_from_slot(competition, payload.get("slot_key"))
    if signal is None or run is None or slot not in ("start", "finish"):
        return JsonResponse({"ok": False, "error": "Bad pairing request."}, status=400)
    ok = arrangement.assign(signal, run, slot)
    if ok:
        # Only clear the ignored flag once the signal is actually in a slot — a
        # rejected pairing must leave it on the rail, not strand it off both.
        if signal.ignored:
            signal.ignored = False
            signal.save(update_fields=["ignored"])
        broadcast_live()
    return JsonResponse({"ok": ok, "rejected": not ok})


@require_POST
def timing_add_run(request):
    """Add an empty run row so the operator can enter a bib/run for an upcoming
    starter. Incoming start times fill these placeholders oldest-first."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    # An operator-created row: owned from the start so the Auto view treats it as
    # a pre-entry rather than an auto-bound slot.
    TimedRun.objects.create(competition=competition, manual_entry=True)
    broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def timing_delete_run(request):
    """Remove an empty placeholder row (one with no start and no finish yet)."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    run = TimedRun.objects.filter(
        id=_json_body(request).get("run_id"), competition=competition,
        start_signal__isnull=True, finish_signal__isnull=True,
    ).first()
    if run is not None:
        run.delete()
        broadcast_live()
    return JsonResponse({"ok": True})


# ----- manually keyed-in times (device failed) -----

@require_POST
def timing_set_time(request):
    """Operator types a start/finish time by hand. Replaces the run's start/finish
    with an *entered* signal (kept distinct from a measured one), or clears it when
    blank. A device signal it displaces is ignored (kept on the rail), an entered
    one is removed. Rejected (start after finish) like a drag pairing."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    slot = payload.get("slot")
    if slot not in ("start", "finish"):
        return JsonResponse({"ok": False, "error": "Bad request."}, status=400)
    raw = payload.get("time")
    clearing = raw is None or str(raw).strip() == ""
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    if run is None:
        # An upcoming competitor in the Auto order has no run yet — make one from
        # their start-order slot so a time can be keyed onto them. Nothing to clear
        # if they still have no run.
        if clearing:
            return JsonResponse({"ok": True})
        run = _run_from_slot(competition, payload.get("slot_key"))
        if run is None:
            return JsonResponse({"ok": False, "error": "Bad request."}, status=400)

    field = "start_signal" if slot == "start" else "finish_signal"
    occupant = getattr(run, field)
    if clearing:
        setattr(run, field, None)
        run.manual_entry = True
        run.save(update_fields=[field, "manual_entry", "updated_at"])
        _discard_displaced(occupant)
        broadcast_live()
        return JsonResponse({"ok": True, "row": _serialize_run(run, competition, competition.competition_type)})

    device_time = _parse_device_time(str(raw).strip())
    if device_time is None:
        return JsonResponse({"ok": False, "error": "time must be hh:mm:ss.mmm."}, status=400)
    other = run.finish_signal if slot == "start" else run.start_signal
    if other is not None:
        if slot == "start" and device_time > other.device_time:
            return JsonResponse({"ok": False, "rejected": True})
        if slot == "finish" and device_time < other.device_time:
            return JsonResponse({"ok": False, "rejected": True})

    settings = TimingSettings.load()
    signal = TimingSignal.objects.create(
        competition=competition,
        running_number=occupant.running_number if occupant else 0,
        port=settings.start_channel if slot == "start" else settings.finish_channel,
        device_time=device_time,
        entered=True,
        source="operator",
    )
    setattr(run, field, signal)
    run.manual_entry = True
    run.save(update_fields=[field, "manual_entry", "updated_at"])
    _discard_displaced(occupant, keep_id=signal.id)
    broadcast_live()
    return JsonResponse({"ok": True, "row": _serialize_run(run, competition, competition.competition_type)})


def _run_from_slot(competition, slot_key):
    """Find (or create) the run for an Auto-timing start-order slot, so a time can
    be keyed onto an upcoming competitor who has no run yet. The slot key is
    ``entry:class:occurrence:run_type:run_number`` (see autotiming.slot_key). A
    created run is operator-owned so it claims its slot and device times skip it."""
    parts = str(slot_key or "").split(":")
    if len(parts) != 5:
        return None
    entry_pk, class_pk, occurrence, run_type, run_number = parts
    if run_type not in (TimedRun.RunType.PRACTICE, TimedRun.RunType.COUNTED):
        return None
    try:
        occurrence, run_number = int(occurrence), int(run_number)
    except (TypeError, ValueError):
        return None
    entry = EventEntry.objects.filter(competition=competition, pk=entry_pk).first()
    cclass = CompetitionClass.objects.filter(competition=competition, pk=class_pk).first()
    if entry is None or cclass is None:
        return None
    identity = dict(
        bib_number=entry.bib_number, competition_class=cclass,
        class_occurrence=occurrence, run_type=run_type, run_number=run_number,
    )
    return (
        TimedRun.objects.filter(competition=competition, **identity).first()
        or TimedRun.objects.create(competition=competition, manual_entry=True, **identity)
    )


def _discard_displaced(signal, keep_id=None):
    """A signal knocked out of a slot by a keyed-in time: delete it if it was
    itself keyed in (operator-created, safe to drop); otherwise keep the measured
    time but ignore it so it lands on the rail rather than vanishing."""
    if signal is None or signal.id == keep_id:
        return
    if signal.entered:
        signal.delete()
    elif not signal.ignored:
        signal.ignored = True
        signal.save(update_fields=["ignored"])


@require_POST
def timing_set_runtime(request):
    """Operator types a run time directly, for when the device gave no usable
    start/finish pair. Sets/clears ``TimedRun.manual_run_time`` (seconds, or an
    hh:mm:ss.mmm duration) and marks the run operator-owned."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    raw = payload.get("run_time")
    clearing = raw is None or str(raw).strip() == ""
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    if run is None:
        if clearing:
            return JsonResponse({"ok": True})
        run = _run_from_slot(competition, payload.get("slot_key"))
        if run is None:
            return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)
    if clearing:
        run.manual_run_time = None
    else:
        seconds = _parse_duration(str(raw).strip())
        if seconds is None:
            return JsonResponse({"ok": False, "error": "run time must be seconds or hh:mm:ss.mmm."}, status=400)
        run.manual_run_time = seconds
    run.manual_entry = True
    run.save(update_fields=["manual_run_time", "manual_entry", "updated_at"])
    broadcast_live()
    return JsonResponse({"ok": True, "row": _serialize_run(run, competition, competition.competition_type)})


def _parse_duration(raw):
    """A typed run time -> Decimal seconds, or None. Accepts plain seconds
    ("30.25") or an hh:mm:ss.mmm / mm:ss.mmm clock duration."""
    from decimal import Decimal, InvalidOperation

    if ":" in raw:
        parts = raw.split(":")
        if len(parts) > 3:
            return None
        try:
            values = [Decimal(p) for p in parts]
        except InvalidOperation:
            return None
        total = Decimal(0)
        for value in values:
            total = total * 60 + value
        return total if total >= 0 else None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value >= 0 else None


# ----- serialization -----

def serialize_arrangement(competition):
    # Keep the Manual view in step with the Auto order: bibs/classes/runs (and
    # marshal penalties) an auto-bound run picked up show here too.
    autotiming.sync_bindings(competition)
    ctype = competition.competition_type
    ignored = (
        competition.timing_signals.filter(ignored=True).order_by("-received_at")
    )
    settings = TimingSettings.load()
    return {
        "penalties_enabled": ctype.penalties_enabled,
        "precision": ctype.timing_precision,
        # The red operator lock: incoming times go straight to the ignore list.
        "input_locked": settings.ignore_incoming,
        "multi_class": competition.allows_multiple_classes_effective(),
        "rows": [_serialize_run(run, competition, ctype) for run in arrangement.rows(competition)],
        "ignored": [
            {
                "id": signal.id,
                "role": signal.role(settings) or "",
                "time": _format_device_time(signal.device_time, ctype.timing_precision),
                "manual": signal.is_manual,
            }
            for signal in ignored
        ],
    }


def _serialize_run(run, competition, ctype):
    precision = ctype.timing_precision
    start, finish = run.start_signal, run.finish_signal
    rt = calc.resolved_run_time(run, precision)
    entry = _resolve_entry(competition, run.bib_number)
    participant = entry.participant if entry else None
    slots = _class_slots(competition, participant, run)
    class_key = _class_key(run) if run.competition_class_id else None
    class_label = next(
        (slot["label"] for slot in slots if slot["value"] == class_key),
        run.competition_class.name if run.competition_class else "",
    )
    # The one canonical penalty (marshal posts + the run's own counts + adjust), so
    # a penalty a marshal added to an operator-selected run adds up here too.
    penalty = autotiming.penalty_seconds(run, competition)
    total = calc.format_precision(rt + penalty, precision) if rt is not None else ""
    return {
        "id": run.id,
        "start": _signal_ref(start, precision),
        "finish": _signal_ref(finish, precision),
        # A row with neither time (and no typed run time) is a placeholder awaiting
        # a starter.
        "placeholder": start is None and finish is None and run.manual_run_time is None,
        "run_time": calc.format_precision(rt, precision),
        # The run time was typed in by hand, not measured — highlighted apart.
        "run_time_manual": run.manual_run_time is not None,
        "run": {
            "id": run.id,
            "bib_number": run.bib_number,
            "name": str(participant) if participant else "",
            # Bib entered but no such starter registered — flag it, but keep it.
            "bib_unknown": bool(run.bib_number and participant is None),
            "class_key": class_key,
            "class_name": class_label,
            "class_options": slots,
            "run_value": _run_value(run),
            "run_options": _run_options(competition, run),
            "pylon_count": run.pylon_count,
            "task_count": run.task_count,
            "stopline_count": run.stopline_count,
            "penalty": penalty,
            "total": total,
            "over_max": _over_max(competition, run),
        },
    }


def _signal_ref(signal, precision):
    if signal is None:
        return None
    return {
        "id": signal.id,
        "time": _format_device_time(signal.device_time, precision),
        "manual": signal.is_manual,
        # Operator typed this time in (device failed) — highlighted apart from a
        # measured time and never re-paired against a device signal automatically.
        "entered": signal.entered,
    }


def _format_device_time(t, precision):
    """hh:mm:ss with the fractional second truncated to the device precision."""
    if t is None:
        return ""
    frac = t.microsecond // (10 ** (6 - precision))
    return f"{t:%H:%M:%S}." + str(frac).zfill(precision)


def _recorded_runs(competition, run, cclass, occurrence):
    """The (run_type, run_number) pairs already recorded for this bib in this
    class occurrence, on rows other than `run`."""
    if not run.bib_number:
        return set()
    return {
        (other.run_type, other.run_number)
        for other in TimedRun.objects.filter(
            competition=competition, bib_number=run.bib_number,
            competition_class=cclass, class_occurrence=occurrence,
        ).exclude(pk=run.pk)
        if other.run_type and other.run_number
    }


def _next_undone_run(competition, run):
    """The next run (P1, P2, …, C1, …) not yet recorded for this run's bib in its
    class occurrence."""
    cclass = run.competition_class
    if cclass is None:
        return None
    used = _recorded_runs(competition, run, cclass, run.class_occurrence)
    for run_type, count in (
        ("practice", cclass.practice_runs or 0),
        ("counted", cclass.counted_runs or 0),
    ):
        for number in range(1, count + 1):
            if (run_type, number) not in used:
                return (run_type, number)
    return None


def _resolve_entry(competition, bib):
    if bib is None:
        return None
    return (
        EventEntry.objects.select_related("participant")
        .filter(competition=competition, bib_number=bib)
        .first()
    )


def _class_key(run):
    return f"{run.competition_class_id}:{run.class_occurrence}"


def _parse_class_key(value, competition):
    """'pk:occurrence' -> (CompetitionClass or None, occurrence int)."""
    pk, _, occ = str(value or "").partition(":")
    cclass = CompetitionClass.objects.filter(id=pk, competition=competition).first()
    try:
        occurrence = int(occ)
    except (TypeError, ValueError):
        occurrence = 0
    return cclass, occurrence


def _participant_slots(competition, participant):
    """The participant's class slots, in order, as (class, occurrence). A class
    they're entered into more than once yields one slot per entry."""
    classes = list(competition.classes_for_participant(participant)) if participant else []
    seen, slots = {}, []
    for cclass in classes:
        occurrence = seen.get(cclass.pk, 0)
        seen[cclass.pk] = occurrence + 1
        slots.append((cclass, occurrence))
    return slots


def _class_slots(competition, participant, run):
    """Serialized class options for the participant: one per class slot, with a
    "(1)/(2)" suffix on classes entered more than once, and disabled once all of
    that slot's runs are recorded."""
    slots = _participant_slots(competition, participant)
    totals = Counter(cclass.pk for cclass, _ in slots)
    options = []
    for cclass, occurrence in slots:
        label = f"{cclass.name} ({occurrence + 1})" if totals[cclass.pk] > 1 else cclass.name
        options.append({
            "value": f"{cclass.pk}:{occurrence}",
            "label": label,
            "disabled": _class_complete(competition, run, cclass, occurrence),
        })
    return options


def _class_complete(competition, run, cclass, occurrence):
    """Whether every run this class grants is already recorded for this bib in
    this occurrence."""
    total = (cclass.practice_runs or 0) + (cclass.counted_runs or 0)
    if total == 0:
        return False
    return len(_recorded_runs(competition, run, cclass, occurrence)) >= total


def _default_class_slot(competition, participant, run):
    """The first class slot the participant hasn't completed (falls back to the
    first slot if all are done), or None if they have no classes."""
    slots = _participant_slots(competition, participant)
    for cclass, occurrence in slots:
        if not _class_complete(competition, run, cclass, occurrence):
            return (cclass, occurrence)
    return slots[0] if slots else None


def _run_options(competition, run):
    """Run choices for this run's class occurrence, with any already recorded for
    this bib marked disabled (e.g. P1 is disabled once this bib has a P1)."""
    cclass = run.competition_class
    if cclass is None:
        return []
    used = _recorded_runs(competition, run, cclass, run.class_occurrence)
    options = []
    for run_type, short, count in (
        ("practice", "P", cclass.practice_runs or 0),
        ("counted", "C", cclass.counted_runs or 0),
    ):
        for number in range(1, count + 1):
            options.append({
                "value": f"{run_type}-{number}",
                "label": f"{short}{number}",
                "disabled": (run_type, number) in used,
            })
    return options


def _run_value(run):
    if run.run_type and run.run_number:
        return f"{run.run_type}-{run.run_number}"
    return ""


def _over_max(competition, run):
    """Whether more runs of this type are assigned for this bib in this class
    occurrence than the class grants."""
    if not run.bib_number or not run.competition_class or not run.run_type:
        return False
    cclass = run.competition_class
    allowance = cclass.practice_runs if run.run_type == "practice" else cclass.counted_runs
    if allowance is None:
        return False
    used = TimedRun.objects.filter(
        competition=competition,
        bib_number=run.bib_number,
        competition_class=cclass,
        class_occurrence=run.class_occurrence,
        run_type=run.run_type,
    ).count()
    return used > allowance


# ----- small parsing helpers -----

def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _as_positive_int(value):
    text = str(value).strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def _as_count(value):
    text = str(value).strip()
    return int(text) if text.isdigit() else 0


def _as_signed(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_run_value(value):
    run_type, _, number = str(value or "").partition("-")
    if run_type in (TimedRun.RunType.PRACTICE, TimedRun.RunType.COUNTED) and number.isdigit():
        return run_type, int(number)
    return "", None
