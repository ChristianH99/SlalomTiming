import json

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.common import safe_next
from apps.competitions.models import CompetitionClass
from apps.competitions.views import ActiveCompetitionMixin
from apps.timing import calc

from . import pdf, pdfmarkup, resultscalc
from .models import (
    RESULT_COLUMNS, ManualTieResolution, ResultColumnSettings, ResultsPdfLayout,
)

# Name-block lines rendered in bold (the competitor's and co-driver's names).
BOLD_KEYS = {"driver_name", "co_driver"}

# A PDF logo is a club emblem printed at ~18 mm high. The two logo fields are
# assigned straight from request.FILES (there is no ModelForm here), so nothing else
# checks them: without this an upload of any size is written into MEDIA_ROOT, and
# anything at all is written as an "image" for ReportLab to choke on at export time.
MAX_LOGO_BYTES = 4 * 1024 * 1024

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
        return _("Best run")
    if method == CompetitionClass.Scoring.REGULARITY:
        return _("Difference")
    return _("Total")


def _run_cell(run, precision):
    """A run cell: the run time as mm:ss.xxx, and the penalty as ``+N s`` when
    non-zero (penalties stay in whole seconds)."""
    if run is None or run.run_time is None:
        return {"time": "", "penalty": ""}
    return {
        "time": calc.format_clock(run.run_time, precision),
        "penalty": f"+{run.penalty} s" if run.penalty else "",
    }


def build_layout(enabled, counted_count, training_count, include_class,
                 score_heading, summary):
    """The header/column structure both the class and Overall tables render: a
    column group appears only when a field in it is enabled, each present field is
    one fixed-height line so rows align. ``summary`` is the starters/classified/
    not-classified tally shown below the table."""
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
        "summary": summary,
    }
    layout["column_count"] = (
        2  # rank + bib
        + (1 if include_class else 0)
        + (1 if name_keys else 0)
        + (1 if address_keys else 0)
        + (1 if has_vehicle else 0)
        + (1 if licence_keys else 0)
        + len(training_labels) + len(counted_labels)
        + 1  # total
    )
    return layout


def build_table(competition, enabled, ranked, unranked, precision,
                counted_count, training_count, include_class, score_heading):
    """Assemble the layout + per-row data both the class and Overall tables render.

    Header and body come from one computed structure so they never drift."""
    # Summary tallies from the competitors: every starter, those with a final time
    # (classified), and those who did not start or were disqualified.
    everyone = list(ranked) + list(unranked)
    summary = {
        "starters": len(everyone),
        "classified": sum(1 for c in everyone if c.score is not None),
        "not_classified": sum(1 for c in everyone if c.status in NOT_CLASSIFIED_STATUSES),
    }
    layout = build_layout(enabled, counted_count, training_count, include_class,
                          score_heading, summary)
    name_keys = layout["name_keys"]
    address_keys = layout["address_keys"]
    has_vehicle = layout["has_vehicle"]
    licence_keys = layout["licence_keys"]
    training_labels = layout["training_labels"]

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


def class_section(competition, cclass):
    """The full render payload for one class results table (layout + rows + labels).
    Shared by the on-screen view and the PDF export so both show the same table."""
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
    class_title = _("Class %(name)s") % {"name": cclass.name}
    return {
        "title": class_title,
        "scoring_label": cclass.get_scoring_method_display(),
        "class_label": class_title,
        "scope": resultscalc.class_scope(cclass),
        "layout": layout, "ranked": ranked, "unranked": unranked,
        "cclass": cclass,
    }


def overall_section(competition, method, runs):
    """The full render payload for one Overall results table. Shared by the
    on-screen view and the PDF export."""
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
    return {
        # title/scoring_label feed the PDF headline only (the web view reads
        # results.method_label); class_label feeds the #class wildcard.
        "title": _("Overall"),
        "scoring_label": _("%(label)s · %(runs)s Runs") % {
            "label": results.method_label, "runs": results.counted_runs},
        "class_label": _("Overall · %(label)s · %(runs)s Runs") % {
            "label": results.method_label, "runs": results.counted_runs},
        "scope": resultscalc.overall_scope(method, runs),
        "layout": layout, "ranked": ranked, "unranked": unranked,
        "results": results,
    }


# ----- PDF export -----

_SAMPLE_NAMES = [
    ("Fischer", "Anna"), ("Weber", "Lukas"), ("Meyer", "Sophie"),
    ("Wagner", "Jonas"), ("Becker", "Laura"), ("Schulz", "Felix"),
    ("Hoffmann", "Marie"), ("Koch", "Paul"), ("Richter", "Emma"),
    ("Klein", "Tim"), ("Wolf", "Lena"), ("Neumann", "Max"),
]
_SAMPLE_CLASSES = ["Class A", "Class B", "Class C"]
_SAMPLE_VEHICLES = ["VW Golf GTI", "Opel Corsa", "BMW 320i", "Audi A3", "Ford Fiesta ST"]
_SAMPLE_CLUBS = ["MSC Bergland", "AMC Talfahrt", "RC Kurvenkönig", "MC Nordwind"]


def _dummy_run(i, j):
    secs = 42 + (i * 7 + j * 3) % 18
    hund = (i * 31 + j * 17) % 100
    return {"time": f"00:{secs:02d}.{hund:02d}", "penalty": "+5 s" if (i + j) % 5 == 0 else ""}


def sample_section(competition, layout_ctx):
    """A section of made-up rows (about 1.5 pages) built on the competition's
    General result columns, so the settings page can preview the PDF layout."""
    enabled = ResultColumnSettings.general_columns(competition)
    counted, training = 2, 1
    # Enough rows to spill past one page (about 1.5) so the page-break handling —
    # a repeated header and rows kept whole — is visible in the preview.
    n = 30
    summary = {"starters": n + 3, "classified": n, "not_classified": 3}
    layout = build_layout(enabled, counted, training, include_class=True,
                          score_heading="Total", summary=summary)

    def block(keys, data):
        return [{"text": data.get(k, ""), "bold": k in BOLD_KEYS} for k in keys]

    rows = []
    for i in range(n):
        last, first = _SAMPLE_NAMES[i % len(_SAMPLE_NAMES)]
        data = {
            "driver_name": f"{last}, {first}",
            "co_driver": f"{_SAMPLE_NAMES[(i + 3) % len(_SAMPLE_NAMES)][0]}, "
                         f"{_SAMPLE_NAMES[(i + 3) % len(_SAMPLE_NAMES)][1]}",
            "club": _SAMPLE_CLUBS[i % len(_SAMPLE_CLUBS)],
            "email": f"{first.lower()}.{last.lower()}@example.com",
            "phone": f"+49 170 {1000000 + i * 137:07d}"[:16],
            "street": f"Bergstraße {i + 1}",
            "city": f"{10000 + i * 111} Musterstadt",
            "vehicle": _SAMPLE_VEHICLES[i % len(_SAMPLE_VEHICLES)],
            "license": f"D-{20000 + i}",
            "birthday": f"{(i % 28) + 1:02d}.0{(i % 9) + 1}.199{i % 10}",
            "birth_year": f"199{i % 10}",
        }
        # Every few rows, use deliberately long values to prove long names, clubs and
        # vehicles are truncated cleanly and never break the fixed row height.
        if i % 6 == 2:
            data["driver_name"] = "Von Und Zu Hohenzollern-Sigmaringen, Maximilian Alexander"
            data["club"] = "Motorsportclub Bergland-Talfahrt und Umgebung e.V. 1927"
            data["vehicle"] = "Volkswagen Golf VII GTI Clubsport S Performance Edition"
        rows.append({
            "rank": i + 1, "bib": i + 1,
            "class_name": _SAMPLE_CLASSES[i % len(_SAMPLE_CLASSES)], "occurrence": 0,
            "name_lines": block(layout["name_keys"], data),
            "address_lines": block(layout["address_keys"], data),
            "vehicle": data["vehicle"] if layout["has_vehicle"] else "",
            "licence_lines": block(layout["licence_keys"], data),
            "training": [_dummy_run(i, j) for j in range(len(layout["training_labels"]))],
            "counted": [_dummy_run(i, j + 5) for j in range(counted)],
            "total": f"01:{25 + i % 30:02d}.{(i * 13) % 100:02d}",
            "gap": "" if i == 0 else f"+{i}.{(i * 7) % 100:02d}",
            "status": "",
        })
    return {
        "title": "Sample", "scoring_label": "Total",
        "class_label": "Sample class", "layout": layout,
        "ranked": rows, "unranked": [],
    }


def _pdf_response(competition, layout, sections, filename):
    data = pdf.render_results_pdf(competition, layout, sections)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


class ResultsExportClassView(ActiveCompetitionMixin, View):
    """Export one class results table to PDF (saved layout)."""

    def get(self, request, pk):
        competition = self.get_active()
        if competition is None:
            raise Http404("No active competition.")
        cclass = get_object_or_404(
            CompetitionClass, pk=pk, competition=competition, is_running=True
        )
        resultscalc.sync_identities(competition)
        layout = ResultsPdfLayout.for_competition(competition)
        section = class_section(competition, cclass)
        return _pdf_response(competition, layout, [section],
                             f"results-{cclass.name}.pdf")


class ResultsExportOverallView(ActiveCompetitionMixin, View):
    """Export one Overall results table to PDF (saved layout)."""

    def get(self, request, method, runs):
        competition = self.get_active()
        if competition is None:
            raise Http404("No active competition.")
        if not ResultColumnSettings.overall_enabled(competition):
            raise Http404("Overall results are turned off.")
        groups = {(g["method"], g["counted_runs"]) for g in resultscalc.overall_groups(competition)}
        if (method, runs) not in groups:
            raise Http404("No such Overall group.")
        resultscalc.sync_identities(competition)
        layout = ResultsPdfLayout.for_competition(competition)
        section = overall_section(competition, method, runs)
        return _pdf_response(competition, layout, [section],
                             f"results-overall-{method}-{runs}.pdf")


class ResultsExportAllView(ActiveCompetitionMixin, View):
    """Export every results table (Overall pages then each class) to one PDF."""

    def get(self, request):
        competition = self.get_active()
        if competition is None:
            raise Http404("No active competition.")
        resultscalc.sync_identities(competition)
        sections = []
        if ResultColumnSettings.overall_enabled(competition):
            for g in resultscalc.overall_groups(competition):
                sections.append(overall_section(competition, g["method"], g["counted_runs"]))
        for cclass in competition._running_classes_ordered():
            sections.append(class_section(competition, cclass))
        if not sections:
            raise Http404("No running classes to export.")
        layout = ResultsPdfLayout.for_competition(competition)
        return _pdf_response(competition, layout, sections, "results-all.pdf")


class ResultsExportSampleView(ActiveCompetitionMixin, View):
    """Preview the PDF layout with dummy data. POSTed from the settings form so the
    preview reflects the *current* (unsaved) header/footer/start-year and any
    just-picked logos, without saving anything."""

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            raise Http404("No active competition.")
        layout = ResultsPdfLayout.for_competition(competition)
        layout.header_html = pdfmarkup.sanitize_header(request.POST.get("header_html", ""))
        layout.footer_html = pdfmarkup.sanitize_footer(request.POST.get("footer_html", ""))
        layout.increment_start_year = _parse_year(request.POST.get("increment_start_year"))
        # A refused logo just doesn't appear in the preview — this response is a
        # PDF, so there is nowhere to put a message.
        for field in ("image_left", "image_right"):
            logo, _problem = _clean_logo(request.FILES.get(field))
            if logo is not None:
                setattr(layout, field, logo)
        section = sample_section(competition, layout)
        return _pdf_response(competition, layout, [section], "results-sample.pdf")


def _clean_logo(upload):
    """A picked logo, or ``(None, message)`` saying why it was refused.

    Size first, then Pillow's own verification via ``forms.ImageField`` — the same
    check a ModelForm would have run, which this page bypasses by assigning
    ``request.FILES`` onto the model directly."""
    if upload is None:
        return None, None
    if upload.size > MAX_LOGO_BYTES:
        return None, _(
            "The logo “%(name)s” is too large (limit %(limit)s MB)."
        ) % {"name": upload.name, "limit": MAX_LOGO_BYTES // (1024 * 1024)}
    try:
        return forms.ImageField().clean(upload), None
    except ValidationError:
        return None, _("“%(name)s” is not an image file.") % {"name": upload.name}


def _parse_year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2999 else None


def _parse_height(value, fallback):
    """A logo height in mm, clamped to a sane range; keeps ``fallback`` when blank."""
    try:
        height = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(5, min(60, height))


class ResultsPdfLogoRemoveView(ActiveCompetitionMixin, View):
    """One-click removal of a PDF logo (posted from the settings page)."""

    def post(self, request):
        competition = self.get_active()
        if competition is None:
            return JsonResponse({"ok": False, "error": "No active competition."}, status=400)
        side = request.POST.get("side")
        field = {"left": "image_left", "right": "image_right"}.get(side)
        if field is None:
            return JsonResponse({"ok": False, "error": "Unknown logo."}, status=400)
        layout = ResultsPdfLayout.objects.filter(competition=competition).first()
        if layout and getattr(layout, field):
            getattr(layout, field).delete(save=False)
            setattr(layout, field, None)
            layout.save(update_fields=[field])
        return JsonResponse({"ok": True})


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
        section = class_section(competition, cclass)
        return render(request, self.template_name, {
            "object": competition,
            "cclass": cclass,
            "scoring_label": section["scoring_label"],
            "scope": section["scope"],
            "layout": section["layout"],
            "ranked": section["ranked"],
            "unranked": section["unranked"],
            "export_url": reverse("results:export-class", args=[cclass.pk]),
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
        section = overall_section(competition, method, runs)
        return render(request, self.template_name, {
            "object": competition,
            "results": section["results"],
            "scoring_label": section["results"].method_label,
            "scope": section["scope"],
            "layout": section["layout"],
            "ranked": section["ranked"],
            "unranked": section["unranked"],
            "export_url": reverse("results:export-overall", args=[method, runs]),
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
            self._save_pdf_layout(request, competition)
        messages.success(request, _("Results settings saved."))
        return redirect(safe_next(request, reverse("results:settings")))

    @staticmethod
    def _save_pdf_layout(request, competition):
        """Persist the PDF header/footer layout: sanitised rich text, the increment
        start year, orientation, and the two optional logos and their heights.
        (Logo *removal* is a one-click AJAX action; see ResultsPdfLogoRemoveView.)"""
        layout, _ = ResultsPdfLayout.objects.get_or_create(competition=competition)
        layout.header_html = pdfmarkup.sanitize_header(request.POST.get("header_html", ""))
        layout.footer_html = pdfmarkup.sanitize_footer(request.POST.get("footer_html", ""))
        layout.increment_start_year = _parse_year(request.POST.get("increment_start_year"))
        orientation = request.POST.get("orientation")
        if orientation in ResultsPdfLayout.Orientation.values:
            layout.orientation = orientation
        for field, height_field in (
            ("image_left", "image_left_height"), ("image_right", "image_right_height")
        ):
            logo, problem = _clean_logo(request.FILES.get(field))
            if problem:
                # The rest of the settings still save; only the bad logo is dropped.
                messages.error(request, problem)
            elif logo is not None:
                setattr(layout, field, logo)
            setattr(layout, height_field,
                    _parse_height(request.POST.get(height_field), getattr(layout, height_field)))
        layout.save()

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
        layout = ResultsPdfLayout.for_competition(competition)
        sizes = [
            {"cls": cls, "label": label}
            for cls, label in (
                ("pdf-sz-small", "Small"),
                ("pdf-sz-medium", "Medium"),
                ("pdf-sz-large", "Large"),
            )
        ]
        orientations = [
            {"value": value, "label": label, "checked": layout.orientation == value}
            for value, label in ResultsPdfLayout.Orientation.choices
        ]
        return {
            "object": competition,
            "available": available,
            "has_columns": bool(available),
            "show_overall": ResultColumnSettings.overall_enabled(competition),
            "general_columns": general_columns,
            "class_rows": class_rows,
            "pdf_layout": layout,
            "pdf_wildcards": pdfmarkup.wildcard_values(competition, layout),
            "pdf_sizes": sizes,
            "pdf_orientations": orientations,
            "pdf_default_logo_height": ResultsPdfLayout.DEFAULT_LOGO_HEIGHT_MM,
            "pdf_export_all_url": reverse("results:export-all"),
            "pdf_sample_url": reverse("results:export-sample"),
            "pdf_logo_remove_url": reverse("results:pdf-logo-remove"),
        }
