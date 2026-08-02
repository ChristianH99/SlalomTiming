"""Reading an export document back into this system.

Two phases, because the operator sits between them:

* ``plan()`` inspects a document without writing anything — which competition
  type it lands in, and what each incoming participant means here (see merge.py).
  The review step renders this.
* ``commit()`` takes the plan plus the operator's resolutions and writes, inside
  one transaction, so a half-imported event can never exist.

Nothing from the document's own ``ref`` ids reaches the database: each section
builds a ``ref -> new object`` map and every foreign key is looked up through it.
Two places hide pks *inside* text and are remapped explicitly — the Auto timing
order's slot keys and a manual tie resolution's scope/members — because a stale
pk there silently misorders an imported event rather than failing loudly.
"""

from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.results import logos, pdfmarkup
from apps.results.models import ManualTieResolution, ResultColumnSettings, ResultsPdfLayout
from apps.timing.autotiming import slot_key
from apps.timing.models import MarshalPenalty, TimedRun, TimingSignal

from . import merge, schema
from .schema import SCOPE_EVENT, SCOPE_TYPE, TransferError, load

# What to do when the file's competition type already exists here by name.
#
# There used to be a third: "reuse" (keep this system's settings) beside
# "update" (take the file's, all of them). Both were the whole row at once,
# which is the wrong grain — a file usually agrees about fourteen settings and
# disagrees about one, and neither answer was right for that. TYPE_MERGE covers
# both and everything between: the settings that differ are listed and answered
# one at a time (see Plan.type_settings), so "keep mine" and "take theirs" are
# now the two ends of the same control rather than the only two options.
TYPE_MERGE = "merge"      # use the existing type, per-setting (see type_settings)
TYPE_CREATE = "create"    # register a separate type under a free name


@dataclass
class Plan:
    """What importing this document would do, before anything is written."""

    document: dict
    scope: str
    type_name: str
    existing_type: CompetitionType = None
    matches: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    # The competition-type settings the file and this system's type disagree
    # about, in the shape the shared dialog renders (see
    # apps/competitions/archiving.setting_differences). Empty when the type is
    # new here, or when the two agree about everything — which is the ordinary
    # case and asks the operator nothing.
    type_settings: list = field(default_factory=list)

    @property
    def is_event(self):
        return self.scope == SCOPE_EVENT

    @property
    def competition_name(self):
        return (self.document.get("competition") or {}).get("name", "")

    @property
    def conflicts(self):
        return [m for m in self.matches if m.needs_decision]

    @property
    def participant_summary(self):
        return merge.summarize(self.matches)

    def default_type_action(self):
        return TYPE_MERGE if self.existing_type is not None else TYPE_CREATE

    def type_actions(self):
        """The choices the review step offers for the competition type.

        None at all when the type is new here, and none when an *event* lands on
        a type this system already has: the event has to be evaluated by some
        type, and registering a second one under a made-up name to hold one
        club's spelling of the same discipline is not a choice anybody wants.
        What is still asked in that case is the settings — see ``type_settings``.
        """
        if self.existing_type is None or self.scope != SCOPE_TYPE:
            return []
        return [
            (TYPE_MERGE, _("Use the existing competition type")),
            (TYPE_CREATE, _("Register a separate competition type")),
        ]


def plan(document):
    """Inspect a document. Raises TransferError if it isn't one we can import."""
    scope = document.get("scope")
    if scope not in schema.SCOPES:
        raise TransferError(_("The export does not say what it contains."))

    type_row = document.get("competition_type") or {}
    type_name = (type_row.get("name") or "").strip()
    if not type_name:
        raise TransferError(_("The export has no competition type."))
    if scope == SCOPE_EVENT and not document.get("competition"):
        raise TransferError(_("The export claims to be an event but holds no competition."))

    existing_type = CompetitionType.objects.filter(name__iexact=type_name).first()
    participants = document.get("participants") or []

    return Plan(
        document=document,
        scope=scope,
        type_name=type_name,
        existing_type=existing_type,
        matches=merge.match_participants(
            _participant_rows(participants), existing_type
        ),
        counts=_counts(document),
        type_settings=_type_settings(type_row, existing_type),
    )


def _type_settings(type_row, existing_type):
    """What the file's competition type and this system's disagree about.

    Compared on *decoded* values, not on the raw JSON: ``schema.load`` puts each
    one back through its own field's ``to_python``, so a setting written by
    another system is compared the way this one would store it rather than the
    way JSON happened to spell it. It is also the check that stops a damaged
    document here, at the door, instead of at commit time behind a screen the
    operator has already worked through.

    ``name`` is excluded — see archiving.setting_differences.
    """
    if existing_type is None:
        return []
    from apps.competitions import archiving

    values = load(CompetitionType, type_row, schema.COMPETITION_TYPE_FIELDS)
    return archiving.setting_differences(values, existing_type, skip=("name",))


def _counts(document):
    timing = document.get("timing") or {}
    results = document.get("results") or {}
    return {
        "participants": len(document.get("participants") or []),
        "classes": len(document.get("classes") or []),
        "entries": len(document.get("entries") or []),
        "marshal_posts": len(document.get("marshal_posts") or []),
        "signals": len(timing.get("signals") or []),
        "runs": len(timing.get("runs") or []),
        "tie_resolutions": len(results.get("tie_resolutions") or []),
    }


@dataclass
class Result:
    """What a commit actually did, for the confirmation page."""

    competition: Competition = None
    competition_type: CompetitionType = None
    created_participants: int = 0
    merged_participants: int = 0
    reused_participants: int = 0
    entries: int = 0
    runs: int = 0
    warnings: list = field(default_factory=list)


@transaction.atomic
def commit(plan, resolutions, type_action=None, type_settings=None, media=None,
           activate=False):
    """Write the planned import. ``resolutions`` maps a participant ref to the
    operator's decision (see merge.apply) and ``type_settings`` a competition-type
    setting to the side they kept (see _resolve_type); anything missing falls back
    to that match's default, and — for a setting — to this system's value.

    Every way a document can be wrong comes back as a TransferError, because the
    operator is holding a file somebody else's system wrote and a traceback tells
    them nothing they can act on. schema.load catches the values a column cannot
    hold; this catches the shapes a *table* cannot hold — two General column rows,
    two entries on one bib — which are constraint violations rather than bad
    values. The whole thing is one transaction, so a refusal writes nothing.
    """
    document = plan.document
    media = media or {}
    result = Result()

    try:
        result.competition_type = _resolve_type(
            plan, type_action or plan.default_type_action(), type_settings)
        participants = _resolve_participants(plan, resolutions, result)

        if plan.is_event:
            _import_event(document, result, participants, media, activate)
    except IntegrityError as exc:
        raise TransferError(
            _("The export holds data this system can’t store together (%(detail)s). "
              "Nothing was imported.") % {"detail": str(exc)[:120]}
        ) from None

    return result


def _resolve_type(plan, action, settings):
    """The competition type this import lands in, reconciled with the file's.

    ``settings`` is ``{field: "incoming" | "current"}`` — the operator's answer
    per disagreeing setting. Anything they were not asked about, or did not
    answer, keeps this system's value: the type is shared by every competition
    of the discipline, so an import quietly moving a penalty amount would
    re-rank results of events that have nothing to do with this file.
    """
    from apps.competitions import archiving

    row = plan.document.get("competition_type") or {}
    values = load(CompetitionType, row, schema.COMPETITION_TYPE_FIELDS)

    if plan.existing_type is not None and action != TYPE_CREATE:
        archiving.adopt_settings(
            plan.existing_type, plan.type_settings, settings or {}
        )
        return plan.existing_type

    values["name"] = _free_type_name(values.get("name") or plan.type_name)
    return CompetitionType.objects.create(**values)


def _free_type_name(name):
    """A type name not already taken (CompetitionType.name is unique)."""
    if not CompetitionType.objects.filter(name__iexact=name).exists():
        return name
    stem = _("%(name)s (imported)") % {"name": name}
    candidate, index = stem, 2
    while CompetitionType.objects.filter(name__iexact=candidate).exists():
        candidate = f"{stem} {index}"
        index += 1
    return candidate


def _resolve_participants(plan, resolutions, result):
    """ref -> Participant, creating/merging per the operator's decisions."""
    mapping = {}
    for match in plan.matches:
        resolution = resolutions.get(match.ref) or match.default_resolution()
        participant = merge.apply(match, resolution, result.competition_type)
        mapping[match.ref] = participant
        action = resolution.get("action")
        if action == merge.ACTION_MERGE:
            result.merged_participants += 1
        elif action == merge.ACTION_REUSE:
            result.reused_participants += 1
        else:
            result.created_participants += 1
    return mapping


def _import_event(document, result, participants, media, activate):
    competition = _create_competition(document, result.competition_type, activate)
    result.competition = competition

    classes = _import_classes(document, competition)
    posts = _import_marshal_posts(document, competition)
    entries = _import_entries(document, competition, participants, result)
    _import_class_assignments(document, participants, classes)
    _import_draw_numbers(document, competition, participants)

    runs = _import_timing(document, competition, classes, result)
    _import_marshal_penalties(document, runs, posts)
    _import_results(document, competition, classes, entries, media)
    _import_archived_starters(document, competition, participants, entries, classes)

    _remap_auto_order(document, competition, entries, classes)


def _create_competition(document, competition_type, activate):
    values = load(Competition, document["competition"], schema.COMPETITION_FIELDS)
    # auto_timing_order holds source pks; rewritten by _remap_auto_order once the
    # entries and classes it names exist.
    values["auto_timing_order"] = []
    competition = Competition.objects.create(competition_type=competition_type, **values)
    # Competition.save() seeds the default classes on create; the file brings its own.
    competition.classes.all().delete()
    if activate:
        Competition.objects.exclude(pk=competition.pk).update(is_active=False)
        competition.is_active = True
        competition.save(update_fields=["is_active", "updated_at"])
    return competition


def _import_classes(document, competition):
    mapping = {}
    for row in document.get("classes") or []:
        mapping[row["ref"]] = CompetitionClass.objects.create(
            competition=competition, **load(CompetitionClass, row, schema.CLASS_FIELDS)
        )
    return mapping


def _import_marshal_posts(document, competition):
    mapping = {}
    for row in document.get("marshal_posts") or []:
        mapping[row["ref"]] = MarshalPost.objects.create(
            competition=competition, **load(MarshalPost, row, schema.MARSHAL_POST_FIELDS)
        )
    return mapping


def _import_entries(document, competition, participants, result):
    """ref -> EventEntry. Two incoming competitors merged into one existing
    participant would collide on this competition's one-entry-per-participant
    rule; the second keeps the first's entry and the operator is told."""
    mapping = {}
    taken = {}
    for row in document.get("entries") or []:
        participant = participants.get(row.get("participant"))
        if participant is None:
            continue
        if participant.pk in taken:
            mapping[row["ref"]] = taken[participant.pk]
            result.warnings.append(
                _("%(name)s appears twice in the file (bib %(bib)s was merged into "
                  "bib %(kept)s).")
                % {
                    "name": str(participant),
                    "bib": row.get("bib_number"),
                    "kept": taken[participant.pk].bib_number,
                }
            )
            continue
        entry = EventEntry.objects.create(
            competition=competition,
            participant=participant,
            **load(EventEntry, row, schema.ENTRY_FIELDS),
        )
        mapping[row["ref"]] = entry
        taken[participant.pk] = entry
        result.entries += 1
    return mapping


def _import_class_assignments(document, participants, classes):
    for row in document.get("class_assignments") or []:
        participant = participants.get(row.get("participant"))
        competition_class = classes.get(row.get("competition_class"))
        if participant is None or competition_class is None:
            continue
        ClassAssignment.objects.create(
            participant=participant, competition_class=competition_class
        )


def _import_draw_numbers(document, competition, participants):
    """The numbers competitors drew at registration (apps/participants/draw.py).

    Two incoming competitors merged onto one participant would collide on the
    one-number-per-participant rule, exactly as they do on entries — the first
    keeps their number and the second is dropped, since a merged person cannot
    have drawn twice.
    """
    from apps.participants.models import DrawNumber

    seen = set()
    rows = []
    for row in document.get("draw_numbers") or []:
        participant = participants.get(row.get("participant"))
        if participant is None or participant.pk in seen:
            continue
        seen.add(participant.pk)
        rows.append(DrawNumber(
            competition=competition, participant=participant,
            **load(DrawNumber, row, schema.DRAW_NUMBER_FIELDS),
        ))
    DrawNumber.objects.bulk_create(rows)


def _import_timing(document, competition, classes, result):
    timing = document.get("timing") or {}

    signals = {}
    arrived = []
    for row in timing.get("signals") or []:
        values = load(TimingSignal, row, schema.SIGNAL_FIELDS)
        # received_at is auto_now_add, so it is stamped on insert and put back
        # afterwards — arrangement.py orders runs by arrival, not device clock,
        # so losing it would reshuffle the imported event's runs.
        received_at = values.pop("received_at", None)
        signal = TimingSignal.objects.create(competition=competition, **values)
        signals[row["ref"]] = signal
        if received_at is not None:
            signal.received_at = received_at
            arrived.append(signal)
    if arrived:
        TimingSignal.objects.bulk_update(arrived, ["received_at"])

    runs = {}
    for row in timing.get("runs") or []:
        run = TimedRun.objects.create(
            competition=competition,
            start_signal=signals.get(row.get("start_signal")),
            finish_signal=signals.get(row.get("finish_signal")),
            competition_class=classes.get(row.get("competition_class")),
            **load(TimedRun, row, schema.RUN_FIELDS),
        )
        runs[row["ref"]] = run
        result.runs += 1
    return runs


def _import_marshal_penalties(document, runs, posts):
    timing = document.get("timing") or {}
    for row in timing.get("marshal_penalties") or []:
        run = runs.get(row.get("timed_run"))
        post = posts.get(row.get("marshal_post"))
        if run is None or post is None:
            continue
        MarshalPenalty.objects.create(
            timed_run=run,
            marshal_post=post,
            **load(MarshalPenalty, row, schema.MARSHAL_PENALTY_FIELDS),
        )


def _import_results(document, competition, classes, entries, media):
    results = document.get("results") or {}

    for row in results.get("columns") or []:
        ResultColumnSettings.objects.create(
            competition=competition,
            competition_class=classes.get(row.get("competition_class")),
            **load(ResultColumnSettings, row, schema.COLUMN_SETTINGS_FIELDS),
        )

    layout_row = results.get("pdf_layout")
    if layout_row:
        values = load(ResultsPdfLayout, layout_row, schema.PDF_LAYOUT_FIELDS)
        # The PDF header/footer is the one field in the document that holds *markup*,
        # and it comes from another club's machine. The settings page renders it back
        # into its editor as HTML, so an unsanitised import is script running in the
        # importer's session — which for a superuser is the whole system. The editor
        # sanitises what an operator types; an import has to go through the same door
        # (and the template sanitises again on the way out — see
        # apps/results/templatetags/pdf_markup.py).
        values["header_html"] = pdfmarkup.sanitize_header(values.get("header_html", ""))
        values["footer_html"] = pdfmarkup.sanitize_footer(values.get("footer_html", ""))
        layout = ResultsPdfLayout(competition=competition, **values)
        layout.save()
        for side in ("left", "right"):
            name = layout_row.get(f"image_{side}")
            content = media.get(name) if name else None
            if not content:
                continue
            # Bytes and name both come from another club's machine, and MEDIA_ROOT
            # is served back by this app. Written unchecked, an archive could plant
            # an HTML file and have the app serve it as HTML — script running in
            # the importer's session — or a `../` name and a SuspiciousFileOperation
            # mid-transaction. logos.clean_bytes verifies it is really an image and
            # gives it a name of ours; a refusal costs the emblem, not the import.
            logo = logos.clean_bytes(name, content)
            if logo is None:
                continue
            getattr(layout, f"image_{side}").save(logo.name, logo, save=True)

    _import_tie_resolutions(results, competition, classes, entries)


def _import_tie_resolutions(results, competition, classes, entries):
    """A tie resolution names its table by pk ("class:<pk>") and its members by
    entry pk, so both sides need remapping; one that points at something the
    import didn't bring across is dropped rather than saved pointing nowhere."""
    for row in results.get("tie_resolutions") or []:
        values = load(ManualTieResolution, row, schema.TIE_RESOLUTION_FIELDS)
        scope = values.get("scope") or ""
        if scope.startswith("class:"):
            source_pk = _as_int(scope.split(":", 1)[1])
            competition_class = classes.get(source_pk)
            if competition_class is None:
                continue
            values["scope"] = f"class:{competition_class.pk}"

        members = []
        for member in values.get("members") or []:
            if not isinstance(member, (list, tuple)) or len(member) < 3:
                continue
            entry = entries.get(member[0])
            if entry is None:
                break
            members.append([entry.pk, member[1], member[2]])
        else:
            values["members"] = members
            ManualTieResolution.objects.create(competition=competition, **values)


def _participant_rows(rows):
    """A document's participant rows decoded into typed values.

    This is where the file stops being text and starts being data, so it is
    where it is checked: ``schema.load`` puts every value back through its own
    field's ``to_python`` and then its validators, turning a damaged export into
    one sentence rather than a row every later read chokes on. Callers holding
    rows that came out of this database hand ``match_participants`` their values
    directly and skip all of it — see that function.
    """
    return [
        {"ref": row["ref"], **load(Participant, row, schema.PARTICIPANT_FIELDS)}
        for row in rows
    ]


def _import_archived_starters(document, competition, participants, entries, classes):
    """The frozen field of an archived event.

    Three ids inside these rows name rows elsewhere in the same document and are
    remapped like any other pk — the entry and participant they stand for, and
    the classes they were entered in. That matters more here than anywhere else:
    an archived event is rendered *only* from these rows, so a stale pk is not a
    broken link somebody notices, it is a competitor who silently vanishes from a
    result that has already been printed.
    """
    from apps.competitions.models import ArchivedStarter

    rows = []
    for row in document.get("archived_starters") or []:
        entry = entries.get(row.get("entry"))
        participant = participants.get(row.get("participant"))
        if entry is None or participant is None:
            continue
        values = load(ArchivedStarter, row, schema.ARCHIVED_STARTER_FIELDS)
        values["class_pks"] = [
            classes[pk].pk for pk in (row.get("class_pks") or []) if pk in classes
        ]
        rows.append(ArchivedStarter(
            competition=competition,
            entry_pk=entry.pk,
            participant_pk=participant.pk,
            **values,
        ))
    ArchivedStarter.objects.bulk_create(rows)


def _remap_auto_order(document, competition, entries, classes):
    """Rewrite the saved Auto timing order onto the new pks. A slot key is
    ``entry:class:occurrence:run_type:run_number`` (apps/timing/autotiming.py);
    a slot naming something that wasn't imported is dropped, which just means the
    computed order decides where it sits."""
    order = document["competition"].get("auto_timing_order") or []
    remapped = []
    for key in order:
        parts = str(key).split(":")
        if len(parts) != 5:
            continue
        entry = entries.get(_as_int(parts[0]))
        competition_class = classes.get(_as_int(parts[1]))
        if entry is None or competition_class is None:
            continue
        remapped.append(
            slot_key(entry.pk, competition_class.pk, _as_int(parts[2]), parts[3], _as_int(parts[4]))
        )
    if remapped:
        competition.auto_timing_order = remapped
        competition.save(update_fields=["auto_timing_order", "updated_at"])


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
