"""Building an export document.

Two scopes, per what an operator actually wants to move between systems:

* **event** — one competition and everything that belongs to it: its type, its
  classes and marshal posts, the participants entered (with their bibs), the
  timing captured, and the results configuration. Enough to re-run or re-print
  the event on another machine.
* **competition type** — a discipline and its rules, plus every participant
  registered under it, with no event attached: the reusable half.

Only ``export()`` is public; the per-section helpers below mirror the sections of
``schema.py`` one-for-one so the document's shape stays readable in one screen.
"""

import os.path

from django.utils import timezone

from apps.competitions.models import Competition, CompetitionType
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.results.models import ManualTieResolution, ResultColumnSettings, ResultsPdfLayout
from apps.timing.models import MarshalPenalty, TimedRun, TimingSignal

from . import archive, schema
from .schema import FORMAT, SCOPE_EVENT, SCOPE_TYPE, VERSION, dump


def export(*, competition=None, competition_type=None, include_timing=True):
    """The archive bytes for one event (pass ``competition``) or one competition
    type (pass ``competition_type``)."""
    if competition is not None:
        document, media = _event_document(competition, include_timing=include_timing)
    elif competition_type is not None:
        document, media = _type_document(competition_type)
    else:
        raise ValueError("export() needs a competition or a competition_type")
    return archive.write(document, media)


def filename(*, competition=None, competition_type=None):
    """A download name that says what's inside and when it was taken."""
    if competition is not None:
        stem = f"{competition.name}-{competition.date:%Y-%m-%d}"
    else:
        stem = f"{competition_type.name}-type"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stem).strip("-")
    return f"{safe or 'export'}-{timezone.localdate():%Y%m%d}.zip"


def _base(scope):
    return {
        "format": FORMAT,
        "version": VERSION,
        "scope": scope,
        "exported_at": timezone.now(),
    }


# --- competition type --------------------------------------------------------


def _type_document(competition_type):
    document = _base(SCOPE_TYPE)
    document["competition_type"] = dump(competition_type, schema.COMPETITION_TYPE_FIELDS)
    document["participants"] = _participants(
        Participant.objects.filter(competition_type=competition_type)
    )
    return document, {}


def _participants(queryset):
    return [dump(p, schema.PARTICIPANT_FIELDS) for p in queryset.order_by("pk")]


# --- event -------------------------------------------------------------------


def _event_document(competition, include_timing=True):
    document = _base(SCOPE_EVENT)
    document["competition_type"] = dump(
        competition.competition_type, schema.COMPETITION_TYPE_FIELDS
    )
    document["competition"] = dump(competition, schema.COMPETITION_FIELDS)

    classes = list(competition.classes.order_by("pk"))
    document["classes"] = [dump(cc, schema.CLASS_FIELDS) for cc in classes]
    document["marshal_posts"] = [
        dump(post, schema.MARSHAL_POST_FIELDS)
        for post in competition.marshal_posts.order_by("pk")
    ]

    entries = list(competition.entries.select_related("participant").order_by("pk"))
    assignments = list(
        ClassAssignment.objects.filter(competition_class__competition=competition).order_by("pk")
    )

    # Everyone the event touches: entered competitors plus anyone assigned to one
    # of its classes without a bib yet.
    participant_pks = {entry.participant_id for entry in entries}
    participant_pks.update(a.participant_id for a in assignments)
    document["participants"] = _participants(
        Participant.objects.filter(pk__in=participant_pks)
    )

    document["entries"] = [
        dict(dump(entry, schema.ENTRY_FIELDS), participant=entry.participant_id)
        for entry in entries
    ]
    document["class_assignments"] = [
        {
            "ref": a.pk,
            "participant": a.participant_id,
            "competition_class": a.competition_class_id,
        }
        for a in assignments
    ]

    # An archived event is rendered from its own frozen field rather than from
    # the tables above, so those rows travel too — see schema.
    document["archived_starters"] = [
        dict(
            dump(row, schema.ARCHIVED_STARTER_FIELDS),
            entry=row.entry_pk,
            participant=row.participant_pk,
        )
        for row in competition.archived_starters.order_by("pk")
    ]

    document["timing"] = _timing(competition) if include_timing else _empty_timing()
    document["results"], media = _results(competition)
    return document, media


def _empty_timing():
    return {"signals": [], "runs": [], "marshal_penalties": []}


def _timing(competition):
    signals = TimingSignal.objects.filter(competition=competition).order_by("pk")
    runs = TimedRun.objects.filter(competition=competition).order_by("pk")
    penalties = MarshalPenalty.objects.filter(timed_run__competition=competition).order_by("pk")
    return {
        "signals": [dump(signal, schema.SIGNAL_FIELDS) for signal in signals],
        "runs": [
            dict(
                dump(run, schema.RUN_FIELDS),
                start_signal=run.start_signal_id,
                finish_signal=run.finish_signal_id,
                competition_class=run.competition_class_id,
            )
            for run in runs
        ],
        "marshal_penalties": [
            dict(
                dump(penalty, schema.MARSHAL_PENALTY_FIELDS),
                timed_run=penalty.timed_run_id,
                marshal_post=penalty.marshal_post_id,
            )
            for penalty in penalties
        ],
    }


def _results(competition):
    columns = [
        dict(
            dump(row, schema.COLUMN_SETTINGS_FIELDS),
            competition_class=row.competition_class_id,
        )
        for row in ResultColumnSettings.objects.filter(competition=competition).order_by("pk")
    ]
    ties = [
        dump(tie, schema.TIE_RESOLUTION_FIELDS)
        for tie in ManualTieResolution.objects.filter(competition=competition).order_by("pk")
    ]

    media = {}
    layout_row = ResultsPdfLayout.objects.filter(competition=competition).first()
    layout = None
    if layout_row is not None:
        layout = dump(layout_row, schema.PDF_LAYOUT_FIELDS)
        for side in ("left", "right"):
            layout[f"image_{side}"] = _pack_logo(layout_row, side, media)

    return {"columns": columns, "pdf_layout": layout, "tie_resolutions": ties}, media


def _pack_logo(layout, side, media):
    """Copy one logo into the archive; returns the media name to reference, or
    None. A missing file (deleted from MEDIA_ROOT behind the app's back) is
    skipped rather than failing the whole export."""
    image = getattr(layout, f"image_{side}")
    if not image:
        return None
    extension = os.path.splitext(image.name)[1] or ".png"
    name = f"pdf-logo-{side}{extension}"
    try:
        with image.open("rb") as handle:
            media[name] = handle.read()
    except (FileNotFoundError, OSError, ValueError):
        return None
    return name


# --- what the export page offers --------------------------------------------


def exportable_competitions():
    return Competition.objects.select_related("competition_type").order_by("-date", "name")


def exportable_types():
    return CompetitionType.objects.order_by("name")


def event_summary(competition):
    """The counts the export page shows next to an event, so the operator can see
    what a file will contain before downloading it."""
    return {
        "classes": competition.classes.count(),
        "entries": EventEntry.objects.filter(competition=competition).count(),
        "runs": TimedRun.objects.filter(competition=competition).count(),
        "marshal_posts": competition.marshal_posts.count(),
    }


def type_summary(competition_type):
    return {
        "participants": Participant.objects.filter(competition_type=competition_type).count(),
        "competitions": competition_type.competitions.count(),
    }
