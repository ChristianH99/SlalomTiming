import json
from collections import Counter

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.common import other_signed_in_users, safe_next
from apps.timing.models import MarshalPenalty

from . import startpattern, taskspec
from .assignment import assignment_methods_meta
from .forms import (
    AssignmentForm,
    CompetitionClassFormSet,
    CompetitionForm,
    CompetitionTypeForm,
    CompetitionTypeSettingsForm,
)
from .models import Competition, CompetitionClass, CompetitionType, MarshalPost


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
                        _("Removed %(count)s registration(s) that didn't belong to “%(type)s”.")
                        % {"count": removed, "type": competition.competition_type},
                    )
            messages.success(request, _("General settings saved."))
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
            messages.success(request, _("Classes saved."))
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
        messages.success(request, _("Run order saved."))
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


MAX_MARSHAL_POSTS = 99


class PenaltiesView(ActiveCompetitionMixin, View):
    """Competition Setup > Penalties: whether marshal posts enter their own
    penalties and, if so, how many posts there are and which tasks each watches."""

    template_name = "competitions/competition_penalties.html"

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        rows = self._rows_from_db(competition)
        return render(request, self.template_name,
                      self._context(competition, competition.penalties_by_marshal_posts, rows))

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        enabled = bool(request.POST.get("penalties_by_marshal_posts"))
        confirmed = bool(request.POST.get("confirm_penalty_loss"))

        if not enabled:
            # Turning it off removes the posts entirely — the timekeeper is back
            # in charge and there is nothing for a post to enter. Every penalty
            # they recorded goes with them (CASCADE), so it is confirmed first.
            doomed = self._penalties_lost(competition, keep=set())
            if doomed and not confirmed:
                return self._ask_to_confirm(request, competition, False,
                                            self._rows_from_db(competition), doomed)
            with transaction.atomic():
                competition.penalties_by_marshal_posts = False
                competition.save(update_fields=["penalties_by_marshal_posts"])
                competition.marshal_posts.all().delete()
            messages.success(request, _("Penalties settings saved."))
            return redirect(safe_next(request, reverse("competitions:penalties")))

        rows, has_errors = self._rows_from_post(request)
        if has_errors:
            messages.error(request, _("Some task lists couldn't be read — fix them and save again."))
            return render(request, self.template_name, self._context(competition, True, rows))
        # Fewer posts than before: the ones dropped take their penalties with them.
        doomed = self._penalties_lost(competition, keep={row["number"] for row in rows})
        if doomed and not confirmed:
            return self._ask_to_confirm(request, competition, True, rows, doomed)
        with transaction.atomic():
            competition.penalties_by_marshal_posts = True
            competition.save(update_fields=["penalties_by_marshal_posts"])
            self._reconcile_posts(competition, rows)
        messages.success(request, _("Penalties settings saved."))
        return redirect(safe_next(request, reverse("competitions:penalties")))

    @staticmethod
    def _penalties_lost(competition, keep):
        """How many recorded marshal penalties this save would delete: the ones
        on posts that wouldn't survive it."""
        return MarshalPenalty.objects.filter(
            marshal_post__competition=competition
        ).exclude(marshal_post__number__in=keep).count()

    def _ask_to_confirm(self, request, competition, enabled, rows, doomed):
        """Re-render the page with the loss spelled out and the confirm dialog
        open. The page asks before submitting too — this is the guard for a
        stale page, a form posted without JavaScript, or a penalty recorded
        between loading the page and pressing Save."""
        context = self._context(competition, enabled, rows)
        context["confirm_loss"] = doomed
        return render(request, self.template_name, context)

    @staticmethod
    def _context(competition, enabled, rows):
        numbers = sorted({n for row in rows for n in row["numbers"]})
        return {
            "object": competition,
            "enabled": enabled,
            "rows": rows,
            "post_count": len(rows),
            "tasks_summary": taskspec.summary(numbers),
            "max_posts": MAX_MARSHAL_POSTS,
            # Recorded penalties per post number, so the page can say what a
            # change would destroy before it is submitted.
            "penalty_counts": PenaltiesView._penalty_counts(competition),
            # How many this save would destroy — non-zero only when the view
            # refused one and is asking (see _ask_to_confirm).
            "confirm_loss": 0,
        }

    @staticmethod
    def _penalty_counts(competition):
        counts = (
            MarshalPenalty.objects.filter(marshal_post__competition=competition)
            .values("marshal_post__number")
            .annotate(total=Count("id"))
        )
        return {str(row["marshal_post__number"]): row["total"] for row in counts}

    @staticmethod
    def _rows_from_db(competition):
        rows = [
            {
                "number": post.number,
                "tasks": post.tasks,
                "numbers": post.task_numbers(),
                "stop_line": post.handles_stop_line,
                "error": "",
            }
            for post in competition.marshal_posts.all()
        ]
        if not rows:
            rows.append(
                {"number": 1, "tasks": "", "numbers": [], "stop_line": False, "error": ""}
            )
        return rows

    @staticmethod
    def _rows_from_post(request):
        try:
            count = int(request.POST.get("post_count", "1"))
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, MAX_MARSHAL_POSTS))
        stop_line_post = request.POST.get("stop_line_post") or ""
        rows = []
        has_errors = False
        for i in range(1, count + 1):
            tasks = (request.POST.get(f"post-{i}-tasks") or "").strip()
            error, numbers = "", []
            try:
                numbers = taskspec.parse(tasks)
            except taskspec.TaskSpecError as exc:
                error = str(exc)
                has_errors = True
            rows.append({
                "number": i,
                "tasks": tasks,
                "numbers": numbers,
                "stop_line": stop_line_post == str(i),
                "error": error,
            })
        # A task may belong to only one post: flag any assigned to more than one.
        counts = Counter(n for row in rows for n in row["numbers"])
        dupes = {n for n, c in counts.items() if c > 1}
        if dupes:
            has_errors = True
            for row in rows:
                overlap = sorted(dupes.intersection(row["numbers"]))
                if overlap and not row["error"]:
                    row["error"] = (
                        f"Task {taskspec.format_ranges(overlap)} is on another post too."
                    )
        return rows, has_errors

    @staticmethod
    def _reconcile_posts(competition, rows):
        """Make the stored MarshalPost rows match the submitted ones: drop posts
        past the new count, then update-or-create 1..count."""
        keep = {row["number"] for row in rows}
        competition.marshal_posts.exclude(number__in=keep).delete()
        for row in rows:
            MarshalPost.objects.update_or_create(
                competition=competition,
                number=row["number"],
                defaults={"tasks": row["tasks"], "handles_stop_line": row["stop_line"]},
            )


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
    """Make one competition the active one.

    There is exactly one active competition for the whole installation, so this
    is not a private choice: it changes what every other timing screen, results
    table and marshal post is showing, instantly. On a single-operator laptop
    that is what you want; with other people signed in it is a trap, so they are
    named and the switch is confirmed first — and once it happens, every open
    live view is told, rather than quietly re-rendering as another event.
    """
    competition = get_object_or_404(Competition, pk=pk)
    previous = Competition.get_current()
    if previous is not None and previous.pk == competition.pk:
        return redirect("competitions:list")

    others = other_signed_in_users(request)
    if others and not request.POST.get("confirm_switch"):
        return render(request, "competitions/competition_confirm_switch.html", {
            "object": competition,
            "previous": previous,
            "others": others,
        })

    with transaction.atomic():
        Competition.objects.exclude(pk=pk).update(is_active=False)
        competition.is_active = True
        competition.save(update_fields=["is_active"])
    # Timing is scoped to the active competition, so tell any open live view — it
    # must not keep showing the previous competition's times, and the people
    # watching have to know the ground moved rather than find out from the times.
    from apps.timing.services import notify_competition_changed
    notify_competition_changed(competition.name)
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
            penalties_by_marshal_posts=original.penalties_by_marshal_posts,
            start_pattern=original.start_pattern,
        )
        MarshalPost.objects.bulk_create(
            MarshalPost(
                competition=copy,
                number=post.number,
                tasks=post.tasks,
                handles_stop_line=post.handles_stop_line,
            )
            for post in original.marshal_posts.all()
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
            for setting, (_label, mandatory, _fields) in CompetitionType.PARTICIPANT_INFO.items()
        ]
        return context

    def form_valid(self, form):
        messages.success(self.request, _("Settings for “%(name)s” saved.") % {"name": form.instance.name})
        return super().form_valid(form)

    def get_success_url(self):
        return safe_next(self.request, reverse("competitions:type-list"))


class MarshalPostsView(ActiveCompetitionMixin, View):
    """Top-level operator surface a marshal uses on their phone: pick your post,
    then tap the task buttons to enter penalties for the current starter. The
    config (which tasks, stop-line) comes from the active competition's setup;
    penalty amounts come from its type. Submitting is a no-op stub for now — the
    transmission back into the system is a later feature."""

    template_name = "competitions/marshal_posts.html"

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        ctype = competition.competition_type
        posts = list(competition.marshal_posts.all())
        posts_data = [
            {
                "number": post.number,
                "tasks": post.task_numbers(),
                "stop_line": post.handles_stop_line,
            }
            for post in posts
        ]
        # Max pylon presses before a task tips over the type's per-task ceiling.
        # None means no ceiling is configured, so the button never blocks.
        max_pylons = None
        if ctype.pylon_penalty and ctype.max_penalty_per_task:
            max_pylons = ctype.max_penalty_per_task // ctype.pylon_penalty
        config = {
            "competitionId": competition.pk,
            "posts": posts_data,
            "maxPylons": max_pylons,
            "pylonPenalty": ctype.pylon_penalty,
            "taskPenalty": ctype.task_penalty,
            "stopLinePenalty": ctype.stop_line_penalty,
        }
        return render(request, self.template_name, {
            "object": competition,
            "penalties_by_marshal_posts": competition.penalties_by_marshal_posts,
            "has_posts": bool(posts),
            # Rendered by {{ config|json_script:"marshal-config" }} — the template
            # does the escaping, so nothing here has to be trusted as markup.
            "config": config,
        })


@require_POST
def delete_competition_type(request, pk):
    competition_type = get_object_or_404(CompetitionType, pk=pk)
    # A type in use (by competitions or participants) can't be removed — the
    # FKs are PROTECT, and the UI disables the button, but guard here too.
    if competition_type.competitions.exists() or competition_type.participants.exists():
        messages.error(request, _("That type is still in use and can't be deleted."))
    else:
        competition_type.delete()
    return redirect("competitions:type-list")
