"""Handing out bibs from the numbers competitors drew at registration.

The desk does two things at once and they used to be the same thing: it takes
somebody's details, and it decides what number they will wear. Deciding the
second at the desk means the start order is whatever order people happened to
turn up in — which is fine for a training day and is not how a championship is
run. So the desk gives out a *drawn* number (a ticket, nothing to do with the
start order), and the bibs are handed out later, per class, in draw order.

Three things about that, each of which is a rule rather than a detail:

* **Per class, always.** A competition's classes are drawn and closed
  separately — the under-12s can be settled and racing while registration for
  the adults is still open. There is no "assign everything" here on purpose.

* **A bib somebody already has is never moved.** A manually typed bib is a
  decision (a returning champion keeps number 1, a sponsor's car is 7), so
  ``plan`` leaves those competitors exactly where they are and allocates
  *around* their numbers. The alternative — renumbering the field to close the
  gaps — would silently overwrite the one thing the operator did by hand.

* **Closing is the write, and it is what stops the draw.** Once a class is
  closed its bibs exist; a competitor who turns up afterwards can still be
  entered, but only by being given a bib by hand, because there is no longer a
  draw for them to be part of. ``CompetitionClass.registration_closed_at``
  records it, and `apps/participants/forms.py` is what refuses.

Nothing here is undoable by this module, and that is not an oversight: undoing
a draw means taking numbers off competitors who may already have run under
them. The bibs it wrote are ordinary bibs and can be edited one at a time,
exactly like any other, from the participant list.
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.competitions.models import Competition

from .models import DrawNumber, EventEntry

# Bib numbers are PositiveIntegerFields and an operator types the start of the
# range by hand, so the walk upwards needs an end. Far above any real field and
# far below the column's limit: the point is that a typo cannot spin.
MAX_BIB = 9999

# The drawn number is typed at the desk and stored in a PositiveIntegerField,
# which SQLite will hold whatever the size of. Bounded at the door for the same
# reason the timing views bound theirs: an absurd value is accepted on the way
# in and then raises on every later read of the row.
MAX_DRAW_NUMBER = 9999

ASCENDING = "asc"
DESCENDING = "desc"

ORDER_CHOICES = (
    (ASCENDING, _("Lowest draw number first")),
    (DESCENDING, _("Highest draw number first")),
)


@dataclass
class Assignment:
    """One competitor's place in the plan."""

    participant: object
    draw_number: int = None
    bib_number: int = None
    # True when they already had this bib before the draw ran — typed in by
    # hand at the desk. Shown as such, and left alone.
    kept: bool = False
    # Re-draws only: the bib they were wearing before. The preview shows it
    # beside the new one, because "which numbers actually move" is the question
    # somebody re-drawing a class is really asking.
    previous_bib: int = None


@dataclass
class Plan:
    """What closing this class would do, before anything is written.

    Only ``assignments`` — the competitors who would come out of this holding a
    bib. It deliberately does not count the ones it passes over: a class's
    membership is resolved from every participant registered under the
    *discipline*, which after a few seasons is mostly people who are not at this
    event at all. They have no drawn number and no bib here, so they are skipped
    correctly and counting them said "14 competitors will not get a bib" about a
    field of six. The draw is right; the number was the only thing that was
    wrong, so the number is gone.
    """

    competition_class: object
    assignments: list = field(default_factory=list)
    # Re-draws only: ``(participant, bib)`` for everybody whose current bib is
    # being handed back before the allocation runs. What the confirmation counts,
    # and what commit() deletes.
    released: list = field(default_factory=list)


def taken_bibs(competition):
    """Every bib already spoken for in this competition — across *all* classes,
    because a bib is unique to the event, not to the class being drawn."""
    return set(
        EventEntry.objects.filter(competition=competition)
        .values_list("bib_number", flat=True)
    )


def next_free_bib(competition, taken=None):
    """The lowest bib nobody holds. What the page offers as the starting point,
    so drawing class after class simply continues the numbering."""
    taken = taken_bibs(competition) if taken is None else taken
    bib = 1
    while bib in taken and bib <= MAX_BIB:
        bib += 1
    return bib


def _class_participants(competition, competition_class, running=None):
    """Everybody in this class, in no particular order.

    Asked of the competition rather than of the class's own assignment rows,
    because which class somebody is in is the *competition's* question — it may
    be answered by their age rather than by a ``ClassAssignment`` row (see
    apps/competitions/assignment.py), and a draw that only worked under manual
    assignment would silently skip every age-based event.

    The running classes are read once and handed down, not asked for inside the
    loop: age-based assignment resolves a competitor's class by walking them, so
    asking per person is one query per registered participant — the same cost
    the live endpoints had to have taken out of them.
    """
    from apps.participants.models import Participant

    if running is None:
        running = competition._running_classes_ordered()
    people = Participant.objects.filter(
        competition_type=competition.competition_type
    )
    if competition.assignment().manual:
        people = people.prefetch_related("class_assignments__competition_class")
    return [
        person for person in people
        if any(
            cc.pk == competition_class.pk
            for cc in competition.classes_for_participant(person, running=running)
        )
    ]


def plan(competition, competition_class, *, order=ASCENDING, start=None,
         reassign=False):
    """What assigning this class's bibs would do. Writes nothing.

    ``start`` is the first bib to hand out; ``None`` means the next free one.
    Numbers already taken anywhere in the competition are stepped over, so a
    class drawn second never collides with the one drawn first.

    ``reassign`` re-draws a class that has already been drawn. Everybody holding
    a *drawn* number gives their bib back and takes whatever the new order hands
    them; the bibs they were holding are freed first, so the class can be
    re-drawn into its own block of numbers rather than pushed to the end of the
    field. A competitor with **no** drawn number keeps their bib either way —
    that number was typed in by hand, which is a decision, and the draw has
    never been allowed to overwrite one.

    Modelling it here rather than by clearing the bibs first is what lets the
    page show the plan before anything is written: a preview that had to delete
    rows to be accurate would not be a preview.
    """
    taken = taken_bibs(competition)
    people = _class_participants(competition, competition_class)

    draws = dict(
        DrawNumber.objects.filter(
            competition=competition, participant__in=people
        ).values_list("participant_id", "number")
    )
    entries = dict(
        EventEntry.objects.filter(
            competition=competition, participant__in=people
        ).values_list("participant_id", "bib_number")
    )

    result = Plan(competition_class=competition_class)
    waiting = []
    for person in people:
        bib = entries.get(person.pk)
        draw = draws.get(person.pk)
        if reassign and draw is not None:
            # Back into the draw with whatever they are wearing now. Their old
            # number is freed before anything is allocated, so it is available
            # to this same re-draw instead of being stepped over as "taken".
            if bib is not None:
                result.released.append((person, bib))
                taken.discard(bib)
            waiting.append((draw, person))
        elif bib is not None:
            # Already wearing a number — a hand-typed decision, kept as it is,
            # and its bib stays out of the allocation below.
            result.assignments.append(
                Assignment(participant=person, draw_number=draw,
                           bib_number=bib, kept=True)
            )
        elif draw is not None:
            waiting.append((draw, person))
        # Neither number: registered under the discipline but not part of this
        # event (or not yet drawn). Nothing to hand out, so they are passed over.

    waiting.sort(key=lambda row: row[0], reverse=(order == DESCENDING))

    bib = next_free_bib(competition, taken) if start is None else max(int(start), 1)
    previous = {person.pk: old for person, old in result.released}
    for draw, person in waiting:
        while bib in taken and bib <= MAX_BIB:
            bib += 1
        if bib > MAX_BIB:
            break
        taken.add(bib)
        result.assignments.append(
            Assignment(participant=person, draw_number=draw, bib_number=bib,
                       previous_bib=previous.get(person.pk))
        )
        bib += 1

    # Kept bibs first, then the drawn ones in the order they were handed out, so
    # the preview reads as the start list it is about to become.
    result.assignments.sort(key=lambda row: row.bib_number)
    return result


@transaction.atomic
def commit(competition, competition_class, plan_):
    """Write *plan_*'s bibs and close the class. Returns how many were assigned.

    The class is closed even when the plan handed out nothing — closing is the
    operator saying the field is final, and a class whose competitors all had
    bibs typed in by hand is exactly as final as one that was drawn.

    A re-draw hands the released bibs back first, in the same transaction, so
    the numbers are free by the time the new ones are written. What is *not*
    given back is the entry's ``status``: a whole-event disqualification belongs
    to the competitor, not to the number they happened to be wearing, so it is
    carried across rather than quietly cleared by the delete.
    """
    statuses = {}
    if plan_.released:
        released_pks = [person.pk for person, _bib in plan_.released]
        old = EventEntry.objects.filter(
            competition=competition, participant_id__in=released_pks
        )
        statuses = dict(old.values_list("participant_id", "status"))
        old.delete()

    created = 0
    for row in plan_.assignments:
        if row.kept:
            continue
        fields = {}
        if row.participant.pk in statuses:
            fields["status"] = statuses[row.participant.pk]
        EventEntry.objects.create(
            participant=row.participant,
            competition=competition,
            bib_number=row.bib_number,
            **fields,
        )
        created += 1
    competition_class.registration_closed_at = timezone.now()
    competition_class.save(update_fields=["registration_closed_at"])
    return created


def closed_classes(competition):
    """The pks of this competition's classes whose draw has been closed — what
    the participant form asks before letting somebody be added without a bib."""
    return set(
        competition.classes.filter(registration_closed_at__isnull=False)
        .values_list("pk", flat=True)
    )


def set_draw_number(competition, participant, number):
    """Record (or clear) a participant's drawn number for this competition.

    ``None`` removes it. Returns the ``DrawNumber`` or ``None``; the caller is
    expected to have checked the number is free (the form does, so it can put
    the complaint on the field).
    """
    if number is None:
        DrawNumber.objects.filter(
            competition=competition, participant=participant
        ).delete()
        return None
    row, _created = DrawNumber.objects.update_or_create(
        competition=competition, participant=participant,
        defaults={"number": number},
    )
    return row


def draw_number_taken_by(competition, number, *, exclude_participant=None):
    """The participant already holding *number*, or ``None``. Two people on one
    ticket is the argument the draw exists to prevent, so it is checked at the
    door rather than left to the constraint."""
    rows = DrawNumber.objects.filter(competition=competition, number=number)
    if exclude_participant is not None:
        rows = rows.exclude(participant=exclude_participant)
    row = rows.select_related("participant").first()
    return row.participant if row else None
