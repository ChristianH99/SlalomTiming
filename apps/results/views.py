import json

from django.contrib import messages
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.common import safe_next
from apps.competitions.models import CompetitionClass
from apps.competitions.views import ActiveCompetitionMixin
from apps.timing import calc

from . import resultscalc
from .models import RESULT_COLUMNS, ManualTieResolution, ResultColumnSettings

# Name-block lines rendered in bold (the competitor's and co-driver's names).
BOLD_KEYS = {"driver_name", "co_driver"}

# Starters counted as "not classified" in the results summary: those who did not
# start or were disqualified. (DNF is neither classified nor counted here yet — a
# DSQ checkbox in timing is still to come.)
NOT_CLASSIFIED_STATUSES = {"dns", "dsq"}


def _value(participant, key):
    """The participant's display value for a result-column key."""
    if participant is None:
        return ""
    if key == "driver_name":
        return f"{participant.last_name}, {participant.first_name}".strip(", ")
    if key == "co_driver":
        if participant.co_driver_last_name or participant.co_driver_first_name:
            return f"{participant.co_driver_last_name}, {participant.co_driver_first_name}".strip(", ")
        return ""
    if key == "club":
        return participant.club
    if key == "email":
        return participant.email
    if key == "phone":
        return participant.phone_number
    if key == "street":
        return participant.address_street
    if key == "city":
        return participant.address_city
    if key == "vehicle":
        return participant.vehicle
    if key == "license":
        return participant.license_number
    if key == "birthday":
        return participant.date_of_birth.strftime("%d.%m.%Y") if participant.date_of_birth else ""
    if key == "birth_year":
        return str(participant.date_of_birth.year) if participant.date_of_birth else ""
    return ""


def _group_keys(enabled, group):
    """Enabled keys belonging to a layout group, in canonical order."""
    return [k for k in enabled if RESULT_COLUMNS[k][2] == group]


def _score_heading(method):
    """The last column's heading — the scoring value competitors are ranked by."""
    if method == CompetitionClass.Scoring.BEST_RUN:
        return "Best run"
    if method == CompetitionClass.Scoring.REGULARITY:
        return "Difference"
    return "Total"


def _run_cell(run, precision):
    """A run cell: the run time as mm:ss.xxx, and the penalty as ``+N s`` when
    non-zero (penalties stay in whole seconds)."""
    if run is None or run.run_time is None:
        return {"time": "", "penalty": ""}
    return {
        "time": calc.format_clock(run.run_time, precision),
        "penalty": f"+{run.penalty} s" if run.penalty else "",
    }


def build_table(competition, enabled, ranked, unranked, precision,
                counted_count, training_count, include_class, score_heading):
    """Assemble the layout + per-row data both the class and Overall tables render.

    Header and body come from one computed structure so they never drift: a column
    group renders only when at least one of its fields is enabled, and each present
    field is one fixed-height line so rows align."""
    name_keys = _group_keys(enabled, "name")
    address_keys = _group_keys(enabled, "address")
    has_vehicle = "vehicle" in enabled
    licence_keys = _group_keys(enabled, "licence")
    training_on = "training" in enabled and training_count > 0
    training_labels = [f"T{n}" for n in range(1, training_count + 1)] if training_on else []
    counted_labels = [f"C{n}" for n in range(1, counted_count + 1)]

    def header(keys):
        return ResultColumnSettings.label_for(keys[0]) if keys else ""

    layout = {
        "name_keys": name_keys,
        "address_keys": address_keys,
        "has_vehicle": has_vehicle,
        "licence_keys": licence_keys,
        "include_class": include_class,
        "training_labels": training_labels,
        "counted_labels": counted_labels,
        "score_heading": score_heading,
        "name_header": header(name_keys),
        "address_header": header(address_keys),
        "licence_header": header(licence_keys),
    }
    column_count = (
        2  # rank + bib
        + (1 if include_class else 0)
        + (1 if name_keys else 0)
        + (1 if address_keys else 0)
        + (1 if has_vehicle else 0)
        + (1 if licence_keys else 0)
        + len(training_labels) + len(counted_labels)
        + 1  # total
    )
    layout["column_count"] = column_count

    # Summary tallies from the competitors: every starter, those with a final time
    # (classified), and those who did not start or were disqualified.
    everyone = list(ranked) + list(unranked)
    layout["summary"] = {
        "starters": len(everyone),
        "classified": sum(1 for c in everyone if c.score is not None),
        "not_classified": sum(1 for c in everyone if c.status in NOT_CLASSIFIED_STATUSES),
    }

    first_score = None
    for c in ranked:
        if c.rank == 1 and c.score is not None:
            first_score = c.score
            break

    def block(participant, keys):
        return [
            {"text": _value(participant, k), "bold": k in BOLD_KEYS}
            for k in keys
        ]

    def build_row(competitor, participant, ranked_row):
        # The gap to first place is shown for every ranked-table row past first —
        # including a skipped repeat entry (no rank, but still compared to the winner).
        gap = ""
        is_first = competitor.rank == 1 and not competitor.skipped
        if ranked_row and not is_first \
                and competitor.score is not None and first_score is not None:
            gap = f"+{calc.format_clock(competitor.score - first_score, precision)}"
        return {
            "rank": competitor.rank,
            "bib": competitor.bib,
            "entry_pk": competitor.entry_pk,
            "occ": competitor.occurrence,
            "class_name": competitor.class_name,
            "occurrence": competitor.occurrence,
            "inspect": competitor.inspect,
            "skipped": competitor.skipped,
            "status": competitor.status,
            "tie_group": competitor.tie_group,
            "tie_state": competitor.tie_state,
            "tie_start": competitor.tie_start,
            "tie_size": competitor.tie_size,
            "name_lines": block(participant, name_keys),
            "address_lines": block(participant, address_keys),
            "vehicle": _value(participant, "vehicle") if has_vehicle else "",
            "licence_lines": block(participant, licence_keys),
            "training": [
                _run_cell(competitor.training[i] if i < len(competitor.training) else None, precision)
                for i in range(len(training_labels))
            ],
            "counted": [_run_cell(run, precision) for run in competitor.runs],
            "total": calc.format_clock(competitor.score, precision)
            if competitor.score is not None else "",
            "gap": gap,
        }

    return layout, ranked_rows(ranked, competition, build_row, True), \
        ranked_rows(unranked, competition, build_row, False)


def ranked_rows(competitors, competition, build_row, is_ranked):
    participants = {
        entry.participant_id: entry.participant
        for entry in competition.entries.select_related("participant").all()
    }
    return [
        build_row(c, participants.get(c.participant_id), is_ranked)
        for c in competitors
    ]


class ResultsIndexView(ActiveCompetitionMixin, View):
    """Results landing page: the Overall pages (when enabled) and every running
    class, each linking to its own results table."""

    template_name = "results/index.html"

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        overall = (
            resultscalc.overall_groups(competition)
            if ResultColumnSettings.overall_enabled(competition) else []
        )
        return render(request, self.template_name, {
            "object": competition,
            "overall_groups": overall,
            "classes": competition._running_classes_ordered(),
        })


class ResultsClassView(ActiveCompetitionMixin, View):
    """A single running class's ranked results table."""

    template_name = "results/results_class.html"

    def get(self, request, pk):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        cclass = get_object_or_404(
            CompetitionClass, pk=pk, competition=competition, is_running=True
        )
        # Fold Auto timing's positional binding into stored run identity so both
        # timing paths feed the same table.
        resultscalc.sync_identities(competition)
        results = resultscalc.compute_class_results(competition, cclass)

        precision = competition.competition_type.timing_precision
        enabled = ResultColumnSettings.columns_for(competition, cclass)
        layout, ranked, unranked = build_table(
            competition, enabled, results.ranked, results.unranked, precision,
            counted_count=cclass.counted_runs or 0,
            training_count=cclass.practice_runs or 0,
            include_class=False,
            score_heading=_score_heading(cclass.scoring_method),
        )
        return render(request, self.template_name, {
            "object": competition,
            "cclass": cclass,
            "scoring_label": cclass.get_scoring_method_display(),
            "scope": resultscalc.class_scope(cclass),
            "layout": layout,
            "ranked": ranked,
            "unranked": unranked,
        })


class ResultsOverallView(ActiveCompetitionMixin, View):
    """Cross-class Overall results for one (scoring method, counted-run count)."""

    template_name = "results/results_overall.html"

    def get(self, request, method, runs):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        if not ResultColumnSettings.overall_enabled(competition):
            raise Http404("Overall results are turned off.")
        if method not in CompetitionClass.Scoring.values:
            raise Http404("Unknown scoring method.")
        groups = {(g["method"], g["counted_runs"]) for g in resultscalc.overall_groups(competition)}
        if (method, runs) not in groups:
            raise Http404("No such Overall group.")

        resultscalc.sync_identities(competition)
        results = resultscalc.compute_overall_results(competition, method, runs)

        precision = competition.competition_type.timing_precision
        enabled = ResultColumnSettings.general_columns(competition)
        layout, ranked, unranked = build_table(
            competition, enabled, results.ranked, results.unranked, precision,
            counted_count=results.counted_runs,
            training_count=results.training_runs,
            include_class=True,
            score_heading=_score_heading(method),
        )
        return render(request, self.template_name, {
            "object": competition,
            "results": results,
            "scoring_label": results.method_label,
            "scope": resultscalc.overall_scope(method, runs),
            "layout": layout,
            "ranked": ranked,
            "unranked": unranked,
        })


class ResultsTieResolveView(ActiveCompetitionMixin, View):
    """Persist a timekeeper's manual ordering of a tie group (posted as JSON from the
    results table's inline editor). Recomputes the table to locate and validate the
    group, then stores the order + ranks so the next render shows them (green flag)."""

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
        try:
            payload = json.loads(request.body or b"{}")
            scope = payload["scope"]
            posted = [
                (int(m["entry_pk"]), int(m["occurrence"]), int(m["rank"]))
                for m in payload["members"]
            ]
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"ok": False, "error": "Malformed request."}, status=400)

        resultscalc.sync_identities(competition)
        ranked = self._ranked_for_scope(competition, scope)
        if ranked is None:
            return JsonResponse({"ok": False, "error": "Unknown results table."}, status=404)
        members, error = resultscalc.validate_resolution(ranked, posted)
        if error:
            return JsonResponse({"ok": False, "error": error}, status=400)

        keys = frozenset((m[0], m[1]) for m in members)
        with transaction.atomic():
            existing = [
                r for r in competition.tie_resolutions.filter(scope=scope)
                if r.member_keys() == keys
            ]
            for stale in existing[1:]:
                stale.delete()
            row = existing[0] if existing else ManualTieResolution(
                competition=competition, scope=scope
            )
            row.members = members
            row.save()
        return JsonResponse({"ok": True})

    @staticmethod
    def _ranked_for_scope(competition, scope):
        if scope.startswith("class:"):
            try:
                pk = int(scope.split(":", 1)[1])
            except (ValueError, IndexError):
                return None
            cclass = competition.classes.filter(pk=pk, is_running=True).first()
            if cclass is None:
                return None
            return resultscalc.compute_class_results(competition, cclass).ranked
        if scope.startswith("overall:"):
            parts = scope.split(":")
            if len(parts) != 3:
                return None
            try:
                runs = int(parts[2])
            except ValueError:
                return None
            return resultscalc.compute_overall_results(competition, parts[1], runs).ranked
        return None


class ResultsSettingsView(ActiveCompetitionMixin, View):
    """Competition Setup > Results: the Overall toggle, which participant-info
    columns show by default (General), and per-class *additions* on top."""

    template_name = "results/results_settings.html"

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        return render(request, self.template_name, self._context(competition))

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return self.render_empty(request)
        available = ResultColumnSettings.available_keys(competition)
        with transaction.atomic():
            general_cols = [k for k in available if request.POST.get(f"general-{k}")]
            ResultColumnSettings.objects.update_or_create(
                competition=competition, competition_class=None,
                defaults={
                    "columns": general_cols,
                    "show_overall": bool(request.POST.get("show-overall")),
                },
            )
            general_set = set(general_cols)
            for cclass in competition._running_classes_ordered():
                # A class can only *add* columns General doesn't already show.
                cols = [
                    k for k in available
                    if k not in general_set and request.POST.get(f"class-{cclass.pk}-{k}")
                ]
                ResultColumnSettings.objects.update_or_create(
                    competition=competition, competition_class=cclass,
                    defaults={"columns": cols},
                )
        messages.success(request, "Results settings saved.")
        return redirect(safe_next(request, reverse("results:settings")))

    @staticmethod
    def _context(competition):
        available = [
            {"key": key, "label": ResultColumnSettings.label_for(key)}
            for key in ResultColumnSettings.available_keys(competition)
        ]
        general = set(ResultColumnSettings.general_columns(competition))
        general_columns = [
            {**col, "checked": col["key"] in general} for col in available
        ]
        class_rows = []
        for cclass in competition._running_classes_ordered():
            additions = set(ResultColumnSettings.class_additions(competition, cclass))
            # Every available column is rendered; the ones already on in General are
            # hidden client-side (and ignored on save) so a class can only *add*.
            class_rows.append({
                "cclass": cclass,
                "columns": [
                    {**col, "checked": col["key"] in additions} for col in available
                ],
            })
        return {
            "object": competition,
            "available": available,
            "has_columns": bool(available),
            "show_overall": ResultColumnSettings.overall_enabled(competition),
            "general_columns": general_columns,
            "class_rows": class_rows,
        }
