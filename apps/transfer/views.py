"""The Backup section: automatic backup, export, import.

Automatic backup is the whole database on a timer, for getting the event back;
export is one event as a portable ``.zip``, for moving or archiving it. They read
as one section and are deliberately not one feature.

Export is one screen: pick an event or a competition type, get a ``.zip``.

Import is a wizard, because the interesting decisions can only be made once the
file has been read: upload → review (what this file would do here, and how to
resolve every participant it can't place unambiguously) → commit. The uploaded
archive waits in ``staging`` between the steps; the session holds only its token.
"""

from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.http import require_POST

from apps.common import other_signed_in_users
from apps.competitions import archiving
from apps.competitions.models import Competition, CompetitionType

from . import (archive, backup, csvimport, exporters, folders, importers,
               merge, staging)
from .forms import BackupSettingsForm
from .models import BackupSettings
from .schema import TransferError

# A participant list is text a human typed; a few thousand starters is well under a
# megabyte. The cap is here rather than in csvimport because it belongs to the door
# (`archive.MAX_UPLOAD_BYTES` does the same job for the export file), and because
# read() takes the whole file into memory.
MAX_CSV_BYTES = 8 * 1024 * 1024


class BackupView(View):
    """Backup → Automatic backup: where the database is copied, and how often.

    Deliberately not a "back up now" button. The point of this page is that the
    operator does *not* have to remember; a button invites them to think they
    should, and the one they forget is the one that mattered. Saving a
    destination checks it can be written to and makes the next copy due at once,
    so pressing Save is the confirmation that it works.
    """

    template_name = "transfer/backup.html"

    def get(self, request):
        settings = BackupSettings.load()
        return render(request, self.template_name,
                      self._context(BackupSettingsForm(instance=settings), settings))

    def post(self, request):
        settings = BackupSettings.load()
        form = BackupSettingsForm(request.POST, instance=settings)
        if not form.is_valid():
            return render(request, self.template_name,
                          self._context(form, settings))
        settings = form.save()
        if settings.enabled:
            backup.runner.start()
            # Due now, so the operator learns from this page whether it worked
            # rather than from a page they have navigated away from.
            backup.runner.run_soon()
            messages.success(request, _(
                "Backups are on. The first copy is being written now."))
        else:
            backup.runner.stop()
            messages.info(request, _("Backups are off."))
        return redirect("transfer:backup")

    @staticmethod
    def _context(form, settings):
        from django.urls import reverse

        return {
            "form": form,
            "settings": settings,
            "page_urls": {
                "status": reverse("transfer:backup-status"),
                "folders": reverse("transfer:backup-folders"),
            },
            "running": backup.runner.is_running,
            # Named here rather than in the template so the page can say what is
            # wrong with a destination that has stopped working — an unplugged
            # stick is the ordinary case, not an exotic one.
            "problem": settings.destination_problem() if settings.enabled else "",
        }


def backup_status(request):
    """The last result, polled by the page so a copy that has just been written
    (or just failed) shows up without a reload."""
    settings = BackupSettings.load()
    return JsonResponse({
        "enabled": settings.enabled,
        "running": backup.runner.is_running,
        "last_run_at": settings.last_run_at.isoformat() if settings.last_run_at else None,
        "last_ok_at": settings.last_ok_at.isoformat() if settings.last_ok_at else None,
        "last_error": settings.last_error,
        "last_file": settings.last_file,
        "last_bytes": settings.last_bytes,
    })


def backup_folders(request):
    """The folders on *this* machine, for the destination picker.

    A file input would offer the folders of whichever machine is displaying the
    page, and over the venue network that is usually somebody else's phone — see
    apps/transfer/folders.py for what this does and does not hand out.
    """
    return JsonResponse(folders.listing(request.GET.get("path") or ""))


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
        # A CSV registers starters *into the active competition*, which a signed-off
        # one no longer takes. The archive half of this page is unaffected: an
        # import creates its own competition and never touches this one.
        if archiving.refuse_page(request, competition):
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
    """The operator's decisions off the review form — shared with the archived
    competition's duplicate review, which asks the same question."""
    return merge.read_resolutions(post, plan.matches)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
