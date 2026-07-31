import datetime
import json
import re
import secrets
from collections import Counter
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView, UpdateView

from apps.accounts import pages
from apps.competitions.models import Competition, CompetitionClass
from apps.common import json_body as _shared_json_body
from apps.participants.models import EventEntry

from . import arrangement, autotiming, calc, cp540, dashboard, runstatus
from .forms import TimingSettingsForm
from .ingest import record_signal
from .models import MarshalPenalty, TimedRun, TimingSettings, TimingSignal
from .services import notify_live


# --- bounds on operator-entered numbers -------------------------------------
# SQLite stores whatever it is handed, so a value past a column's declared range
# isn't rejected — it is written and only surfaces later. For a Decimal that is
# fatal: Django's SQLite converter raises on *every subsequent read of the row*,
# so one mistyped run time would 500 the timing views, the Auto view and the
# dashboard for the rest of the event. These are the doors those numbers come
# through, so the bounds are enforced here.
#
# A typed run time goes into TimedRun.manual_run_time — DecimalField(max_digits=9,
# decimal_places=3). 24 h is past any conceivable run and comfortably inside the
# column.
MAX_RUN_SECONDS = Decimal("86400")
RUN_TIME_PLACES = Decimal("0.001")
# Only digits, colons and dots: no sign, and no "1e3" quietly becoming 1000 s.
_DURATION_CHARS = re.compile(r"^[0-9:.]+$")
# Penalty counts are PositiveSmallIntegerField and the timekeeper's adjusts are
# IntegerField. They are driven by steppers, so anything outside this range is a
# broken client rather than an operator — clamped, so the run stays readable.
MAX_PENALTY_COUNT = 999
# A device's running number counts starts, so the column's own range is already far
# past anything real. Bounded at the door for the same reason as the counts above.
MAX_RUNNING_NUMBER = 2_147_483_647
# A marshal post's per-task breakdown, as its phone sends it. A post watches a
# handful of tasks, so this is orders of magnitude more than any real board — but
# it is a JSONField written straight from a request, and every open Auto timing
# page re-downloads whatever is in it on every nudge.
MAX_DETAIL_TASKS = 200
# The fields of a run-update that say *who this run belongs to*. Editing one of
# these is what makes the run operator-owned (manual_entry); the penalty counts in
# the same payload deliberately do not — see timing_run_update.
IDENTITY_FIELDS = ("bib_number", "class_key", "run_value")


def broadcast_live():
    """Nudge every open live-timing view to re-fetch the arrangement. Works from a
    request handler or a background thread (see services.notify_live)."""
    notify_live()


def _rebind_and_broadcast(competition):
    """For an edit that changes *which* runs exist or who owns them — a new
    placeholder, an ignored time, a re-pairing, an operator taking a row over.
    Each of those moves the start-order binding, so the stored slot identity is
    refreshed here, on the write, before the views are nudged: they bind in memory
    when they render and no longer persist it themselves (see
    autotiming.sync_bindings). Penalty and lock edits don't move it and just
    broadcast."""
    autotiming.sync_bindings(competition)
    broadcast_live()


# The endpoint maps the live pages' scripts drive. They used to be written into an
# inline <script> in each template; with a Content-Security-Policy in place nothing
# on a page may be inline, so the view hands them over as data and the template
# renders them through `json_script` (which escapes them for us).
def _urls(*names):
    from django.urls import reverse

    return {
        _CAMEL.get(name, name.replace("-", "_")): reverse(f"timing:{name}")
        for name in names
    }


# url name -> the key its script already uses.
_CAMEL = {
    "arrangement": "arrangement", "run-update": "run", "run-add": "runAdd",
    "run-delete": "runDelete", "run-status": "runStatus", "ignore": "ignore",
    "pair": "pair", "set-time": "setTime", "set-runtime": "setRuntime",
    "input-lock": "inputLock", "auto-state": "state", "auto-reorder": "reorder",
    "auto-reset-order": "resetOrder", "auto-adjust": "adjust",
    "marshal-unlock": "unlock", "marshal-lock": "lock",
    "marshal-lock-all": "lockAll", "marshal-task-edit": "taskEdit",
    "dashboard-state": "state", "signal": "signal", "cp540-status": "status",
}


class DashboardView(TemplateView):
    """The organiser overview: a live, read-only status view of the whole event
    (overall run progress, per-class state, the competitor on course, headline
    counts). Read-only and derived from the same data the timing views use."""

    template_name = "timing/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        context["competition"] = competition
        context["page_urls"] = _urls("dashboard-state")
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_urls"] = _urls("cp540-status")
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        action = self.request.POST.get("action")
        settings = self.object
        # Only one source may feed the database at a time: selecting any device
        # other than the CP540 stops its reader.
        if settings.device != TimingSettings.Device.CP540:
            cp540.reader.stop()
            self._remember_reader(settings, False)

        if action == "connect" and settings.device == TimingSettings.Device.CP540:
            cp540.reader.start(settings.ip_address, settings.port)
            # The reader itself is process state; this is what survives a restart
            # so the link comes back on its own (cp540.autostart, from asgi.py).
            self._remember_reader(settings, True)
            messages.info(
                self.request,
                _("Connecting to the Tag Heuer CP540 at %(ip)s:%(port)s…")
                % {"ip": settings.ip_address, "port": settings.port},
            )
        elif action == "disconnect":
            cp540.reader.stop()
            self._remember_reader(settings, False)
            messages.info(self.request, _("Disconnected from the timing device."))
        else:
            messages.success(self.request, _("Timing settings saved."))
        return response

    @staticmethod
    def _remember_reader(settings, enabled):
        if settings.reader_enabled != enabled:
            settings.reader_enabled = enabled
            settings.save(update_fields=["reader_enabled"])


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_urls"] = _urls("signal")
        return context


class TimingLiveView(TemplateView):
    """The operator's live timing view for the active competition: incoming start
    and finish times paired into runs, with bib/class/run and penalty entry."""

    template_name = "timing/live.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        context["competition"] = competition
        context["page_urls"] = _urls(
            "arrangement", "run-update", "run-add", "run-delete", "run-status",
            "ignore", "pair", "set-time", "set-runtime", "input-lock",
        )
        if competition is not None:
            context["arrangement"] = serialize_arrangement(competition)
        return context


class AutoTimingView(TemplateView):
    """The order-driven live view: the start order down the left, and the
    previous/current/next competitors with their times and each marshal post's
    running penalty on the right.

    Auto timing *is* the start order, so with no start pattern there is nothing
    for this page to be. It then shows one sentence and a link to build one, and
    no timing controls at all — rather than a page of empty furniture, or (what it
    used to do with times already recorded) a screen of flame-bordered
    "unattributed time" alarms, one per run, with nothing saying why. The rest of
    the app does not need a pattern: Manual timing, the dashboard and the results
    all work from the entries and their classes.
    """

    template_name = "timing/auto.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        context["competition"] = competition
        context["needs_pattern"] = (
            competition is not None and not competition.start_pattern_blocks()
        )
        context["page_urls"] = _urls(
            "auto-state", "auto-reorder", "auto-reset-order", "ignore", "pair",
            "set-time", "set-runtime", "run-status", "auto-adjust",
            "marshal-unlock", "marshal-lock", "marshal-lock-all",
            "marshal-task-edit", "input-lock",
        )
        if competition is not None and not context["needs_pattern"]:
            context["auto"] = autotiming.serialize(competition)
        return context


def auto_arrangement(request):
    """Auto timing state as JSON — fetched on load and on every WebSocket nudge."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"competition": False})
    if not competition.start_pattern_blocks():
        # The page answers this itself and doesn't load its script (see
        # AutoTimingView), so nothing normally asks — but the endpoint says the
        # same thing rather than serialising an order that cannot exist.
        return JsonResponse({"competition": True, "needs_pattern": True})
    data = autotiming.serialize(competition)
    data["competition"] = True
    data["needs_pattern"] = False
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
    # Filtered to slots that exist *and* deduplicated: a key twice over would put
    # one competitor in two places in the order, and `ordered_slots` resolves that
    # by silently dropping the second — an order that doesn't match what was sent
    # and never says so. Bounded too, since this is a list from a client.
    known = {slot["key"] for slot in autotiming.computed_slots(competition)}
    seen, order = set(), []
    for key in posted:
        if key in known and key not in seen:
            seen.add(key)
            order.append(key)
            # Bounded by the *result*, not by the input: capping the input first
            # threw away real keys whenever the client sent an unknown one before
            # them, which is exactly what it does when a slot has just gone.
            if len(order) == len(known):
                break
    competition.auto_timing_order = order
    competition.save(update_fields=["auto_timing_order"])
    # A different order binds runs to different competitors — persist it here, on
    # the write, because the readers no longer do (see autotiming.sync_bindings).
    autotiming.sync_bindings(competition)
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
    autotiming.sync_bindings(competition)
    broadcast_live()
    return JsonResponse({"ok": True})


# ----- marshal posts <-> auto timing link -----

# Who may write here. The access gate grants the marshal endpoints to *either* the
# Timing page or the Marshal Posts page (the two surfaces share them), so it cannot
# tell a timekeeper from a marshal — the views do, because the two are allowed very
# different things: the timekeeper owns lock/unlock/task-edit and may reach any run,
# a marshal may only enter the penalty for the post their device holds.
#
# How far back a marshal's phone may write: a tap the network swallowed waits in the
# page's outbox and is retried (static/js/marshal_posts.js), so a post may
# legitimately deliver a penalty for a competitor a few starters ago — but not for a
# run that finished an hour back. The window is the last few *started* runs.
MARSHAL_RUN_WINDOW = 10


def _holds_timing(user):
    """Whether this user acts as the timekeeper (holds the Timing page)."""
    return user.is_authenticated and "timing" in pages.user_pages(user)


def _timekeeper_required(request):
    """A 403 response unless the caller holds the Timing page, else None."""
    if _holds_timing(request.user):
        return None
    return JsonResponse(
        {"ok": False, "error": "That is the timekeeper's action."}, status=403
    )


def _marshal_may_write(competition, post, run, token):
    """Whether a marshal's device may enter *this* post's penalty for *this* run.

    The claim is what makes a post one device's: marshal_claim hands it out and a
    heartbeat keeps it alive. A write therefore has to present the same token —
    without that check the whole claim machinery was decoration, and any phone on
    the venue network could rewrite any post's penalties on any run of the event.
    """
    if not (post.claim_token and token):
        return False
    if not secrets.compare_digest(post.claim_token, token):
        return False
    return run.id in autotiming.recent_run_ids(competition, MARSHAL_RUN_WINDOW)


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
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
    post = competition.marshal_posts.filter(number=_as_pk(payload.get("post"))).first()
    if run is None or post is None:
        return JsonResponse({"ok": False, "error": "Unknown run or post."}, status=404)
    # A marshal writes with the claim their device holds; the timekeeper may write
    # any post (they enter a penalty a marshal phoned in).
    if not _holds_timing(request.user) and not _marshal_may_write(
        competition, post, run, str(payload.get("token") or "")
    ):
        return JsonResponse(
            {"ok": False, "error": "This post isn’t held by this device."}, status=403
        )
    existing = MarshalPenalty.objects.filter(timed_run=run, marshal_post=post).first()
    if existing is not None and existing.submitted:
        return JsonResponse({"ok": False, "locked": True}, status=409)
    detail = _clean_detail(payload.get("detail"))
    MarshalPenalty.objects.update_or_create(
        timed_run=run,
        marshal_post=post,
        defaults={
            "pylon_count": _as_count(payload.get("pylon_count")),
            "task_count": _as_count(payload.get("task_count")),
            "stopline_count": _as_count(payload.get("stopline_count")),
            "detail": detail,
            "submitted": bool(payload.get("submitted")),
        },
    )
    broadcast_live()
    return JsonResponse({"ok": True})


@require_POST
def marshal_unlock(request):
    """Timekeeper action: reopen a submitted marshal penalty so the marshal can
    edit and re-submit it. Identified by the run and post number."""
    refused = _timekeeper_required(request)
    if refused is not None:
        return refused
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
    post = competition.marshal_posts.filter(number=_as_pk(payload.get("post"))).first()
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
    refused = _timekeeper_required(request)
    if refused is not None:
        return refused
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
    if run is None:
        return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)
    for post in competition.marshal_posts.all():
        MarshalPenalty.objects.update_or_create(
            timed_run=run, marshal_post=post, defaults={"submitted": True},
        )
    broadcast_live()
    return JsonResponse({"ok": True})


def _resolve_run_and_post(competition, payload):
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
    post = competition.marshal_posts.filter(number=_as_pk(payload.get("post"))).first()
    return run, post


@require_POST
def marshal_lock(request):
    """Timekeeper locks (submits) a single post at its current values."""
    refused = _timekeeper_required(request)
    if refused is not None:
        return refused
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
    refused = _timekeeper_required(request)
    if refused is not None:
        return refused
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
    # Summed across the post's tasks, so bounded again: each task's pylons is
    # already capped, their total is not.
    mp.pylon_count = min(
        sum(int((v or {}).get("pylons", 0) or 0) for v in tasks.values()), MAX_PENALTY_COUNT
    )
    mp.task_count = min(
        sum(1 for v in tasks.values() if isinstance(v, dict) and v.get("task")), MAX_PENALTY_COUNT
    )
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
    post = competition.marshal_posts.filter(number=_as_pk(payload.get("post"))).first()
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
    post = competition.marshal_posts.filter(number=_as_pk(payload.get("post"))).first()
    # compare_digest, like _marshal_may_write: this is the secret that says which
    # device owns the post, and the two comparisons should not differ.
    if post is not None and post.claim_token and token and secrets.compare_digest(
        post.claim_token, token
    ):
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
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
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
    # Authorisation: a logged-in operator who holds the Timing page (the browser
    # Simulator carries its session) or a device presenting the shared token. See
    # _signal_authorized. 403 rather than 401 for a signed-in caller without the
    # Timing page, so the log distinguishes "who?" from "not you".
    if not _signal_authorized(request):
        status = 403 if request.user.is_authenticated else 401
        return JsonResponse({"ok": False, "error": "Unauthorized."}, status=status)

    if TimingSettings.load().device != TimingSettings.Device.SIMULATOR:
        return JsonResponse(
            {"ok": False, "error": "The timing device isn’t the simulator; these signals are ignored."},
            status=409,
        )

    payload = _json_body(request)
    if not payload:
        return JsonResponse({"ok": False, "error": "Malformed request."}, status=400)

    try:
        running_number = int(payload["running_number"])
        port = int(payload["port"])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "running_number and port are required integers."}, status=400)

    # Bounded like every other number an outside caller sends: running_number is a
    # PositiveIntegerField, and SQLite takes whatever it is handed rather than
    # refusing it (see the note at the top of this file). A device counts starts,
    # so anything past a signed 32-bit column is a broken client.
    if not (1 <= running_number <= MAX_RUNNING_NUMBER) or not (1 <= port <= 4):
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


class _CsrfCheck(CsrfViewMiddleware):
    """CsrfViewMiddleware that reports rather than responds, so this view can ask
    it a question. ``process_view`` returns None when the token is good."""

    def _reject(self, request, reason):
        return reason


def _device_token_ok(request):
    """A device presenting the configured shared secret (constant-time compared)."""
    token = getattr(settings, "TIMING_DEVICE_TOKEN", "")
    if not token:
        return False
    provided = request.headers.get("X-Device-Token", "")
    return bool(provided) and secrets.compare_digest(provided, token)


def _signal_authorized(request):
    """Who may POST a raw timing signal.

    This is the app's one endpoint outside the login gate, because a physical
    timing device cannot log in — so it decides for itself, and there are exactly
    two callers:

    * **a device**, identified by a matching ``X-Device-Token``. It carries no
      cookies, so CSRF does not apply to it and the view stays ``csrf_exempt``
      for its sake;
    * **the browser Simulator**, which runs in the operator's session. A login
      alone is not enough — writing a time into the live event is the Timing
      page's business, and every other Timing URL is gated on it — and because
      this caller *does* carry cookies, its request is CSRF-checked here. Without
      that, ``csrf_exempt`` plus session auth meant any page an operator visited
      could post times into a running event, with nothing but the session
      cookie's SameSite=Lax default standing in the way. That default is a
      browser's choice, not ours.

    With neither, the door is open only while DEBUG is on (local dev); a
    deployment refuses an anonymous, tokenless post.
    """
    if _device_token_ok(request):
        return True
    if request.user.is_authenticated:
        if "timing" not in pages.user_pages(request.user):
            return False
        return _CsrfCheck(lambda r: None).process_view(request, None, (), {}) is None
    return settings.DEBUG


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
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
    if run is None:
        return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)

    ctx = _RowContext.load(competition)
    if "bib_number" in payload:
        # A new bib re-resolves the class (clearing the bib clears the class), and
        # resets the run so it re-derives for the new participant. The default
        # class is the participant's first slot they haven't yet completed.
        run.bib_number = _as_positive_int(payload.get("bib_number"))
        run.run_type, run.run_number = "", None
        slot = _default_class_slot(ctx, ctx.participant(run.bib_number), run)
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
        nxt = _next_undone_run(ctx, run)
        if nxt:
            run.run_type, run.run_number = nxt
    for field in ("pylon_count", "task_count", "stopline_count"):
        if field in payload:
            setattr(run, field, _as_count(payload.get(field)))
    # Only an *identity* edit takes the run over: from then on it claims its slot
    # in the Auto timing order and the positional binding won't reassign it. A
    # penalty stepper used to do the same, so nudging a pylon count on an
    # auto-bound run silently pinned it to whatever slot it was showing — a side
    # effect nobody expects from a "+" button.
    if any(field in payload for field in IDENTITY_FIELDS):
        run.manual_entry = True
    run.save()
    _rebind_and_broadcast(competition)
    # Re-read after the save: over-max counts this row too, so the snapshot the
    # response is built from has to be the one that includes the change.
    return JsonResponse({"ok": True, "row": _serialize_run(run, _RowContext.load(competition))})


@require_POST
def timing_run_status(request):
    """Close a run with a state code instead of a time — DNF / DNC / DNS / DSQ —
    or clear the one it carries.

    Posted from three places: both timing views (by ``run_id``, or by ``slot_key``
    for a competitor with no run yet) and the results table's not-yet-ranked block
    (always by ``slot_key`` — a DNS is exactly the case where nothing was ever
    recorded). That is why this endpoint belongs to the Results page as well as
    Timing (see apps/accounts/pages.py); both are timekeeper surfaces.
    """
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    status = runstatus.parse(payload.get("status"))
    if status is None:
        return JsonResponse({"ok": False, "error": "Unknown status."}, status=400)
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
    if run is None:
        if not status:
            return JsonResponse({"ok": True})  # nothing recorded, nothing to clear
        run = _run_from_slot(competition, payload.get("slot_key"))
        if run is None:
            return JsonResponse({"ok": False, "error": "Unknown run."}, status=404)
    runstatus.apply(run, status)
    # Taking a run over moves the start-order binding, so this is a write path
    # that re-syncs before nudging the live views.
    _rebind_and_broadcast(competition)
    return JsonResponse({
        "ok": True, "status": run.status,
        "row": _serialize_run(run, _RowContext.load(competition)),
    })


@require_POST
def timing_ignore(request):
    """Mark a time as a wrong measurement (or restore it). Ignoring removes it
    from its run; restoring re-inserts it causally."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    payload = _json_body(request)
    signal = TimingSignal.objects.filter(id=_as_pk(payload.get("signal_id")), competition=competition).first()
    if signal is None:
        return JsonResponse({"ok": False, "error": "Unknown signal."}, status=404)
    signal.ignored = bool(payload.get("ignored"))
    signal.save(update_fields=["ignored"])
    if signal.ignored:
        arrangement.detach(signal)
    else:
        arrangement.ingest(signal, TimingSettings.load())
    _rebind_and_broadcast(competition)
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
    signal = TimingSignal.objects.filter(id=_as_pk(payload.get("signal_id")), competition=competition).first()
    slot = payload.get("slot")
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
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
        _rebind_and_broadcast(competition)
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
    _rebind_and_broadcast(competition)
    return JsonResponse({"ok": True})


@require_POST
def timing_delete_run(request):
    """Remove an empty placeholder row (one with no start and no finish yet)."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
    run = TimedRun.objects.filter(
        id=_as_pk(_json_body(request).get("run_id")), competition=competition,
        start_signal__isnull=True, finish_signal__isnull=True,
    ).first()
    if run is not None:
        run.delete()
        _rebind_and_broadcast(competition)
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
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
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
        _rebind_and_broadcast(competition)
        return JsonResponse({"ok": True, "row": _serialize_run(run, _RowContext.load(competition))})

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
    _rebind_and_broadcast(competition)
    return JsonResponse({"ok": True, "row": _serialize_run(run, _RowContext.load(competition))})


# Making a run from a start-order slot lives in runstatus.py: a status may be set
# on a competitor who never started, which is the same "no run exists yet" problem
# keying a time onto an upcoming starter has, so the two share one door.
_run_from_slot = runstatus.run_for_slot


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
    run = TimedRun.objects.filter(id=_as_pk(payload.get("run_id")), competition=competition).first()
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
    _rebind_and_broadcast(competition)
    return JsonResponse({"ok": True, "row": _serialize_run(run, _RowContext.load(competition))})


def _parse_duration(raw):
    """A typed run time -> Decimal seconds, or None. Accepts plain seconds
    ("30.25") or an hh:mm:ss.mmm / mm:ss.mmm clock duration.

    Refused rather than stored: anything with a character other than a digit,
    colon or dot, and anything longer than MAX_RUN_SECONDS — see the note there
    for what an out-of-range value does to the rest of the event."""
    if not _DURATION_CHARS.match(raw):
        return None
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
    if total < 0 or total > MAX_RUN_SECONDS:
        return None
    # Truncated to the column's precision, never rounded — the same rule calc.py
    # applies to a measured time.
    return total.quantize(RUN_TIME_PLACES, rounding=ROUND_DOWN)


# ----- serialization -----

class _RowContext:
    """What the Manual timing rows need to know about the rest of the competition —
    who is registered, and which runs are already recorded — read **once per
    request** instead of once per row.

    Every row used to resolve its own bib (a query), its participant's classes
    (another), then ask "is this already recorded?" once per class option, once
    more for the run options and once again to count over-max. At 40 starters that
    was 293 queries for a table every open browser re-fetches on every incoming
    time — and it grew with the field (1413 at 200). The whole competition is three
    queries, so it is read whole and answered from memory.

    A context is a snapshot: build it *after* the writes of a request, not before.
    """

    def __init__(self, competition, runs):
        self.competition = competition
        self.ctype = competition.competition_type
        self.precision = self.ctype.timing_precision
        self._entries = {
            entry.bib_number: entry
            for entry in EventEntry.objects.filter(competition=competition)
            .select_related("participant")
            # ManualAssignment.classes_for walks these in Python, so prefetching
            # them makes participant_slots() free.
            .prefetch_related("participant__class_assignments__competition_class")
        }
        # (bib, class pk, occurrence) -> the runs recorded in that slot. The key a
        # row asks about is a *candidate* class (a dropdown option), not
        # necessarily the run's own — hence the class in the key.
        self._by_slot = {}
        for other in runs:
            key = (other.bib_number, other.competition_class_id, other.class_occurrence)
            self._by_slot.setdefault(key, []).append(other)
        self._slots = {}  # participant pk -> [(class, occurrence)]

    @classmethod
    def load(cls, competition):
        """A context for a caller that hasn't already read the runs — the mutate
        endpoints, which serialize their one changed row back."""
        return cls(
            competition,
            TimedRun.objects.filter(competition=competition).only(
                "id", "bib_number", "competition_class", "class_occurrence",
                "run_type", "run_number",
            ),
        )

    def entry(self, bib):
        return self._entries.get(bib) if bib else None

    def participant(self, bib):
        entry = self.entry(bib)
        return entry.participant if entry else None

    def participant_slots(self, participant):
        """The participant's class slots, in order, as (class, occurrence). A class
        they're entered into more than once yields one slot per entry."""
        if participant is None:
            return []
        slots = self._slots.get(participant.pk)
        if slots is None:
            seen, slots = {}, []
            for cclass in self.competition.classes_for_participant(participant):
                occurrence = seen.get(cclass.pk, 0)
                seen[cclass.pk] = occurrence + 1
                slots.append((cclass, occurrence))
            self._slots[participant.pk] = slots
        return slots

    def recorded_runs(self, run, cclass, occurrence):
        """The (run_type, run_number) pairs already recorded for this run's bib in
        this class occurrence, on rows other than ``run``."""
        if not run.bib_number:
            return set()
        return {
            (other.run_type, other.run_number)
            for other in self._by_slot.get((run.bib_number, cclass.pk, occurrence), ())
            if other.pk != run.pk and other.run_type and other.run_number
        }

    def class_complete(self, run, cclass, occurrence):
        """Whether every run this class grants is already recorded for this bib in
        this occurrence."""
        total = (cclass.practice_runs or 0) + (cclass.counted_runs or 0)
        if total == 0:
            return False
        return len(self.recorded_runs(run, cclass, occurrence)) >= total

    def runs_of_type(self, run, cclass, occurrence, run_type):
        """How many runs of ``run_type`` this bib has in this class occurrence —
        ``run`` itself included, which is what over-max counts."""
        return sum(
            1
            for other in self._by_slot.get((run.bib_number, cclass.pk, occurrence), ())
            if other.run_type == run_type
        )


def serialize_arrangement(competition):
    ctype = competition.competition_type
    settings = TimingSettings.load()
    rows = arrangement.rows(competition)
    # Keep the Manual view in step with the Auto order: bibs/classes/runs (and
    # marshal penalties) an auto-bound run picked up show here too. Bound on the
    # rows about to be rendered, in memory — this is a GET, and it must not take
    # the write lock the timing rig needs (see autotiming.sync_bindings).
    autotiming.apply_bindings(competition, rows)
    ctx = _RowContext(competition, rows)
    return {
        "penalties_enabled": ctype.penalties_enabled,
        "precision": ctype.timing_precision,
        # The red operator lock: incoming times go straight to the ignore list.
        "input_locked": settings.ignore_incoming,
        # The device link, so a reader that has lost the CP540 raises its alarm
        # here rather than only on the settings page.
        "device_link": cp540.link_state(settings),
        "multi_class": competition.allows_multiple_classes_effective(),
        # The state codes a run can be closed with (DNF/DNC/DNS/DSQ) — one list
        # for the page rather than a copy per row.
        "status_options": runstatus.options(),
        "rows": [_serialize_run(run, ctx) for run in rows],
        # The same rail as Auto timing, built by the same code so the two pages
        # can't drift apart.
        "ignored": autotiming.ignored_signals(competition, ctype.timing_precision, settings),
        "ignored_split": autotiming.ignored_split(settings),
        # One light barrier: what the next pulse will be read as (see barrier_phase).
        "barrier": autotiming.barrier_phase(settings, rows),
    }


def _serialize_run(run, ctx):
    competition, ctype = ctx.competition, ctx.ctype
    precision = ctx.precision
    start, finish = run.start_signal, run.finish_signal
    rt = calc.resolved_run_time(run, precision)
    participant = ctx.participant(run.bib_number)
    slots = _class_slots(ctx, participant, run)
    class_key = _class_key(run) if run.competition_class_id else None
    class_label = next(
        (slot["label"] for slot in slots if slot["value"] == class_key),
        run.competition_class.name if run.competition_class else "",
    )
    # The one canonical penalty (marshal posts + the run's own counts + adjust), so
    # a penalty a marshal added to an operator-selected run adds up here too.
    penalty = autotiming.penalty_seconds(run, competition)
    total = calc.format_clock(rt + penalty, precision) if rt is not None else ""
    return {
        "id": run.id,
        "start": _signal_ref(start, precision),
        "finish": _signal_ref(finish, precision),
        # A row with neither time (and no typed run time) is a placeholder awaiting
        # a starter. A row closed with a state code is not awaiting anything — it
        # is a recorded outcome, so it neither reads as a placeholder nor offers
        # the × that would throw the outcome away (clear the status first).
        "placeholder": (start is None and finish is None
                        and run.manual_run_time is None and not run.status),
        "run_time": calc.format_clock(rt, precision),
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
            "run_options": _run_options(ctx, run),
            # DNF / DNC / DNS / DSQ, or "" for an ordinary run. A run carrying one
            # is closed: it is never scored, whatever times sit on it.
            "status": run.status,
            "pylon_count": run.pylon_count,
            "task_count": run.task_count,
            "stopline_count": run.stopline_count,
            # Whether this row's steppers still do anything: in marshal mode an
            # auto-bound run's own counts are ignored, so they are disabled rather
            # than quietly taking a number that never reaches the total.
            "penalties_editable": autotiming.own_counts_apply(run, competition),
            "penalty": penalty,
            # The rendered form, so the table can't invent a fourth notation of
            # its own — see calc.format_penalty.
            "penalty_text": calc.format_penalty(penalty),
            "total": total,
            "over_max": _over_max(ctx, run),
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


def _next_undone_run(ctx, run):
    """The next run (P1, P2, …, C1, …) not yet recorded for this run's bib in its
    class occurrence."""
    cclass = run.competition_class
    if cclass is None:
        return None
    used = ctx.recorded_runs(run, cclass, run.class_occurrence)
    for run_type, count in (
        ("practice", cclass.practice_runs or 0),
        ("counted", cclass.counted_runs or 0),
    ):
        for number in range(1, count + 1):
            if (run_type, number) not in used:
                return (run_type, number)
    return None


def _class_key(run):
    return f"{run.competition_class_id}:{run.class_occurrence}"


def _parse_class_key(value, competition):
    """'pk:occurrence' -> (CompetitionClass or None, occurrence int)."""
    pk, _, occ = str(value or "").partition(":")
    cclass = CompetitionClass.objects.filter(
        id=_as_pk(pk), competition=competition
    ).first()
    occurrence = _digits(occ) or 0
    return cclass, occurrence


def _class_slots(ctx, participant, run):
    """Serialized class options for the participant: one per class slot, with a
    "(1)/(2)" suffix on classes entered more than once, and disabled once all of
    that slot's runs are recorded."""
    slots = ctx.participant_slots(participant)
    totals = Counter(cclass.pk for cclass, _ in slots)
    options = []
    for cclass, occurrence in slots:
        label = f"{cclass.name} ({occurrence + 1})" if totals[cclass.pk] > 1 else cclass.name
        options.append({
            "value": f"{cclass.pk}:{occurrence}",
            "label": label,
            "disabled": ctx.class_complete(run, cclass, occurrence),
        })
    return options


def _default_class_slot(ctx, participant, run):
    """The first class slot the participant hasn't completed (falls back to the
    first slot if all are done), or None if they have no classes."""
    slots = ctx.participant_slots(participant)
    for cclass, occurrence in slots:
        if not ctx.class_complete(run, cclass, occurrence):
            return (cclass, occurrence)
    return slots[0] if slots else None


def _run_options(ctx, run):
    """Run choices for this run's class occurrence, with any already recorded for
    this bib marked disabled (e.g. P1 is disabled once this bib has a P1)."""
    cclass = run.competition_class
    if cclass is None:
        return []
    used = ctx.recorded_runs(run, cclass, run.class_occurrence)
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


def _over_max(ctx, run):
    """Whether more runs of this type are assigned for this bib in this class
    occurrence than the class grants."""
    if not run.bib_number or not run.competition_class or not run.run_type:
        return False
    cclass = run.competition_class
    allowance = cclass.practice_runs if run.run_type == "practice" else cclass.counted_runs
    if allowance is None:
        return False
    used = ctx.runs_of_type(run, cclass, run.class_occurrence, run.run_type)
    return used > allowance


# ----- small parsing helpers -----

def _json_body(request):
    """The request's JSON body as a dict. See apps/common.json_body for the three
    ways a body that is not our JSON used to reach a view as a 500."""
    return _shared_json_body(request)


def _digits(value):
    """``value`` as an int when it is written in plain ASCII digits, else None.

    ``str.isdigit()`` alone is not that test: it is True for "²" and "٣" while
    ``int()`` accepts only the second, so an isdigit-then-int pair raises ValueError
    on the first. Every number a client sends comes through here.
    """
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _as_pk(value):
    """A primary key from a client payload, or None.

    Handing a non-numeric string straight to ``filter(id=…)`` makes Django raise
    ValueError while it prepares the query — an unhandled 500 on every mutate
    endpoint from one malformed request. A pk that isn't a number simply matches
    nothing, which is what None does at every call site.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    return _digits(value)


def _as_positive_int(value):
    number = _digits(value)
    return number if number is not None and number > 0 else None


def _as_count(value):
    """A penalty count from the client -> 0..MAX_PENALTY_COUNT (garbage -> 0)."""
    number = _digits(value)
    return min(number, MAX_PENALTY_COUNT) if number is not None else 0


def _clean_detail(value):
    """A marshal board's per-task breakdown, reduced to the shape we render.

    It arrives as free JSON from a phone and lands in a JSONField that every open
    Auto timing page then re-downloads on every nudge, so it is rebuilt here
    rather than stored as sent: known keys only, counts bounded, and a cap on how
    many tasks a post may report."""
    if not isinstance(value, dict):
        return {}
    tasks_in = value.get("tasks")
    tasks = {}
    if isinstance(tasks_in, dict):
        for key, cell in list(tasks_in.items())[:MAX_DETAIL_TASKS]:
            number = _digits(key)
            if number is None or not isinstance(cell, dict):
                continue
            pylons = _as_count(cell.get("pylons"))
            failed = bool(cell.get("task"))
            if pylons or failed:
                tasks[str(number)] = {"pylons": pylons, "task": failed}
    return {"tasks": tasks, "stop_line": bool(value.get("stop_line"))}


def _as_signed(value):
    """A timekeeper's signed +/- adjust, clamped to ±MAX_PENALTY_COUNT."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(-MAX_PENALTY_COUNT, min(number, MAX_PENALTY_COUNT))


def _parse_run_value(value):
    run_type, _, number = str(value or "").partition("-")
    if run_type in (TimedRun.RunType.PRACTICE, TimedRun.RunType.COUNTED) and number.isdigit():
        return run_type, int(number)
    return "", None
