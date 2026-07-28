import json

from django.db.models import F, OuterRef, Q, Subquery
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext, ngettext, gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.common import safe_next
from apps.competitions.models import Competition, CompetitionType

from .bibs import bib_change_effect
from .forms import ParticipantCreateForm, ParticipantUpdateForm
from .models import ClassAssignment, EventEntry, Participant


def save_class_assignments(participant, competition, form):
    """Persist a manual class selection: replace this competition's assignments
    for the participant with the validated list (which may contain repeats)."""
    if competition is None or getattr(form, "class_mode", None) != "manual":
        return
    if form.selected_classes is None:  # type mismatch / not applicable
        return
    ClassAssignment.objects.filter(
        participant=participant, competition_class__competition=competition
    ).delete()
    ClassAssignment.objects.bulk_create(
        ClassAssignment(participant=participant, competition_class=cc)
        for cc in form.selected_classes
    )

# Common consumer email domains, offered as completions once the user types "@".
COMMON_EMAIL_DOMAINS = [
    "gmail.com",
    "icloud.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "aol.com",
    "gmx.net",
    "web.de",
    "t-online.de",
    "proton.me",
]


class ParticipantFormContextMixin:
    """Shared context for the add/edit participant form: the active
    competition's per-class age ranges (for the live class preview), the known
    club names (for the club autocomplete), and the common email domains."""

    def get_success_url(self):
        return safe_next(self.request, reverse("participants:list"))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        ranges = []
        if competition:
            ranges = [
                {"label": cc.name, "age_from": cc.age_from, "age_to": cc.age_to}
                for cc in competition.classes.filter(
                    is_running=True, age_from__isnull=False, age_to__isnull=False
                )
            ]
        # Passed to the template via {{ ...|json_script }}, which handles the
        # JSON serialization — so these stay as plain Python objects here.
        context["class_ranges"] = ranges
        context["competition_year"] = competition.date.year if competition else None
        context["club_options"] = known_clubs()
        context["email_domains"] = COMMON_EMAIL_DOMAINS
        context["check_url"] = reverse("participants:check")
        return context


def known_clubs():
    """Distinct, non-empty club names already on record, alphabetically."""
    return list(
        Participant.objects.exclude(club="")
        .order_by("club")
        .values_list("club", flat=True)
        .distinct()
    )


def _format_address(participant):
    line2 = " ".join(
        part for part in (participant.address_zip_code, participant.address_city) if part
    )
    return ", ".join(part for part in (participant.address_street, line2) if part)


# The detail groups shown in the participant list's expandable detail panel:
# (PARTICIPANT_INFO setting, label, value function). Bib, name, date of birth and
# class are already in the main row; club and licence get their own columns when
# collected — so the panel carries the rest of what the edit view collects.
DETAIL_FIELDS = [
    ("requires_co_driver", _("Co-driver"),
     lambda p: f"{p.co_driver_first_name} {p.co_driver_last_name}".strip()),
    ("requires_vehicle", _("Vehicle"), lambda p: p.vehicle),
    ("requires_address", _("Address"), _format_address),
    ("requires_email", _("E-Mail"), lambda p: p.email),
    ("requires_phone", _("Phone"), lambda p: p.phone_number),
]


def participant_detail_rows(participant, collected_info):
    """Label/value pairs for a participant's detail panel: the details the active
    type collects (``collected_info``) that actually hold a value — everything the
    edit view would show that isn't already a column in the list."""
    rows = []
    for setting, label, value_fn in DETAIL_FIELDS:
        if setting not in collected_info:
            continue
        value = (value_fn(participant) or "").strip()
        if value:
            rows.append({"label": label, "value": value})
    return rows


class ParticipantListView(ListView):
    model = Participant
    context_object_name = "participants"
    template_name = "participants/participant_list.html"

    def get_queryset(self):
        self.competition = Competition.get_current()
        # Default is to show every participant; "active only" is the opt-in.
        self.active_only = self.request.GET.get("active") == "1"
        self.query = self.request.GET.get("q", "").strip()

        # Participants are always scoped to the active competition's type, so with
        # no competition selected there is nothing to show — the template prompts
        # the user to pick one.
        if self.competition is None:
            return Participant.objects.none()

        qs = Participant.objects.filter(competition_type=self.competition.competition_type)
        entry_here = EventEntry.objects.filter(
            competition=self.competition, participant=OuterRef("pk")
        )
        qs = qs.annotate(
            current_bib=Subquery(entry_here.values("bib_number")[:1]),
            # Whether this participant is out of the whole event (the detail
            # panel's disqualification switch), not of a single run.
            current_status=Subquery(entry_here.values("status")[:1]),
        )
        if self.active_only:
            qs = qs.filter(entries__competition=self.competition)
        # Bib order first (unassigned last), then last name as the tiebreaker.
        qs = qs.order_by(F("current_bib").asc(nulls_last=True), "last_name", "first_name")

        if self.query:
            qs = qs.filter(
                Q(first_name__icontains=self.query)
                | Q(last_name__icontains=self.query)
                | Q(club__icontains=self.query)
                | Q(entries__bib_number__icontains=self.query)
            )
        # Manual assignment resolves each participant's classes from their
        # ClassAssignment rows — prefetch so the list doesn't do a query per row.
        if self.competition.assignment().manual:
            qs = qs.prefetch_related("class_assignments__competition_class")
        return qs.distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["competition"] = self.competition
        context["active_only"] = self.active_only
        context["query"] = self.query
        # Which type-optional columns (club, licence) to show — only the details
        # the active competition's type actually collects.
        collected = set()
        if self.competition:
            ctype = self.competition.competition_type
            collected = {
                setting
                for setting in CompetitionType.PARTICIPANT_INFO
                if getattr(ctype, setting)
            }
        context["collected_info"] = collected
        # Fixed columns (bib, name, dob, class, actions) plus the shown optional ones.
        context["column_count"] = 5 + ("requires_club" in collected) + ("requires_license" in collected)
        # Attach each participant's detail-panel rows for the expandable view.
        for participant in context["participants"]:
            participant.detail_rows = participant_detail_rows(participant, collected)
        context["set_bib_url"] = reverse("participants:set-bib")
        context["set_dsq_url"] = reverse("participants:set-dsq")
        return context


class ParticipantCreateView(ParticipantFormContextMixin, CreateView):
    model = Participant
    form_class = ParticipantCreateForm
    template_name = "participants/participant_form.html"

    def dispatch(self, request, *args, **kwargs):
        # A participant is registered under the active competition's type, so one
        # can't be added with no competition selected — send the user to the list,
        # which prompts them to pick a competition first.
        if Competition.get_current() is None:
            return redirect("participants:list")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        competition = Competition.get_current()
        bib_number = form.cleaned_data.get("bib_number")
        if bib_number is not None:
            EventEntry.objects.create(
                participant=self.object,
                competition=competition,
                bib_number=bib_number,
            )
        save_class_assignments(self.object, competition, form)
        return response


class ParticipantUpdateView(ParticipantFormContextMixin, UpdateView):
    model = Participant
    form_class = ParticipantUpdateForm
    template_name = "participants/participant_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["competition"] = Competition.get_current()
        return kwargs

    def form_valid(self, form):
        # A bib is not just a label: runs are keyed by the number, so moving it
        # moves times (see apps/participants/bibs.py). Say what would move and
        # take an explicit yes before saving anything.
        effect = self._bib_change(form)
        if effect is not None and not self.request.POST.get("confirm_bib_change"):
            return self.render_to_response(
                self.get_context_data(form=form, confirm_bib_change=effect)
            )
        response = super().form_valid(form)
        if form.competition is not None and self.object.competition_type_id == form.competition.competition_type_id:
            bib_number = form.cleaned_data.get("bib_number")
            if bib_number is not None:
                # Preserve any run status already recorded; only the bib is
                # managed on this form (status is handled per-run elsewhere).
                entry, created = EventEntry.objects.get_or_create(
                    participant=self.object,
                    competition=form.competition,
                    defaults={"bib_number": bib_number},
                )
                if not created and entry.bib_number != bib_number:
                    entry.bib_number = bib_number
                    entry.save(update_fields=["bib_number"])
            elif form.entry is not None:
                form.entry.delete()
        save_class_assignments(self.object, form.competition, form)
        return response

    def _bib_change(self, form):
        """The times this save would move, or None if it moves none."""
        if form.competition is None or \
                self.object.competition_type_id != form.competition.competition_type_id:
            return None
        old_bib = form.entry.bib_number if form.entry is not None else None
        return bib_change_effect(form.competition, old_bib, form.cleaned_data.get("bib_number"))


class ParticipantDeleteView(DeleteView):
    model = Participant
    template_name = "participants/participant_confirm_delete.html"
    success_url = reverse_lazy("participants:list")

    def get_context_data(self, **kwargs):
        # Deleting a participant CASCADE-removes their registrations and class
        # assignments, but their recorded runs are keyed only by bib number (no
        # FK back to the participant), so those times survive — orphaned. Surface
        # both facts so the operator isn't surprised.
        context = super().get_context_data(**kwargs)
        from apps.timing.models import TimedRun

        participant = self.object
        entries = list(participant.entries.select_related("competition"))
        context["entry_count"] = len(entries)
        context["assignment_count"] = participant.class_assignments.count()
        orphaned = 0
        for entry in entries:
            orphaned += (
                TimedRun.objects.filter(
                    competition=entry.competition, bib_number=entry.bib_number
                )
                .filter(Q(start_signal__isnull=False) | Q(finish_signal__isnull=False))
                .count()
            )
        context["orphaned_run_count"] = orphaned
        return context


@require_POST
def participant_set_bib(request):
    """Assign / change / clear a participant's bib for the active competition,
    straight from the participant list's expandable detail (no full edit needed).
    Enforces the same per-competition uniqueness the edit form does."""
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": gettext("No competition is selected.")}, status=400)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": gettext("Malformed request.")}, status=400)

    participant = Participant.objects.filter(
        pk=payload.get("participant"), competition_type=competition.competition_type
    ).first()
    if participant is None:
        return JsonResponse({"ok": False, "error": gettext("Unknown participant.")}, status=404)

    entry = EventEntry.objects.filter(participant=participant, competition=competition).first()
    old_bib = entry.bib_number if entry is not None else None
    raw = str(payload.get("bib", "")).strip()
    confirmed = bool(payload.get("confirm"))

    if raw == "":
        # Clearing the bib removes the registration (its run status goes with it),
        # and leaves any time recorded under the number behind — so it is the same
        # question as a change, asked the same way.
        warning = _bib_change_warning(competition, old_bib, None, confirmed)
        if warning:
            return warning
        if entry is not None:
            entry.delete()
        return JsonResponse({"ok": True, "bib": None})

    if not raw.isdigit() or int(raw) < 1:
        return JsonResponse({"ok": False, "error": gettext("Bib must be a positive number.")})
    bib = int(raw)

    conflict = EventEntry.objects.filter(competition=competition, bib_number=bib)
    if entry is not None:
        conflict = conflict.exclude(pk=entry.pk)
    if conflict.exists():
        return JsonResponse({"ok": False, "error": gettext("Bib %(bib)s is already taken.") % {"bib": bib}})

    warning = _bib_change_warning(competition, old_bib, bib, confirmed)
    if warning:
        return warning

    if entry is None:
        EventEntry.objects.create(participant=participant, competition=competition, bib_number=bib)
    elif entry.bib_number != bib:
        entry.bib_number = bib
        entry.save(update_fields=["bib_number"])
    return JsonResponse({"ok": True, "bib": bib})


@require_POST
def participant_set_dsq(request):
    """Disqualify a participant from the **whole event**, or take it back, from the
    participant list's expandable detail.

    This is the wider of the two disqualifications the app records and it is
    deliberately kept away from the other: a DSQ on a *run* is entered on the
    timing views and only costs that run, while this one ends the participant's
    event in every class they are entered in. It is scoped to the active
    competition — the flag lives on their EventEntry, so the same person is
    unaffected at the next event.
    """
    competition = Competition.get_current()
    if competition is None:
        return JsonResponse({"ok": False, "error": gettext("No competition is selected.")}, status=400)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": gettext("Malformed request.")}, status=400)

    entry = EventEntry.objects.filter(
        participant__pk=payload.get("participant"),
        participant__competition_type=competition.competition_type,
        competition=competition,
    ).first()
    if entry is None:
        # No entry means no bib: they aren't in this event at all, so there is
        # nothing to disqualify them from.
        return JsonResponse(
            {"ok": False, "error": gettext("Give this participant a bib first.")}, status=404
        )
    dsq = bool(payload.get("dsq"))
    # Only ever moved between these two: the other statuses belong to a run, and
    # taking a disqualification back must not invent a "finished" that never was.
    entry.status = EventEntry.Status.DSQ if dsq else EventEntry.Status.REGISTERED
    entry.save(update_fields=["status", "updated_at"])
    return JsonResponse({"ok": True, "dsq": dsq})


def _bib_change_warning(competition, old_bib, new_bib, confirmed):
    """The refusal to send back when a bib change would move recorded times and
    nobody has said yes to it yet — the same guard the edit form applies, since
    this endpoint changes exactly the same thing. None when it may go ahead."""
    effect = bib_change_effect(competition, old_bib, new_bib)
    if effect is None or confirmed:
        return None
    lines = []
    if effect["leaving"]:
        lines.append(ngettext(
            "%(count)s recorded run stays with bib %(bib)s and stops being this participant's.",
            "%(count)s recorded runs stay with bib %(bib)s and stop being this participant's.",
            effect["leaving"],
        ) % {"count": effect["leaving"], "bib": old_bib})
    if effect["arriving"]:
        lines.append(ngettext(
            "%(count)s run already recorded under bib %(bib)s becomes this participant's.",
            "%(count)s runs already recorded under bib %(bib)s become this participant's.",
            effect["arriving"],
        ) % {"count": effect["arriving"], "bib": new_bib})
    return JsonResponse({
        "ok": False,
        "confirm": " ".join(lines),
        "error": gettext("This moves recorded times."),
    })


def participant_check(request):
    """Return participants that look like duplicates of what's being entered:
    an exact (case-insensitive) licence-number match, or the same first and
    last name. Used by the add/edit form to warn before a duplicate is saved."""
    first_name = request.GET.get("first_name", "").strip()
    last_name = request.GET.get("last_name", "").strip()
    license_number = request.GET.get("license_number", "").strip()
    exclude = request.GET.get("exclude", "")

    base = Participant.objects.all()
    if exclude.isdigit():
        base = base.exclude(pk=int(exclude))

    results = {}
    if license_number:
        for participant in base.filter(license_number__iexact=license_number)[:5]:
            results[participant.pk] = (participant, gettext("licence number"))
    if first_name and last_name:
        for participant in base.filter(first_name__iexact=first_name, last_name__iexact=last_name)[:5]:
            if participant.pk in results:
                results[participant.pk] = (participant, gettext("name and licence number"))
            else:
                results[participant.pk] = (participant, gettext("name"))

    matches = [
        {
            "id": participant.pk,
            "name": f"{participant.first_name} {participant.last_name}",
            "club": participant.club,
            "license_number": participant.license_number,
            "reason": reason,
            "edit_url": reverse("participants:edit", kwargs={"pk": participant.pk}),
        }
        for participant, reason in results.values()
    ]
    return JsonResponse({"matches": matches})
