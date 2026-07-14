import json

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView

from .assignment import assignment_methods_meta
from .forms import (
    AssignmentForm,
    CompetitionClassFormSet,
    CompetitionForm,
    CompetitionTypeForm,
)
from .models import Competition, CompetitionClass, CompetitionType


def safe_next(request, fallback):
    """Return the POSTed ?next URL if it's a safe in-app path, else fallback.
    Lets the unsaved-changes modal's "Save changes" land on the page the user
    was navigating to."""
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return fallback


class CompetitionListView(ListView):
    model = Competition
    context_object_name = "competitions"
    template_name = "competitions/competition_list.html"


class CompetitionCreateView(CreateView):
    model = Competition
    form_class = CompetitionForm
    template_name = "competitions/competition_add.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        # A freshly created competition becomes the active one so the setup
        # sub-pages (General/Classes/Run order) target it right away.
        with transaction.atomic():
            Competition.objects.exclude(pk=self.object.pk).update(is_active=False)
            Competition.objects.filter(pk=self.object.pk).update(is_active=True)
        return response

    def get_success_url(self):
        return safe_next(self.request, reverse("competitions:general"))


class ActiveCompetitionMixin:
    """Sub-pages that edit whichever competition is currently active. When none
    is selected they render an empty state prompting the user to pick one."""

    template_name = None
    empty_template_name = "competitions/no_active_competition.html"

    def get_active(self):
        return Competition.get_current()

    def render_empty(self, request):
        return render(request, self.empty_template_name, {})


class GeneralView(ActiveCompetitionMixin, View):
    template_name = "competitions/competition_general.html"

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        form = CompetitionForm(instance=competition)
        return render(request, self.template_name, {"object": competition, "form": form})

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        form = CompetitionForm(request.POST, instance=competition)
        if form.is_valid():
            form.save()
            messages.success(request, "General settings saved.")
            return redirect(safe_next(request, reverse("competitions:general")))
        return render(request, self.template_name, {"object": competition, "form": form})


class ClassesView(ActiveCompetitionMixin, View):
    template_name = "competitions/competition_classes.html"

    def _context(self, competition, assignment_form, formset):
        return {
            "object": competition,
            "assignment_form": assignment_form,
            "formset": formset,
            "assignment_methods_meta": assignment_methods_meta(),
        }

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        assignment_form = AssignmentForm(instance=competition)
        formset = CompetitionClassFormSet(queryset=competition.classes.all())
        return render(request, self.template_name,
                      self._context(competition, assignment_form, formset))

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        assignment_form = AssignmentForm(request.POST, instance=competition)
        formset = CompetitionClassFormSet(request.POST, queryset=competition.classes.all())
        if assignment_form.is_valid() and formset.is_valid():
            with transaction.atomic():
                assignment_form.save()
                # commit=False so we can attach the competition FK and give any
                # brand-new class a list position after the existing ones. Run
                # grouping (run_position) is owned by the Run order page, so it
                # is deliberately left untouched here.
                next_position = (
                    competition.classes.aggregate(m=Max("position"))["m"] or 0
                ) + 1
                instances = formset.save(commit=False)
                for obj in instances:
                    obj.competition = competition
                    if obj.pk is None and not obj.position:
                        obj.position = next_position
                        next_position += 1
                    obj.save()
                for obj in formset.deleted_objects:
                    obj.delete()
            messages.success(request, "Classes saved.")
            return redirect(safe_next(request, reverse("competitions:classes")))
        return render(request, self.template_name,
                      self._context(competition, assignment_form, formset))


class RunOrderView(ActiveCompetitionMixin, View):
    template_name = "competitions/competition_runorder.html"

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        run_groups_data = [
            [{"pk": cc.pk, "name": cc.name} for cc in run]
            for run in competition.run_groups()
        ]
        return render(request, self.template_name, {
            "object": competition, "run_groups_data": run_groups_data,
        })

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        self._apply_run_order(request, competition)
        messages.success(request, "Run order saved.")
        return redirect(safe_next(request, reverse("competitions:runorder")))

    @staticmethod
    def _apply_run_order(request, competition):
        """Persist run grouping and order from the run_order hidden field: JSON
        list of runs, each a list of class pks. Classes sharing a run get the
        same run_position; `position` captures the overall sequence (and so the
        order within a run). Classes not listed sort after with run_position
        cleared."""
        try:
            runs = json.loads(request.POST.get("run_order") or "[]")
        except (ValueError, TypeError):
            runs = []
        classes = {cc.pk: cc for cc in competition.classes.all()}
        position = 0
        seen = set()
        with transaction.atomic():
            for run_index, run in enumerate(runs):
                if not isinstance(run, list):
                    continue
                for pk in run:
                    cc = classes.get(pk)
                    if cc is None or cc.pk in seen:
                        continue
                    cc.run_position = run_index
                    cc.position = position
                    cc.save(update_fields=["run_position", "position"])
                    seen.add(cc.pk)
                    position += 1
            for cc in sorted(classes.values(), key=lambda c: c.position):
                if cc.pk in seen:
                    continue
                cc.run_position = None
                cc.position = position
                cc.save(update_fields=["run_position", "position"])
                position += 1


class CompetitionDeleteView(DeleteView):
    model = Competition
    template_name = "competitions/competition_confirm_delete.html"
    success_url = reverse_lazy("competitions:list")


@require_POST
def select_competition(request, pk):
    competition = get_object_or_404(Competition, pk=pk)
    with transaction.atomic():
        Competition.objects.exclude(pk=pk).update(is_active=False)
        competition.is_active = True
        competition.save(update_fields=["is_active"])
    return redirect("competitions:list")


@require_POST
def duplicate_competition(request, pk):
    original = get_object_or_404(Competition, pk=pk)
    from apps.participants.models import ClassAssignment

    with transaction.atomic():
        copy = Competition.objects.create(
            competition_type=original.competition_type,
            name=f"{original.name} (Copy)",
            date=original.date,
            assignment_method=original.assignment_method,
            allow_multiple_classes=original.allow_multiple_classes,
        )
        # Drop the default classes seeded on create and mirror the original's.
        copy.classes.all().delete()
        name_to_copy = {}
        for oc in original.classes.all():
            name_to_copy[oc.name] = CompetitionClass.objects.create(
                competition=copy,
                name=oc.name,
                position=oc.position,
                is_running=oc.is_running,
                age_from=oc.age_from,
                age_to=oc.age_to,
                practice_runs=oc.practice_runs,
                counted_runs=oc.counted_runs,
                allow_multiple_entries=oc.allow_multiple_entries,
                run_position=oc.run_position,
            )
        # Copy participant class assignments (incl. repeats) onto the matching
        # copied classes. Bibs (EventEntry) are deliberately never copied.
        ClassAssignment.objects.bulk_create(
            ClassAssignment(
                participant_id=assignment.participant_id,
                competition_class=name_to_copy[assignment.competition_class.name],
            )
            for assignment in ClassAssignment.objects.filter(
                competition_class__competition=original
            ).select_related("competition_class")
            if assignment.competition_class.name in name_to_copy
        )
    return redirect("competitions:list")


class CompetitionTypeListView(ListView):
    model = CompetitionType
    context_object_name = "competition_types"
    template_name = "competitions/competitiontype_list.html"

    def get_queryset(self):
        # Usage counts drive whether a type can be deleted; competitions are
        # prefetched for the expandable per-type list.
        return (
            CompetitionType.objects.annotate(
                competition_count=Count("competitions", distinct=True),
                participant_count=Count("participants", distinct=True),
            )
            .prefetch_related("competitions")
        )


class CompetitionTypeCreateView(CreateView):
    model = CompetitionType
    form_class = CompetitionTypeForm
    template_name = "competitions/competitiontype_form.html"
    success_url = reverse_lazy("competitions:type-list")


@require_POST
def delete_competition_type(request, pk):
    competition_type = get_object_or_404(CompetitionType, pk=pk)
    # A type in use (by competitions or participants) can't be removed — the
    # FKs are PROTECT, and the UI disables the button, but guard here too.
    if competition_type.competitions.exists() or competition_type.participants.exists():
        messages.error(request, "That type is still in use and can't be deleted.")
    else:
        competition_type.delete()
    return redirect("competitions:type-list")
