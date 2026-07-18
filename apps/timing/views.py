import datetime
import json

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView, UpdateView

from apps.competitions.models import Competition, CompetitionClass
from apps.participants.models import EventEntry

from . import arrangement, calc
from .forms import TimingSettingsForm
from .models import TimedRun, TimingEvent, TimingSettings, TimingSignal
from .services import LIVE_GROUP


def broadcast_live():
    """Nudge every open live-timing view to re-fetch the arrangement."""
    layer = get_channel_layer()
    if layer is not None:
        async_to_sync(layer.group_send)(LIVE_GROUP, {"type": "timing.refresh"})


class DashboardView(ListView):
    model = TimingEvent
    context_object_name = "events"
    template_name = "timing/dashboard.html"
    paginate_by = 50


class TimingSettingsView(UpdateView):
    """The single timing-rig settings row. 'Save' persists the settings; the
    device-specific 'Start' control (opening the simulator, or connecting to a
    real device) lives in the template — connecting to hardware isn't built yet."""

    form_class = TimingSettingsForm
    template_name = "timing/settings.html"
    success_url = reverse_lazy("timing:settings")

    def get_object(self, queryset=None):
        return TimingSettings.load()

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("action") == "connect":
            messages.info(
                self.request,
                "Settings saved. Connecting to the Tag Heuer TP540 isn’t implemented yet.",
            )
        else:
            messages.success(self.request, "Timing settings saved.")
        return response


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


# ----- signal ingestion (called by the simulator / device) -----

@csrf_exempt  # a physical timing device posts here and can't carry a CSRF token
@require_POST
def timing_signal(request):
    """Receive one raw timing signal (running number, port, time), record it
    stamped with the active competition, and place it into the run arrangement."""
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

    competition = Competition.get_current()
    signal = TimingSignal.objects.create(
        competition=competition,
        running_number=running_number,
        port=port,
        is_manual=bool(payload.get("is_manual")),
        device_time=device_time,
        source=str(payload.get("source", "simulator"))[:20],
    )
    if competition is not None:
        arrangement.ingest(signal, TimingSettings.load())
        broadcast_live()

    return JsonResponse({"ok": True, "id": signal.id})


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
        # resets the run so it re-derives for the new participant.
        run.bib_number = _as_positive_int(payload.get("bib_number"))
        run.run_type, run.run_number = "", None
        entry = _resolve_entry(competition, run.bib_number)
        classes = competition.classes_for_participant(entry.participant) if entry else []
        run.competition_class = classes[0] if classes else None
    if "class_id" in payload:
        run.competition_class = CompetitionClass.objects.filter(
            id=payload.get("class_id"), competition=competition
        ).first()
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
    a pairing that would put a start after its finish."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    signal = TimingSignal.objects.filter(id=payload.get("signal_id"), competition=competition).first()
    run = TimedRun.objects.filter(id=payload.get("run_id"), competition=competition).first()
    slot = payload.get("slot")
    if signal is None or run is None or slot not in ("start", "finish"):
        return JsonResponse({"ok": False, "error": "Bad pairing request."}, status=400)
    if signal.ignored:
        signal.ignored = False
        signal.save(update_fields=["ignored"])
    ok = arrangement.assign(signal, run, slot)
    if ok:
        broadcast_live()
    return JsonResponse({"ok": ok, "rejected": not ok})


@require_POST
def timing_add_run(request):
    """Add an empty run row so the operator can enter a bib/run for an upcoming
    starter. Incoming start times fill these placeholders oldest-first."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    TimedRun.objects.create(competition=competition)
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


# ----- serialization -----

def serialize_arrangement(competition):
    ctype = competition.competition_type
    ignored = (
        competition.timing_signals.filter(ignored=True).order_by("-received_at")
    )
    settings = TimingSettings.load()
    return {
        "penalties_enabled": ctype.penalties_enabled,
        "precision": ctype.timing_precision,
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
    rt = calc.run_time(
        start.device_time if start else None,
        finish.device_time if finish else None,
        precision,
    )
    entry = _resolve_entry(competition, run.bib_number)
    participant = entry.participant if entry else None
    cclass = run.competition_class
    penalty = calc.total_penalty(run, ctype)
    total = calc.format_precision(rt + penalty, precision) if rt is not None else ""
    return {
        "id": run.id,
        "start": _signal_ref(start, precision),
        "finish": _signal_ref(finish, precision),
        # A row with neither time is a placeholder awaiting a starter.
        "placeholder": start is None and finish is None,
        "run_time": calc.format_precision(rt, precision),
        "run": {
            "id": run.id,
            "bib_number": run.bib_number,
            "name": str(participant) if participant else "",
            # Bib entered but no such starter registered — flag it, but keep it.
            "bib_unknown": bool(run.bib_number and participant is None),
            "class_id": cclass.pk if cclass else None,
            "class_name": cclass.name if cclass else "",
            "class_options": _class_options(competition, participant),
            "run_value": _run_value(run),
            "run_options": _run_options(competition, run, cclass),
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
    }


def _format_device_time(t, precision):
    """hh:mm:ss with the fractional second truncated to the device precision."""
    if t is None:
        return ""
    frac = t.microsecond // (10 ** (6 - precision))
    return f"{t:%H:%M:%S}." + str(frac).zfill(precision)


def _next_undone_run(competition, run):
    """The next run (P1, P2, …, C1, …) not yet recorded for this run's bib+class."""
    cclass = run.competition_class
    if cclass is None:
        return None
    used = set()
    if run.bib_number:
        used = {
            (other.run_type, other.run_number)
            for other in TimedRun.objects.filter(
                competition=competition, bib_number=run.bib_number, competition_class=cclass
            ).exclude(pk=run.pk)
            if other.run_type and other.run_number
        }
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


def _class_options(competition, participant):
    if participant is None:
        return []
    seen, options = set(), []
    for cclass in competition.classes_for_participant(participant):
        if cclass.pk not in seen:
            seen.add(cclass.pk)
            options.append({"value": cclass.pk, "label": cclass.name})
    return options


def _run_options(competition, run, cclass):
    """Run choices for a class, with any already recorded for this bib+class
    marked disabled (e.g. P1 is disabled once this bib has a P1 on another run)."""
    if cclass is None:
        return []
    used = set()
    if run.bib_number:
        used = {
            (other.run_type, other.run_number)
            for other in TimedRun.objects.filter(
                competition=competition, bib_number=run.bib_number, competition_class=cclass
            ).exclude(pk=run.pk)
            if other.run_type and other.run_number
        }
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
    """Whether more runs of this type have been assigned for this bib+class than
    the class grants."""
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


def _parse_run_value(value):
    run_type, _, number = str(value or "").partition("-")
    if run_type in (TimedRun.RunType.PRACTICE, TimedRun.RunType.COUNTED) and number.isdigit():
        return run_type, int(number)
    return "", None
