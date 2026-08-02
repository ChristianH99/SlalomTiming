"""Signing an event off: freezing everything it was run with, for good.

The problem this exists for. A ``CompetitionType`` is a *discipline* and the
rules its competitions run under, and it is shared by every competition of that
discipline — so the timing precision, the penalty amounts and the tie-break that
decided July's event are read off a row somebody edits in November to set up the
next one. A ``Participant`` is the same shape of problem one layer down: they
belong to the discipline, and get edited, corrected and eventually deleted for
years after the event they raced in. The times never move; everything printed
around them does, silently, the next time anybody opens the results.

Archiving is the answer, and it has three parts:

* **The rules.** ``archive()`` copies every setting of the competition's type
  onto the competition (``Competition.archived_rules``), and from then on
  ``Competition.rules`` hands out that copy instead of the live row. Every
  evaluation in the app reads ``rules`` — the results engine, both timing views,
  the dashboard, the PDFs — so freezing it here freezes all of them at once.
* **The competitors.** ``ArchivedStarter`` rows carry each bib's participant
  details and class entries as they read on the day. The same readers get those
  back as ordinary ``EventEntry``/``Participant`` objects (``entry_rows``), so
  nothing downstream needs to know which kind of event it is looking at.
* **The lock.** ``refuse_json``/``refuse_page`` are the one door every write
  asks, and ``config/archived_tests.py`` discovers the app's write endpoints and
  checks each one either refuses or is on a named list of things that
  legitimately still work. The screens go read-only with it — the payloads carry
  ``read_only`` and the pages disable their own controls, because a stepper that
  takes a number and drops it is worse than one that is visibly dead.

**There is no way back.** Archiving is one-way on purpose. Un-archiving would
put an event back on settings that have moved on since, which is the exact
failure this exists to stop, and it would do it to an event whose results have
already been printed and handed out. Editing a signed-off event is done by
*duplicating* it (``apps/competitions/views.duplicate_competition``): the copy is
live, can carry the times and bibs, and reconciles the frozen settings with the
type's current ones in front of the operator.

Not to be confused with ``apps/transfer/archive.py``, which is the export
``.zip``. That one is a file; this one is a state an event is in. An export
*carries* this state (see ``apps/transfer/schema.py``), so an archived event
moved to another machine arrives archived, with everything it was run with.
"""

from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import ArchivedStarter, CompetitionType, FrozenCompetitionType

# Every CompetitionType field a snapshot carries — which is every field of it
# except the primary key.
#
# The whole row rather than "the ones that affect ranking", because the second
# list is a judgement that has to be re-made every time a field is added, and
# getting it wrong is invisible: the results still render, with one number quietly
# taken from November. `name` is here too — it is what the results PDF prints as
# the discipline, so it belongs to the day as much as the penalty amounts do.
FROZEN_FIELDS = tuple(
    field.name
    for field in CompetitionType._meta.concrete_fields
    if not field.primary_key
)

READ_ONLY = _("This competition is archived, so it cannot be changed. "
              "Duplicate it if you need an editable copy.")

# How the settings pop-up lays the frozen values out: the same groups, in the
# same order, under the same headings as the competition-type settings page.
# Somebody reading "what was this run under?" is comparing it against that page
# from memory, and a flat list of fifteen rows makes them do the sorting.
#
# Not a closed list: anything a later field adds that is named in no group falls
# into the last one rather than vanishing off the screen, and
# `test_every_frozen_setting_is_in_exactly_one_group` fails so it can be given a
# home deliberately.
RULE_GROUPS = (
    ("", ("name",)),
    (_("Penalties"), ("penalties_enabled", *CompetitionType.PENALTY_FIELDS)),
    (_("Evaluation"), ("tie_break", "timing_precision")),
    (_("Required participant info"), tuple(CompetitionType.PARTICIPANT_INFO)),
)

# Settings whose number means nothing without its unit. The penalty amounts are
# whole seconds; the timing precision carries its own unit in its choice label
# ("1/100 s"), and everything else is a word or a switch.
RULE_UNITS = {name: _("s") for name in CompetitionType.PENALTY_FIELDS}


# --- the rules --------------------------------------------------------------


def snapshot(competition_type):
    """The settings of *competition_type*, as the plain dict a competition
    stores. Values are whatever the fields hold — all of them JSON scalars, so
    the row round-trips through the column and through an export unchanged."""
    return {name: getattr(competition_type, name) for name in FROZEN_FIELDS}


def frozen_rules(data):
    """A stored snapshot back as something that behaves like the type it was
    taken from. Keys the current model no longer has are dropped rather than
    raising: a snapshot is written once and read for years, and an event whose
    results still render is worth more than a strict decode of a field that has
    since been removed."""
    known = {name: value for name, value in data.items() if name in FROZEN_FIELDS}
    return FrozenCompetitionType(**known)


def _labels():
    """setting name -> the label the competition-type settings page shows it
    under.

    Read off that page's own form rather than from the model's ``verbose_name``.
    Two reasons, and the second is why it matters: the form is where the
    operator learned these names ("Pylon", not "pylon penalty"), and a
    CompetitionType field carries no verbose_name at all — so Django derives an
    English string from the attribute name, which no catalogue has ever seen.
    The settings pop-up was printing "TIMING PRECISION" at a German operator.
    """
    from .forms import CompetitionTypeSettingsForm

    form = CompetitionTypeSettingsForm()
    return {name: field.label for name, field in form.fields.items()}


def rule_differences(competition):
    """``[{field, label, archived, current, archived_display, current_display}]``
    for every frozen setting whose value the live type no longer agrees with.

    What the duplicate-for-editing dialog is built from: a copy of a signed-off
    event has to be run under *some* live type, and the operator is the only one
    who can say whether this year's discipline should keep the old number or the
    new one. Empty means the type has not moved and there is nothing to ask.
    """
    if not competition.archived_rules:
        return []
    live = competition.competition_type
    labels = _labels()
    rows = []
    for name in FROZEN_FIELDS:
        archived = competition.archived_rules.get(name)
        current = getattr(live, name)
        if archived == current:
            continue
        field = CompetitionType._meta.get_field(name)
        rows.append({
            "field": name,
            "label": labels.get(name, name),
            "archived": archived,
            "current": current,
            "archived_display": _display(field, archived),
            "current_display": _display(field, current),
        })
    return rows


def rule_summary(competition):
    """``[{title, rows: [{label, value, value_unit, current, current_unit,
    changed}]}]`` — every frozen setting as the operator reads it on the settings
    page, beside what the live type says now, in that page's own groups.

    The pop-up showed the frozen column alone at first, which answers "what was
    this run under?" but not the question somebody actually opens it with, which
    is "and what would be different if I ran it today?". Both columns and a
    marker per row answer that at a glance; the count below is the same fact
    summed up.

    """
    if not competition.archived_rules:
        return []
    labels = _labels()
    live = competition.competition_type
    grouped = []
    for title, names in RULE_GROUPS:
        grouped.append({"title": title, "rows": [
            _rule_row(competition, live, labels, name)
            for name in names if name in FROZEN_FIELDS
        ]})
    # Whatever no group claimed, so a field added to the model later is visible
    # (in the last group) rather than silently missing from the pop-up.
    claimed = {name for _title, names in RULE_GROUPS for name in names}
    grouped[-1]["rows"].extend(
        _rule_row(competition, live, labels, name)
        for name in FROZEN_FIELDS if name not in claimed
    )
    return [group for group in grouped if group["rows"]]


def _rule_row(competition, live, labels, name):
    field = CompetitionType._meta.get_field(name)
    archived = competition.archived_rules.get(name)
    current = getattr(live, name)
    unit = RULE_UNITS.get(name, "")
    return {
        "label": labels.get(name, name),
        "value": _display(field, archived),
        # The unit rides beside the number, not in the label — the settings page
        # does the same — and only when there *is* a number: "— s" is not a
        # reading of a penalty that was never set.
        "value_unit": unit if archived is not None else "",
        "current": _display(field, current),
        "current_unit": unit if current is not None else "",
        "changed": archived != current,
    }


def _display(field, value):
    """A settings value as the operator sees it on the settings page: the choice
    label where there is one, Yes/No for a toggle, the value otherwise."""
    if field.choices:
        return dict(field.flatchoices).get(value, value)
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    return "—" if value is None else value


# --- the competitors --------------------------------------------------------

# Which Participant fields the snapshot keeps: everything the record holds about
# the person, minus the discipline they are registered under (the archived event
# carries its own) and the two timestamps that describe when the *row* was
# touched.
#
# `last_used_at` is kept, and it is the one that needed thinking about. It is the
# field a retention sweep ages off — "when was this person last used in a
# competition" — so it looks like live housekeeping rather than something about
# the day. But a competitor restored from this snapshot (because they have since
# been deleted) has to come back with the answer they had, not with today's date:
# putting them back must not restart their retention clock. See
# apps/competitions/duplication.py, which is the only thing that reads it.
_SKIP_PARTICIPANT_FIELDS = {"competition_type", "created_at", "updated_at"}


def _participant_fields():
    from apps.participants.models import Participant

    return [
        field for field in Participant._meta.concrete_fields
        if not field.primary_key and field.name not in _SKIP_PARTICIPANT_FIELDS
    ]


def starter_snapshot(competition):
    """``[ArchivedStarter, …]`` for every competitor with a bib, unsaved.

    **Entries, not participants**, and that is the whole definition of who was in
    the event. A Participant belongs to the *discipline* and can sit on file for
    years without entering anything; an ``EventEntry`` is a bib — the column is
    not nullable and clearing a bib deletes the row — so walking the entries is
    exactly "everyone who had a number on the day". Somebody merely assigned to a
    class, or registered under the type and never entered, was never part of this
    event and is not in its archive.

    Values go through the field's ``value_to_string`` and come back through its
    ``to_python`` (see ``_rebuild_participant``), the same way an export encodes
    itself — a date has to survive a JSON column, and the two directions must
    not be able to drift apart.
    """
    fields = _participant_fields()
    rows = []
    for entry in competition.entries.select_related("participant").order_by("bib_number"):
        participant = entry.participant
        rows.append(ArchivedStarter(
            competition=competition,
            entry_pk=entry.pk,
            participant_pk=participant.pk,
            bib_number=entry.bib_number,
            status=entry.status,
            details={
                field.name: field.value_to_string(participant) for field in fields
            },
            class_pks=[
                cc.pk for cc in competition.classes_for_participant(participant)
            ],
        ))
    return rows


def entry_rows(competition):
    """The archived event's competitors, as the ``EventEntry`` objects every
    reader in the app already expects — each carrying its ``Participant``.

    Unsaved instances with the original primary keys, exactly like
    ``frozen_rules``: the point is that a results table, a timing view or a
    dashboard tile does not have to know whether the event it is rendering is
    live. They are never saved; the writes that would save them are refused
    long before they get here.
    """
    from apps.participants.models import EventEntry

    rows = []
    for row in competition.archived_starters.all():
        entry = EventEntry(
            id=row.entry_pk,
            competition=competition,
            bib_number=row.bib_number,
            status=row.status,
        )
        entry.participant = _rebuild_participant(row)
        rows.append(entry)
    return rows


def _rebuild_participant(row):
    from apps.participants.models import Participant

    person = Participant(id=row.participant_pk)
    for field in _participant_fields():
        if field.name in row.details:
            setattr(person, field.name, field.to_python(row.details[field.name]))
    return person


def class_map(competition):
    """``{participant pk: [CompetitionClass, …]}`` as the event was entered —
    repeats in order, so a competitor entered twice in a class still gets two
    slots. Replaces the live assignment lookup for an archived event, which is
    what makes ``classes_for_participant`` answer the same way for ever.
    """
    classes = {cc.pk: cc for cc in competition.classes.all()}
    return {
        row.participant_pk: [classes[pk] for pk in row.class_pks if pk in classes]
        for row in competition.archived_starters.all()
    }


# --- signing off ------------------------------------------------------------


def archive(competition):
    """Sign *competition* off: freeze its type's settings and its competitors
    onto it, and mark it read-only.

    Idempotent, and one-way. Archiving an archived event leaves the original
    snapshot alone, so a second click cannot quietly re-take it against a type
    that has changed since; and there is no ``reopen`` to pair with this — see
    the module docstring.
    """
    from django.db import transaction

    if competition.is_archived:
        return competition
    with transaction.atomic():
        starters = starter_snapshot(competition)
        competition.archived_at = timezone.now()
        competition.archived_rules = snapshot(competition.competition_type)
        competition.save(update_fields=["archived_at", "archived_rules"])
        ArchivedStarter.objects.bulk_create(starters)
    competition.forget_archive_caches()
    return competition


# --- the lock ---------------------------------------------------------------
#
# Two doors because the app has two kinds of write: a page that posts a form and
# expects to be redirected somewhere with a message, and a JSON endpoint a live
# view calls with fetch(). Both answer the same question and say the same thing.


def refuse_json(competition):
    """A refusal for a JSON endpoint asked to write an archived competition, or
    ``None`` when the write may go ahead (including with no event selected — that
    is a different refusal, and the endpoints make it themselves).

    409 rather than 403: nothing is wrong with who is asking, the event is in a
    state that does not take writes. That distinction is load-bearing on the
    Marshal Posts board, whose outbox retries a failed push with backoff *for
    ever* — a 409 is the one answer it drops quietly (static/js/marshal_posts.js),
    so an archived event lets a stranded phone give up instead of hammering the
    server until somebody notices. ``archived`` is in the body so a caller that
    wants to tell this apart from a locked post can.
    """
    if competition is None or not competition.is_archived:
        return None
    return JsonResponse(
        {"ok": False, "archived": True, "error": str(READ_ONLY)}, status=409
    )


def refuse_page(request, competition):
    """Whether a form post must be turned away, saying so in a message first.
    ``False`` — the ordinary case — leaves the request untouched."""
    if competition is None or not competition.is_archived:
        return False
    messages.error(request, READ_ONLY)
    return True
