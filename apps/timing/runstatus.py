"""Closing a run with a state code instead of a time.

A run can end DNF (did not finish), DNC (did not classify), DNS (did not start)
or DSQ (disqualified). Three surfaces set one — the Manual timing view, the Auto
timing view and the results table's not-yet-ranked block — and all three come
through here, because two of them can name a run that **does not exist yet**:

  * DNS is the obvious case. A competitor who never started has no signal, so no
    ``TimedRun`` was ever opened for them; the row has to be made from their place
    in the start order before a status can be written to it.
  * the results table works entirely in start-order identities (bib × class ×
    occurrence × run), never in run ids.

So the unit of address is the **slot key** (``autotiming.slot_key``), and
``run_for_slot`` is the one door that turns one into a row — creating it
operator-owned, so it claims its slot in the Auto order and incoming device times
step over it rather than filling it in.

What a status means for a *result* is the class's business, not the run's: see
``apps/results/resultscalc.py`` for the DNS/DNC/DSQ rules.
"""

from apps.competitions.models import CompetitionClass
from apps.participants.models import EventEntry

from . import autotiming
from .models import TimedRun


def _as_pk(value):
    """A pk (or a small non-negative number) out of a slot key, else None."""
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None

# The state codes, in the order the timing screens offer them.
CHOICES = [
    TimedRun.Status.DNF,
    TimedRun.Status.DNC,
    TimedRun.Status.DNS,
    TimedRun.Status.DSQ,
]


def options():
    """``[{value, label}]`` for the timing views' status controls — the codes
    themselves as labels (DNF/DNC/DSQ are read as codes on a timing screen), with
    the spelt-out meaning carried as the title."""
    return [
        {"value": status.value, "label": status.value.upper(), "title": str(status.label)}
        for status in CHOICES
    ]


def parse(value):
    """A posted status -> a stored value, or None when it is not one of ours.
    ``""``/``None`` clears the status, which is a legitimate edit (an operator
    marked the wrong competitor)."""
    text = str(value or "").strip().lower()
    if text == "":
        return ""
    return text if text in TimedRun.Status.values else None


def run_for_slot(competition, slot_key):
    """Find (or create) the run for an Auto-timing start-order slot, so a time or
    a status can be put on a competitor who has no run yet. The slot key is
    ``entry:class:occurrence:run_type:run_number`` (see ``autotiming.slot_key``).
    A created run is operator-owned so it claims its slot and device times skip it.

    The stored binding is refreshed first: this looks an existing run up *by that
    identity*, and since the live views stopped persisting what they render (see
    ``autotiming.sync_bindings``) a stale row here would mean a second run created
    for a competitor who already has one.
    """
    autotiming.sync_bindings(competition)
    parts = str(slot_key or "").split(":")
    if len(parts) != 5:
        return None
    entry_pk, class_pk, occurrence, run_type, run_number = parts
    if run_type not in (TimedRun.RunType.PRACTICE, TimedRun.RunType.COUNTED):
        return None
    # A slot key arrives as text from a client, and both halves of it are pks.
    # Handing a non-numeric one to filter(pk=…) makes Django raise while it
    # prepares the query — the same 500 the timing endpoints used to have.
    entry_pk, class_pk = _as_pk(entry_pk), _as_pk(class_pk)
    occurrence, run_number = _as_pk(occurrence), _as_pk(run_number)
    if None in (class_pk, run_number) or entry_pk is None or occurrence is None:
        return None
    entry = EventEntry.objects.filter(competition=competition, pk=entry_pk).first()
    cclass = CompetitionClass.objects.filter(competition=competition, pk=class_pk).first()
    if entry is None or cclass is None:
        return None
    identity = dict(
        bib_number=entry.bib_number, competition_class=cclass,
        class_occurrence=occurrence, run_type=run_type, run_number=run_number,
    )
    return (
        TimedRun.objects.filter(competition=competition, **identity).first()
        or TimedRun.objects.create(competition=competition, manual_entry=True, **identity)
    )


def apply(run, status):
    """Write a status onto a run (``""`` clears it).

    Setting one takes the run over (``manual_entry``): a status is a statement
    about *this competitor's* run, so it must keep the slot it was set on rather
    than being handed to the next auto-bound starter. Clearing one leaves that
    ownership alone — the operator is undoing the status, not the identity.
    """
    fields = ["status", "updated_at"]
    run.status = status
    if status and not run.manual_entry:
        run.manual_entry = True
        fields.append("manual_entry")
    run.save(update_fields=fields)
