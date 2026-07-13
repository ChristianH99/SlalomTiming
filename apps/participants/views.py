from django.db.models import F, OuterRef, Q, Subquery
from django.http import JsonResponse
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.competitions.models import Competition

from .forms import ParticipantCreateForm, ParticipantUpdateForm
from .models import EventEntry, Participant

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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        competition = Competition.get_current()
        ranges = []
        if competition:
            ranges = [
                {"code": cc.code, "label": cc.get_code_display(), "age_from": cc.age_from, "age_to": cc.age_to}
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


class ParticipantListView(ListView):
    model = Participant
    context_object_name = "participants"
    template_name = "participants/participant_list.html"

    def get_queryset(self):
        self.competition = Competition.get_current()
        # Default is to show every participant; "active only" is the opt-in.
        self.active_only = self.request.GET.get("active") == "1"
        self.query = self.request.GET.get("q", "").strip()

        qs = Participant.objects.all()
        if self.competition:
            qs = qs.filter(competition_type=self.competition.competition_type)
            bib_subquery = EventEntry.objects.filter(
                competition=self.competition, participant=OuterRef("pk")
            ).values("bib_number")[:1]
            qs = qs.annotate(current_bib=Subquery(bib_subquery))
            if self.active_only:
                qs = qs.filter(entries__competition=self.competition)
            # Bib order first (unassigned last), then last name as the tiebreaker.
            qs = qs.order_by(F("current_bib").asc(nulls_last=True), "last_name", "first_name")
        else:
            qs = qs.order_by("last_name", "first_name")

        if self.query:
            qs = qs.filter(
                Q(first_name__icontains=self.query)
                | Q(last_name__icontains=self.query)
                | Q(club__icontains=self.query)
                | Q(entries__bib_number__icontains=self.query)
            )
        return qs.distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["competition"] = self.competition
        context["active_only"] = self.active_only
        context["query"] = self.query
        return context


class ParticipantCreateView(ParticipantFormContextMixin, CreateView):
    model = Participant
    form_class = ParticipantCreateForm
    template_name = "participants/participant_form.html"
    success_url = reverse_lazy("participants:list")

    def get_initial(self):
        initial = super().get_initial()
        competition = Competition.get_current()
        if competition:
            initial["competition_type"] = competition.competition_type_id
        return initial

    def form_valid(self, form):
        response = super().form_valid(form)
        bib_number = form.cleaned_data.get("bib_number")
        if bib_number is not None:
            EventEntry.objects.create(
                participant=self.object,
                competition=Competition.get_current(),
                bib_number=bib_number,
            )
        return response


class ParticipantUpdateView(ParticipantFormContextMixin, UpdateView):
    model = Participant
    form_class = ParticipantUpdateForm
    template_name = "participants/participant_form.html"
    success_url = reverse_lazy("participants:list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["competition"] = Competition.get_current()
        return kwargs

    def form_valid(self, form):
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
        return response


class ParticipantDeleteView(DeleteView):
    model = Participant
    template_name = "participants/participant_confirm_delete.html"
    success_url = reverse_lazy("participants:list")


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
            results[participant.pk] = (participant, "licence number")
    if first_name and last_name:
        for participant in base.filter(first_name__iexact=first_name, last_name__iexact=last_name)[:5]:
            if participant.pk in results:
                results[participant.pk] = (participant, "name and licence number")
            else:
                results[participant.pk] = (participant, "name")

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
