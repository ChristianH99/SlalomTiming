import json

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.common import safe_next

from . import startpattern
from .assignment import assignment_methods_meta
from .forms import (
    AssignmentForm,
    CompetitionClassFormSet,
    CompetitionForm,
    CompetitionTypeForm,
    CompetitionTypeSettingsForm,
)
from .models import Competition, CompetitionClass, CompetitionType


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
        old_type_id = competition.competition_type_id
        form = CompetitionForm(request.POST, instance=competition)
        if form.is_valid():
            form.save()
            if competition.competition_type_id != old_type_id:
                # A registration belongs to one discipline; changing the
                # competition's type drops the ones that no longer fit (their
                # bibs were silently blocking the new discipline otherwise).
                removed = _clear_foreign_registrations(competition)
                if removed:
                    messages.info(
                        request,
                        f"Removed {removed} registration(s) that didn't belong to "
                        f"“{competition.competition_type}”.",
                    )
            messages.success(request, "General settings saved.")
            return redirect(safe_next(request, reverse("competitions:general")))
        return render(request, self.template_name, {"object": competition, "form": form})


def _clear_foreign_registrations(competition):
    """Drop this competition's entries/class-assignments for participants that
    aren't of its (current) type — used after the type is changed. Returns the
    number of event entries removed. The Participant records themselves stay."""
    from apps.participants.models import ClassAssignment, EventEntry

    entries = EventEntry.objects.filter(competition=competition).exclude(
        participant__competition_type=competition.competition_type
    )
    removed = entries.count()
    entries.delete()
    ClassAssignment.objects.filter(
        competition_class__competition=competition
    ).exclude(participant__competition_type=competition.competition_type).delete()
    return removed


class ClassesView(ActiveCompetitionMixin, View):
    template_name = "competitions/competition_classes.html"

    def _context(self, competition, assignment_form, formset):
        self._annotate_usage(competition, formset)
        return {
            "object": competition,
            "assignment_form": assignment_form,
            "formset": formset,
            "assignment_methods_meta": assignment_methods_meta(),
        }

    @staticmethod
    def _annotate_usage(competition, formset):
        """Tag each class form's instance with how much data references it, so the
        template can warn before a class carrying results or assignments is
        removed. Deleting a class unlinks its recorded runs (SET_NULL) and
        CASCADE-deletes its participant assignments."""
        from apps.participants.models import ClassAssignment
        from apps.timing.models import TimedRun

        run_counts = {
            row["competition_class"]: row["n"]
            for row in TimedRun.objects.filter(
                competition=competition, competition_class__isnull=False
            )
            .filter(Q(start_signal__isnull=False) | Q(finish_signal__isnull=False))
            .values("competition_class")
            .annotate(n=Count("id"))
        }
        assignment_counts = {
            row["competition_class"]: row["n"]
            for row in ClassAssignment.objects.filter(
                competition_class__competition=competition
            )
            .values("competition_class")
            .annotate(n=Count("id"))
        }
        for form in formset.forms:
            pk = form.instance.pk
            form.instance.recorded_run_count = run_counts.get(pk, 0)
            form.instance.assignment_count = assignment_counts.get(pk, 0)

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
        # practice/counted travel with each class so the preview can build dummy
        # starters before anyone is registered, when there are no real starters
        # to read run counts from.
        run_groups_data = [
            [
                {
                    "pk": cc.pk,
                    "name": cc.name,
                    "practice": cc.practice_runs,
                    "counted": cc.counted_runs,
                }
                for cc in run
            ]
            for run in competition.run_groups()
        ]
        return render(request, self.template_name, {
            "object": competition,
            "run_groups_data": run_groups_data,
            "starters_data": self._starters_data(competition),
            "start_pattern_data": startpattern.serialize(competition.start_pattern_blocks()),
            "run_type_labels": startpattern.RUN_TYPE_LABELS,
            "max_dummy": startpattern.MAX_DUMMY_STARTERS,
        })

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        with transaction.atomic():
            self._apply_run_order(request, competition)
            self._apply_start_pattern(request, competition)
        messages.success(request, "Run order saved.")
        return redirect(safe_next(request, reverse("competitions:runorder")))

    @staticmethod
    def _starters_data(competition):
        """Starters grouped by class pk, for the pattern preview. Keyed by class
        rather than by run so the preview can follow the run-order widget as the
        user drags classes about, without a round trip."""
        return {
            str(class_pk): [
                {
                    "key": "-".join(str(part) for part in starter.key),
                    "bib": starter.bib,
                    "name": starter.name,
                    "class_name": starter.class_name,
                    "practice": starter.practice_runs,
                    "counted": starter.counted_runs,
                }
                for starter in starters
            ]
            for class_pk, starters in competition.starters_by_class().items()
        }

    @staticmethod
    def _apply_start_pattern(request, competition):
        """Persist the start pattern from the start_pattern hidden field: JSON
        list of blocks, each {window, chips}. Parsed through startpattern so only
        well-formed blocks are stored."""
        try:
            posted = json.loads(request.POST.get("start_pattern") or "[]")
        except (ValueError, TypeError):
            posted = []
        competition.start_pattern = startpattern.serialize(startpattern.parse(posted))
        competition.save(update_fields=["start_pattern"])

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

    def get_context_data(self, **kwargs):
        # Spell out what deleting the competition takes with it: everything below
        # is CASCADE-deleted along with it and cannot be recovered.
        context = super().get_context_data(**kwargs)
        from apps.participants.models import ClassAssignment, EventEntry
        from apps.timing.models import TimedRun, TimingSignal

        competition = self.object
        context["entry_count"] = EventEntry.objects.filter(competition=competition).count()
        context["class_count"] = competition.classes.count()
        context["assignment_count"] = ClassAssignment.objects.filter(
            competition_class__competition=competition
        ).count()
        context["signal_count"] = TimingSignal.objects.filter(competition=competition).count()
        # "Recorded" runs are those that actually captured a time (not empty
        # placeholder rows) — the results that would be lost.
        context["recorded_run_count"] = (
            TimedRun.objects.filter(competition=competition)
            .filter(Q(start_signal__isnull=False) | Q(finish_signal__isnull=False))
            .count()
        )
        return context


@require_POST
def select_competition(request, pk):
    competition = get_object_or_404(Competition, pk=pk)
    with transaction.atomic():
        Competition.objects.exclude(pk=pk).update(is_active=False)
        competition.is_active = True
        competition.save(update_fields=["is_active"])
    # Timing is scoped to the active competition, so tell any open live-timing view
    # to re-fetch — it must not keep showing the previous competition's times.
    from apps.timing.views import broadcast_live
    broadcast_live()
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
            start_pattern=original.start_pattern,
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
                scoring_method=oc.scoring_method,
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


class CompetitionTypeSettingsView(UpdateView):
    model = CompetitionType
    form_class = CompetitionTypeSettingsForm
    template_name = "competitions/competitiontype_settings.html"
    context_object_name = "competition_type"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["form"]
        context["penalty_fields"] = [form[name] for name in CompetitionType.PENALTY_FIELDS]
        context["participant_info_fields"] = [
            (form[setting], mandatory)
            for setting, (_, mandatory, _) in CompetitionType.PARTICIPANT_INFO.items()
        ]
        return context

    def form_valid(self, form):
        messages.success(self.request, f"Settings for “{form.instance.name}” saved.")
        return super().form_valid(form)

    def get_success_url(self):
        return safe_next(self.request, reverse("competitions:type-list"))


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
