from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.common import safe_next
from apps.competitions.models import Competition, CompetitionClass
from apps.competitions.views import ActiveCompetitionMixin
from apps.timing import calc

from . import resultscalc
from .models import ResultColumnSettings


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
        columns = [
            {"key": key, "label": ResultColumnSettings.label_for(key)}
            for key in ResultColumnSettings.columns_for(competition, cclass)
        ]
        participants = {
            entry.participant_id: entry.participant
            for entry in competition.entries.select_related("participant").all()
        }
        is_regularity = cclass.scoring_method == CompetitionClass.Scoring.REGULARITY
        context = {
            "object": competition,
            "cclass": cclass,
            "columns": columns,
            "scoring_label": cclass.get_scoring_method_display(),
            "is_regularity": is_regularity,
            "score_heading": "Difference" if is_regularity else "Total",
            "counted_runs": cclass.counted_runs or 0,
            "run_labels": [f"C{n}" for n in range(1, (cclass.counted_runs or 0) + 1)],
            "column_count": 3 + len(columns) + (cclass.counted_runs or 0) + 1,
            "ranked": [
                self._row(c, participants.get(c.participant_id), columns, precision)
                for c in results.ranked
            ],
            "unranked": [
                self._row(c, participants.get(c.participant_id), columns, precision)
                for c in results.unranked
            ],
        }
        return render(request, self.template_name, context)

    @staticmethod
    def _row(competitor, participant, columns, precision):
        return {
            "rank": competitor.rank,
            "bib": competitor.bib,
            "name": competitor.name,
            "occurrence": competitor.occurrence,
            "inspect": competitor.inspect,
            "skipped": competitor.skipped,
            "status": competitor.status,
            "cells": [_cell(participant, col["key"]) for col in columns],
            "runs": [
                calc.format_precision(run.total, precision) if run.recorded else ""
                for run in competitor.runs
            ],
            "score": calc.format_precision(competitor.score, precision)
            if competitor.score is not None else "",
        }


def _cell(participant, key):
    """The participant's value for a configured column — the column's fields joined."""
    if participant is None:
        return ""
    values = [
        str(getattr(participant, field))
        for field in ResultColumnSettings.fields_for(key)
        if getattr(participant, field)
    ]
    return " ".join(values)


class ResultsSettingsView(ActiveCompetitionMixin, View):
    """Competition Setup > Results: which participant-info columns the results
    tables show — a General default and optional per-class overrides."""

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
                defaults={"columns": general_cols, "inherit_general": False},
            )
            for cclass in competition._running_classes_ordered():
                inherit = bool(request.POST.get(f"class-{cclass.pk}-inherit"))
                cols = [k for k in available if request.POST.get(f"class-{cclass.pk}-{k}")]
                ResultColumnSettings.objects.update_or_create(
                    competition=competition, competition_class=cclass,
                    defaults={"columns": cols, "inherit_general": inherit},
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
            row = ResultColumnSettings.objects.filter(
                competition=competition, competition_class=cclass
            ).first()
            inherit = row.inherit_general if row is not None else True
            selected = set(row.columns) if row is not None else set()
            class_rows.append({
                "cclass": cclass,
                "inherit": inherit,
                "columns": [
                    {**col, "checked": col["key"] in selected} for col in available
                ],
            })
        return {
            "object": competition,
            "available": available,
            "has_columns": bool(available),
            "general_columns": general_columns,
            "class_rows": class_rows,
        }
