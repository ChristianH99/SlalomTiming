"""Signing an event off: freezing the rules it was run under, and refusing to
write it afterwards.

The problem this exists for. A ``CompetitionType`` is a *discipline* and the
rules its competitions run under, and it is shared by every competition of that
discipline — so the timing precision, the penalty amounts and the tie-break that
decided July's event are read off a row somebody edits in November to set up the
next one. The times never move; the numbers computed over them do, silently,
the next time anybody opens the results. A finished result had no business
depending on a live row.

Archiving is the answer, and it has two halves, both of which are needed:

* **The snapshot.** ``archive()`` copies every setting of the competition's type
  onto the competition (``Competition.archived_rules``), and from then on
  ``Competition.rules`` hands out that copy instead of the live row. Every
  evaluation in the app reads ``rules`` — the results engine, both timing views,
  the dashboard, the PDFs — so freezing it here freezes all of them at once.
  This is why the swap was done at ``rules`` rather than at each caller: a
  reader added next year gets the frozen settings by asking the question the
  normal way.
* **The lock.** A frozen result that the app still lets you record times into is
  only half a promise: the settings would be the day's, the data would not.
  ``refuse_json``/``refuse_page`` are the one door every write asks, and
  ``config/archived_tests.py`` discovers the app's write endpoints and checks each
  one either refuses or is on a named list of things that legitimately still work
  (the device settings, which belong to the installation; creating or importing a
  competition, which makes a different one; the device's own signal door).

Reopening is deliberately possible and deliberately loud: an event signed off
too early is a mistake somebody has to be able to undo, and going back to the
live settings can change the results, so the page says so first.

Not to be confused with ``apps/transfer/archive.py``, which is the export
``.zip``. That one is a file; this one is a state an event is in. An export
*carries* this state (see ``apps/transfer/schema.COMPETITION_FIELDS``), so an
archived event moved to another machine arrives archived, with the settings it
was run under rather than whatever that machine's type row happens to say.
"""

from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import CompetitionType, FrozenCompetitionType

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
              "Reopen it from Competition Setup if you need to edit it.")


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


def archive(competition):
    """Sign *competition* off: freeze its type's settings onto it and mark it
    read-only. Idempotent — archiving an archived event leaves the original
    snapshot (and its timestamp) alone, so a second click cannot quietly re-take
    it against a type that has changed since."""
    if competition.is_archived:
        return competition
    competition.archived_at = timezone.now()
    competition.archived_rules = snapshot(competition.competition_type)
    competition.save(update_fields=["archived_at", "archived_rules"])
    _forget_rules(competition)
    return competition


def reopen(competition):
    """Undo the sign-off: the event follows its type's live settings again and
    may be edited.

    The snapshot is dropped rather than kept-but-ignored. Keeping it would leave
    two answers to "what rules is this event under?" on the same row, and the
    one in the column would be the wrong one — the whole point of ``rules`` is
    that there is exactly one place to look."""
    if not competition.is_archived:
        return competition
    competition.archived_at = None
    competition.archived_rules = {}
    competition.save(update_fields=["archived_at", "archived_rules"])
    _forget_rules(competition)
    return competition


def _forget_rules(competition):
    """Drop the cached ``rules`` so the instance in hand answers with the state
    it is now in, not the one it was loaded in."""
    competition.__dict__.pop("rules", None)


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
