import json

from django.db.models import Q
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.competitions.models import Competition

from .forms import ParticipantCreateForm, ParticipantUpdateForm
from .models import EventEntry, Participant


class ClassRangeContextMixin:
    """Adds the active competition's per-class age ranges as JSON, for the
    participant form's live class-preview script."""

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
        context["class_ranges_json"] = json.dumps(ranges)
        context["competition_year"] = competition.date.year if competition else None
        return context


class ParticipantListView(ListView):
    model = Participant
    context_object_name = "participants"
    template_name = "participants/participant_list.html"

    def get_queryset(self):
        self.competition = Competition.get_current()
        self.show_all = self.request.GET.get("all") == "1"
        self.query = self.request.GET.get("q", "").strip()

        qs = Participant.objects.all()
        if self.competition:
            qs = qs.filter(competition_type=self.competition.competition_type)
            if not self.show_all:
                qs = qs.filter(entries__competition=self.competition)
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
        participants = list(context["participants"])
        entries = {}
        if self.competition:
            entries = {
                e.participant_id: e
                for e in EventEntry.objects.filter(
                    competition=self.competition, participant__in=participants
                )
            }
        for participant in participants:
            participant.current_entry = entries.get(participant.id)
        context["participants"] = participants
        context["competition"] = self.competition
        context["show_all"] = self.show_all
        context["query"] = self.query
        return context


class ParticipantCreateView(ClassRangeContextMixin, CreateView):
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


class ParticipantUpdateView(ClassRangeContextMixin, UpdateView):
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
                EventEntry.objects.update_or_create(
                    participant=self.object,
                    competition=form.competition,
                    defaults={
                        "bib_number": bib_number,
                        "status": form.cleaned_data.get("status") or EventEntry.Status.REGISTERED,
                    },
                )
            elif form.entry is not None:
                form.entry.delete()
        return response


class ParticipantDeleteView(DeleteView):
    model = Participant
    template_name = "participants/participant_confirm_delete.html"
    success_url = reverse_lazy("participants:list")
