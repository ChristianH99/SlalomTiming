"""The Import / Export pages.

Export is one screen: pick an event or a competition type, get a ``.zip``.

Import is a wizard, because the interesting decisions can only be made once the
file has been read: upload → review (what this file would do here, and how to
resolve every participant it can't place unambiguously) → commit. The uploaded
archive waits in ``staging`` between the steps; the session holds only its token.
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.http import require_POST

from apps.common import other_signed_in_users
from apps.competitions.models import Competition, CompetitionType

from . import archive, csvimport, exporters, importers, merge, staging
from .schema import TransferError

# A participant list is text a human typed; a few thousand starters is well under a
# megabyte. The cap is here rather than in csvimport because it belongs to the door
# (`archive.MAX_UPLOAD_BYTES` does the same job for the export file), and because
# read() takes the whole file into memory.
MAX_CSV_BYTES = 8 * 1024 * 1024


class ExportView(View):
    template_name = "transfer/export.html"

    def get(self, request):
        return render(request, self.template_name, self._context())

    def _context(self):
        return {
            "competitions": [
                {"competition": competition, "summary": exporters.event_summary(competition)}
                for competition in exporters.exportable_competitions()
            ],
            "types": [
                {"type": competition_type, "summary": exporters.type_summary(competition_type)}
                for competition_type in exporters.exportable_types()
            ],
        }

    def post(self, request):
        target = request.POST.get("target")
        pk = request.POST.get("pk")

        if target == "competition":
            competition = get_object_or_404(Competition, pk=pk)
            payload = exporters.export(
                competition=competition,
                include_timing=request.POST.get("include_timing") == "on",
            )
            name = exporters.filename(competition=competition)
        elif target == "type":
            competition_type = get_object_or_404(CompetitionType, pk=pk)
            payload = exporters.export(competition_type=competition_type)
            name = exporters.filename(competition_type=competition_type)
        else:
            messages.error(request, _("Choose what to export."))
            return redirect("transfer:export")

        response = HttpResponse(payload, content_type="application/zip")
        # Built by Django rather than interpolated: the name carries a competition
        # name (see apps/results/views.py::_pdf_response for what that cost).
        response["Content-Disposition"] = content_disposition_header(True, name)
        return response


class ImportView(View):
    """The one place data comes in, offering the two kinds of file side by side:

    * a **Slalom Timing export** (.zip) — step 1 of the review wizard. It is parsed
      straight away so a wrong file is rejected here, where the operator still has
      the file picker in front of them, rather than after they've worked through a
      review screen.
    * a **participant list** (.csv) — registered into the active competition in one
      go (see csvimport). Refused whole if any line is incomplete, so the page
      re-renders with what was wrong rather than redirecting.

    Which one was submitted is read off the file field's name; the CSV half is the
    only part that needs an active competition, so the page still works without one.
    """

    template_name = "transfer/import.html"

    def get(self, request):
        return render(request, self.template_name, self._context())

    def _context(self, csv_report=None, csv_result=None):
        competition = Competition.get_current()
        return {
            "competition": competition,
            "columns": csvimport.columns_for(competition) if competition else [],
            "csv_report": csv_report,
            "csv_result": csv_result,
        }

    def post(self, request):
        if "csv" in request.FILES:
            return self._import_participants(request)
        return self._stage_archive(request)

    def _stage_archive(self, request):
        upload = request.FILES.get("archive")
        if upload is None:
            messages.error(request, _("Choose a file to import."))
            return redirect("transfer:import")
        if upload.size > archive.MAX_UPLOAD_BYTES:
            messages.error(request, _(
                "That file is %(size)s MB — too large for an export archive "
                "(the limit is %(limit)s MB)."
            ) % {
                "size": upload.size // (1024 * 1024),
                "limit": archive.MAX_UPLOAD_BYTES // (1024 * 1024),
            })
            return redirect("transfer:import")

        try:
            document, _media = archive.read(upload)
            importers.plan(document)
        except TransferError as error:
            messages.error(request, str(error))
            return redirect("transfer:import")

        upload.seek(0)
        _discard_staged(request)
        request.session[staging.SESSION_KEY] = staging.store(upload)
        request.session["transfer_import_name"] = upload.name
        return redirect("transfer:review")

    def _import_participants(self, request):
        competition = Competition.get_current()
        if competition is None:
            messages.error(request, _("Choose a competition to import participants into."))
            return redirect("transfer:import")

        upload = request.FILES["csv"]
        if upload.size > MAX_CSV_BYTES:
            messages.error(request, _(
                "That file is %(size)s MB — too large for a participant list "
                "(the limit is %(limit)s MB)."
            ) % {
                "size": upload.size // (1024 * 1024),
                "limit": MAX_CSV_BYTES // (1024 * 1024),
            })
            return redirect("transfer:import")

        report = csvimport.read(upload.read(), competition)
        if not report.ok:
            return render(request, self.template_name, self._context(csv_report=report))

        result = csvimport.commit(report, competition)
        messages.success(request, _("Participants imported."))
        return render(request, self.template_name, self._context(csv_result=result))


class ImportReviewView(View):
    """Step 2 — what this file would do here, and the decisions only a human can
    make: which competition type it lands in, and every participant the system
    recognises but can't match exactly."""

    template_name = "transfer/import_review.html"
    done_template_name = "transfer/import_done.html"

    def get(self, request):
        staged = _staged(request)
        if staged is None:
            return redirect("transfer:import")
        _document, _media, plan = staged
        return render(request, self.template_name, self._context(request, plan))

    def _context(self, request, plan):
        return {
            "plan": plan,
            # "Make this the current event" moves every open screen, exactly as
            # creating a competition does. Named, not refused.
            "others": other_signed_in_users(request),
            "current": Competition.get_current(),
            "filename": request.session.get("transfer_import_name", ""),
            "summary": plan.participant_summary,
            "conflicts": plan.conflicts,
            "type_actions": plan.type_actions(),
            "default_type_action": plan.default_type_action(),
            "identical": [m for m in plan.matches if m.status == merge.IDENTICAL],
            "new_participants": [m for m in plan.matches if m.status == merge.NEW],
        }

    def post(self, request):
        staged = _staged(request)
        if staged is None:
            return redirect("transfer:import")
        _document, media, plan = staged

        try:
            result = importers.commit(
                plan,
                resolutions=_resolutions(request.POST, plan),
                type_action=request.POST.get("type_action"),
                media=media,
                activate=request.POST.get("activate") == "on",
            )
        except TransferError as error:
            # A value the document carries but the database can't hold (see
            # schema.load). commit() is one transaction, so nothing was written.
            _discard_staged(request)
            messages.error(request, str(error))
            return redirect("transfer:import")

        _discard_staged(request)
        messages.success(request, _("Import finished."))
        return render(request, self.done_template_name, {"result": result})


def csv_sample(request):
    """The example file, with exactly the columns this competition asks for."""
    competition = Competition.get_current()
    if competition is None:
        return redirect("transfer:import")
    response = HttpResponse(
        csvimport.sample_csv(competition), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = content_disposition_header(
        True, "participants-sample.csv")
    return response


@require_POST
def cancel_import(request):
    _discard_staged(request)
    messages.info(request, _("Import cancelled — nothing was changed."))
    return redirect("transfer:import")


# --- wizard plumbing ---------------------------------------------------------


def _staged(request):
    """``(document, media, plan)`` for the archive this session is importing, or
    None when there is nothing staged (a fresh session, or a swept upload)."""
    token = request.session.get(staging.SESSION_KEY)
    raw = staging.read(token) if token else None
    if raw is None:
        messages.error(request, _("The upload has expired. Please choose the file again."))
        return None
    try:
        document, media = archive.read(raw)
        return document, media, importers.plan(document)
    except TransferError as error:
        _discard_staged(request)
        messages.error(request, str(error))
        return None


def _discard_staged(request):
    token = request.session.pop(staging.SESSION_KEY, None)
    request.session.pop("transfer_import_name", None)
    if token:
        staging.discard(token)


def _resolutions(post, plan):
    """The operator's per-participant decisions, read off the review form.

    Each conflict renders one radio group ``choice-<ref>`` — "create", or
    "merge:<pk>" naming the existing participant — plus, per candidate, a
    per-field group ``field-<ref>-<pk>-<name>`` choosing which side wins. The
    field groups are keyed by candidate so picking a different candidate can't
    inherit the previous one's choices.

    Only conflicts render inputs, so anything absent keeps that match's default —
    which is exactly what an untouched review screen should mean.
    """
    resolutions = {}
    for match in plan.matches:
        if not match.needs_decision:
            continue

        choice = post.get(f"choice-{match.ref}") or ""
        if choice == merge.ACTION_CREATE:
            resolutions[match.ref] = {"action": merge.ACTION_CREATE, "target": None, "fields": {}}
            continue

        candidate = match.candidate(_int(choice.split(":", 1)[1])) if ":" in choice else None
        if candidate is None:
            continue  # unreadable choice: fall back to this match's default

        resolutions[match.ref] = {
            "action": merge.ACTION_MERGE,
            "target": candidate.pk,
            "fields": {
                diff.field: (
                    merge.KEEP_EXISTING
                    if post.get(f"field-{match.ref}-{candidate.pk}-{diff.field}") == merge.KEEP_EXISTING
                    else merge.KEEP_IMPORTED
                )
                for diff in candidate.diffs
            },
        }
    return resolutions


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
