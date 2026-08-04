"""Copying a competition — and, for a signed-off one, the only way to edit it.

There are two jobs here and they look similar enough to be confused, which is
why they are one module with one switch rather than two functions:

* **Setup only** is what "Duplicate" has always meant: last year's classes, run
  order and marshal posts become this year's event. No bibs, no times — those
  belong to the event that recorded them.
* **Everything** is the way back into an archived event. Archiving is one-way
  (see archiving.py), so correcting a time on a signed-off event means copying
  it whole — times, signals, bibs, penalties, the lot — into a live competition
  and correcting it there. The original stays exactly as it was printed, which
  is the point of having archived it.

The second one has a problem the first does not: an archived event was run under
settings its type may have moved on from, and a *live* copy has to run under a
*live* type. Reconciling the two is a decision only the operator can make (this
year's discipline may genuinely have changed), so ``archiving.rule_differences``
lists what disagrees and the dialog asks. What they keep is written to the shared
type, which is why the dialog also names how many other competitions that moves.

Field lists come from ``apps/transfer/schema.py`` rather than being spelled out
again. That module already answers "what is a competition made of" for the
export, and two answers to that question drift the first time a field is added
to one of them.
"""

from dataclasses import dataclass, field

from django.db import transaction

from apps.transfer import merge, schema


@dataclass
class Plan:
    """What copying this event’s competitors would mean on this system.

    The same question an import asks, answered the same way and by the same
    code: a copy carries people who may since have been edited, deleted, or
    re-registered under a new record, and only the operator can say whether the
    "Ada Lovelace" on file is the one who raced. ``merge.match_participants``
    classifies each of them NEW / IDENTICAL / CONFLICT; a conflict is the merge
    window (competition_duplicate_review.html).
    """

    matches: list = field(default_factory=list)
    # Participant ref -> the last_used_at they had when the event was archived.
    # A restored competitor comes back with that date rather than today's; see
    # _restore_clocks.
    clocks: dict = field(default_factory=dict)

    @property
    def conflicts(self):
        return [m for m in self.matches if m.needs_decision]

    @property
    def needs_review(self):
        return bool(self.conflicts)

    @property
    def summary(self):
        return merge.summarize(self.matches)


def plan(original):
    """The competitor plan for a full copy of *original*."""
    rows, clocks = [], {}
    for entry in original.entry_rows():
        person = entry.participant
        rows.append({
            "ref": person.pk,
            **{name: getattr(person, name) for name in schema.PARTICIPANT_FIELDS},
        })
        clocks[person.pk] = person.last_used_at
    return Plan(
        matches=merge.match_participants(rows, original.competition_type),
        clocks=clocks,
    )


def copy(original, *, with_data, name=None, competitor_plan=None, resolutions=None):
    """A new, live competition from *original*.

    ``with_data`` carries the competitors, bibs, times and everything recorded
    against them; without it this is the setup only. ``competitor_plan`` and
    ``resolutions`` are the merge decisions (see ``plan``); without them every
    match takes its default, which is what an untouched review screen means.
    Returns the copy.
    """
    if with_data and competitor_plan is None:
        competitor_plan = plan(original)
    with transaction.atomic():
        copy_ = _competition(original, name)
        classes = _classes(original, copy_, keep_draw_state=with_data)
        _marshal_posts(original, copy_)
        if with_data:
            entries = _starters(
                original, copy_, classes, competitor_plan, resolutions or {}
            )
            _draw_numbers(original, copy_, entries)
            _timing(original, copy_, classes, entries)
            _results_config(original, copy_, classes, entries)
        else:
            _class_assignments(original, classes)
    return copy_


def _values(instance, fields):
    return {name: getattr(instance, name) for name in fields}


def _competition(original, name):
    from .models import Competition

    values = _values(original, schema.COMPETITION_FIELDS)
    # The copy is a *live* competition — that is the whole reason for making it.
    # auto_timing_order names rows that do not exist yet; it is rewritten at the
    # end of _timing once they do.
    values.pop("archived_at", None)
    values.pop("archived_rules", None)
    values["auto_timing_order"] = []
    values["name"] = name or f"{original.name} (Copy)"
    return Competition.objects.create(
        competition_type=original.competition_type, **values
    )


def _classes(original, copy_, *, keep_draw_state):
    """``{original class pk: copied class}``. Keyed by pk, not by name: a class
    can be renamed to match another one, and a copy that merged two classes
    because they had ended up sharing a name would put their competitors and
    times in one table with nothing to show for the loss.

    ``keep_draw_state`` is whether the copy inherits ``registration_closed_at``.
    A full copy does — it is the same event, being corrected, and its bibs are
    coming with it. A setup-only copy must not: that is *next year's* event, and
    classes that arrived already closed would refuse every registration before
    anybody had drawn a number.
    """
    from .models import CompetitionClass

    copy_.classes.all().delete()  # drop the defaults Competition.save() seeded
    mapping = {}
    for source in original.classes.all():
        values = _values(source, schema.CLASS_FIELDS)
        if not keep_draw_state:
            values["registration_closed_at"] = None
        mapping[source.pk] = CompetitionClass.objects.create(
            competition=copy_, **values
        )
    return mapping


def _draw_numbers(original, copy_, entries):
    """The numbers this event's competitors drew at registration.

    Only for a full copy, and only for competitors the copy actually took over.
    Who those are has to be looked up rather than assumed: ``_starters`` resolves
    each of the original's competitors against this system (they may have been
    edited, deleted or re-registered since — see ``plan``), so the participant a
    copied entry points at is often *not* the one the original drew for. The map
    is therefore original-participant → copied-participant, keyed through the
    entries, which are the only thing that names both sides.

    Two of them merged onto one record keep the first number, as on the import
    path: a merged person cannot have drawn twice.
    """
    from apps.participants.models import DrawNumber

    source_participant = {
        entry.pk: entry.participant_id for entry in original.entry_rows()
    }
    moved = {
        source_participant[source_pk]: copied.participant_id
        for source_pk, copied in entries.items()
        if source_pk in source_participant
    }
    seen, rows = set(), []
    for source in original.draw_numbers.all():
        person = moved.get(source.participant_id)
        if person is None or person in seen:
            continue
        seen.add(person)
        rows.append(DrawNumber(
            competition=copy_, participant_id=person, number=source.number))
    DrawNumber.objects.bulk_create(rows)


def _marshal_posts(original, copy_):
    from .models import MarshalPost

    mapping = {}
    for source in original.marshal_posts.all():
        mapping[source.pk] = MarshalPost.objects.create(
            competition=copy_, **_values(source, schema.MARSHAL_POST_FIELDS)
        )
    return mapping


def _class_assignments(original, classes):
    """Setup-only duplication keeps *who is in which class* but not their bibs —
    the classes are being reused for a new event, the registrations are not."""
    from apps.participants.models import ClassAssignment

    ClassAssignment.objects.bulk_create(
        ClassAssignment(
            participant_id=assignment.participant_id,
            competition_class=classes[assignment.competition_class_id],
        )
        for assignment in ClassAssignment.objects.filter(
            competition_class__competition=original
        )
        if assignment.competition_class_id in classes
    )


def _starters(original, copy_, classes, competitor_plan, resolutions):
    """Recreate the field: a participant, a bib and the class entries for each
    competitor. Returns ``{original entry pk: copied entry}``.

    Reads through ``entry_rows``, so an archived original is copied *as it was
    archived* — which is the only version of it that still exists. Who each of
    those competitors *is* on this system is then the import’s question, and is
    answered by the import’s own code (``merge.apply``): reuse the record when it
    matches, fold in whichever fields the operator kept when it does not, create
    one when nobody recognisable is on file. A competitor deleted since is simply
    the last of those — a copy missing them would be a start list with a hole in
    it.
    """
    from apps.participants.models import ClassAssignment, EventEntry, Participant

    # Read before anything is written: what each competitor already on file says
    # about when they were last used. That is the answer they keep — rolling it
    # back to the archived event's date would age somebody off who has raced
    # since. Only a competitor being *re-created* takes the snapshot's value,
    # because there is nothing else left of them.
    live_clocks = dict(
        Participant.objects.filter(competition_type=original.competition_type)
        .values_list("pk", "last_used_at")
    )
    by_ref = {m.ref: m for m in competitor_plan.matches}
    entries, used = {}, {}
    for source in original.entry_rows():
        ref = source.participant_id
        match = by_ref.get(ref)
        if match is None:            # a live original, planned against nothing
            person = source.participant
        else:
            person = merge.apply(
                match,
                resolutions.get(ref) or _default_resolution(match),
                original.competition_type,
            )
        if person.pk not in used:
            used[person.pk] = (
                live_clocks[person.pk] if person.pk in live_clocks
                else competitor_plan.clocks.get(ref)
            )
        entries[source.pk] = EventEntry.objects.create(
            competition=copy_,
            participant=person,
            bib_number=source.bib_number,
            status=source.status,
        )
        ClassAssignment.objects.bulk_create(
            ClassAssignment(participant=person, competition_class=classes[cc.pk])
            for cc in original.classes_for_participant(source.participant)
            if cc.pk in classes
        )
    _restore_clocks(used)
    return entries


def _default_resolution(match):
    """What happens to a competitor nobody decided about.

    ``merge.Match.default_resolution`` leads with the incoming values, which is
    right for an import. Here they are the archived event's, and the record on
    file is the current truth about a living person — so an undecided conflict
    reuses that record untouched rather than rewriting it from a finished event.
    """
    resolution = match.default_resolution()
    if resolution["action"] == merge.ACTION_MERGE:
        resolution["fields"] = {
            name: merge.KEEP_EXISTING for name in resolution["fields"]
        }
    return resolution


def _restore_clocks(used):
    """Put every touched competitor’s ``last_used_at`` back.

    Copying a finished event is not somebody racing again — and three separate
    things here say otherwise: ``Participant.save()`` stamps the field on every
    write, the ``EventEntry`` post_save stamps it whenever a bib is assigned, and
    both fire more than once per competitor during a copy. Left alone,
    duplicating a 2019 event would mark all of its competitors as used today,
    which is exactly the question the field exists to answer — it is what a
    retention sweep ages off (see apps/participants/models.Participant).

    So the clock is put back rather than held off: the signal and the save are
    the app’s normal behaviour and should stay that way for every other caller,
    and this runs inside the copy’s transaction, so an interrupted copy leaves
    nothing half-stamped. ``update()`` rather than ``save()``, because a save
    would stamp it again.
    """
    from apps.participants.models import Participant

    for pk, clock in used.items():
        Participant.objects.filter(pk=pk).update(last_used_at=clock)


def _timing(original, copy_, classes, entries):
    """Every signal and run, and the marshal penalties over them.

    Signals are copied first and mapped, because a run points at two of them
    through a OneToOne — pointing the copy at the *original's* signals would not
    duplicate the event, it would move half of it.
    """
    from apps.timing.models import MarshalPenalty, TimedRun, TimingSignal

    signals = {}
    for source in TimingSignal.objects.filter(competition=original).order_by("pk"):
        signals[source.pk] = TimingSignal.objects.create(
            competition=copy_, **_values(source, schema.SIGNAL_FIELDS)
        )

    posts = {
        post.number: post for post in copy_.marshal_posts.all()
    }
    runs = {}
    for source in TimedRun.objects.filter(competition=original).order_by("pk"):
        values = _values(source, schema.RUN_FIELDS)
        cclass = classes.get(source.competition_class_id)
        runs[source.pk] = TimedRun.objects.create(
            competition=copy_,
            competition_class=cclass,
            start_signal=signals.get(source.start_signal_id),
            finish_signal=signals.get(source.finish_signal_id),
            **values,
        )

    for source in MarshalPenalty.objects.filter(
        timed_run__competition=original
    ).select_related("marshal_post"):
        post = posts.get(source.marshal_post.number)
        run = runs.get(source.timed_run_id)
        if post is None or run is None:
            continue
        MarshalPenalty.objects.create(
            timed_run=run, marshal_post=post,
            **_values(source, schema.MARSHAL_PENALTY_FIELDS),
        )

    _auto_order(original, copy_, classes, entries)


def _auto_order(original, copy_, classes, entries):
    """The saved Auto-timing order, with the pks buried inside its slot keys
    rewritten. A stale pk here does not fail — it silently orders the copy's
    start list as if it were somebody else's event, which is why it is remapped
    explicitly rather than carried across (the import path does the same; see
    apps/transfer/importers._remap_auto_order)."""
    from apps.timing.autotiming import slot_key

    order = []
    for key in original.auto_timing_order or []:
        parts = str(key).split(":")
        if len(parts) != 5:
            continue
        entry = entries.get(_as_int(parts[0]))
        cclass = classes.get(_as_int(parts[1]))
        if entry is None or cclass is None:
            continue
        order.append(
            slot_key(entry.pk, cclass.pk, _as_int(parts[2]), parts[3], _as_int(parts[4]))
        )
    if order:
        copy_.auto_timing_order = order
        copy_.save(update_fields=["auto_timing_order"])


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _results_config(original, copy_, classes, entries):
    """The results columns, the PDF layout and the saved tie resolutions — the
    parts of a result that are a *decision* rather than a measurement, and the
    ones somebody copying an event to correct it would have to make again."""
    from apps.results.models import (
        ManualTieResolution, ResultColumnSettings, ResultsPdfLayout,
    )

    for source in ResultColumnSettings.objects.filter(competition=original):
        ResultColumnSettings.objects.create(
            competition=copy_,
            competition_class=classes.get(source.competition_class_id),
            **_values(source, schema.COLUMN_SETTINGS_FIELDS),
        )

    layout = ResultsPdfLayout.objects.filter(competition=original).first()
    if layout is not None:
        ResultsPdfLayout.objects.create(
            competition=copy_,
            image_left=layout.image_left,
            image_right=layout.image_right,
            **_values(layout, schema.PDF_LAYOUT_FIELDS),
        )

    for source in ManualTieResolution.objects.filter(competition=original):
        scope = _remap_scope(source.scope, classes)
        members = [
            [entries[entry_pk].pk, occurrence, rank]
            for entry_pk, occurrence, rank in (source.members or [])
            if entry_pk in entries
        ]
        if scope is None or not members:
            continue
        ManualTieResolution.objects.create(
            competition=copy_, scope=scope, members=members, score=source.score,
        )


def _remap_scope(scope, classes):
    """A tie resolution's scope names a class by pk (``class:12``); an Overall
    scope names no rows and travels unchanged."""
    if not scope.startswith("class:"):
        return scope
    try:
        pk = int(scope.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    cclass = classes.get(pk)
    return f"class:{cclass.pk}" if cclass else None


# Reconciling the frozen settings with the live type's is `archiving`'s job, not
# this module's — the import wizard asks the same question of a competition type
# that arrived in a file, and both write the answer back the same way. See
# archiving.setting_differences / archiving.adopt_settings.
