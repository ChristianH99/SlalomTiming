"""What a bib number carries with it.

``TimedRun.bib_number`` is a loose integer, not a foreign key to the entry: a run
belongs to whoever wears that number, resolved fresh every time results are
computed. That is deliberate — a time is recorded against the number on the car,
which is what the timekeeper can actually see — but it means the number is not
just a label. Changing it hands every time already recorded under it to whoever
holds it next, and takes on any time already recorded under the new one.

Fixing a registration mistake by swapping two bibs is a perfectly ordinary thing
to do at a registration desk, and it used to do exactly that silently. So the
two places a bib can change (the participant form and the list's inline field)
ask first, and this is what they count.
"""

from django.db.models import Q


# What makes a run a *real* time rather than a placeholder the operator
# pre-entered. One definition, because the single-bib and the many-bib question
# below have to agree about what would be moved.
HAS_A_TIME = (
    Q(start_signal__isnull=False)
    | Q(finish_signal__isnull=False)
    | Q(manual_run_time__isnull=False)
)


def runs_recorded_for(competition, bib_number):
    """Runs in this competition that carry a real time under this bib.

    A placeholder row the operator pre-entered has no time on it yet and nothing
    to lose, so it doesn't count; a measured time and a keyed-in one both do.
    """
    from apps.timing.models import TimedRun

    if competition is None or bib_number is None:
        return TimedRun.objects.none()
    return TimedRun.objects.filter(
        competition=competition, bib_number=bib_number
    ).filter(HAS_A_TIME)


def runs_recorded_under(competition, bib_numbers):
    """The same question asked of a whole set of bibs, in one query.

    A re-draw moves a class's numbers all at once, and asking per bib would be a
    query per competitor on a confirmation screen.
    """
    from apps.timing.models import TimedRun

    numbers = [bib for bib in bib_numbers if bib is not None]
    if competition is None or not numbers:
        return 0
    return TimedRun.objects.filter(
        competition=competition, bib_number__in=numbers
    ).filter(HAS_A_TIME).count()


def bib_change_effect(competition, old_bib, new_bib):
    """What changing a participant's bib from ``old_bib`` to ``new_bib`` would
    move, or None when it moves nothing (and so needs no confirmation).

    ``leaving``  — times recorded under the old number, which stay with the
                   number and stop being this participant's.
    ``arriving`` — times already recorded under the new number, which become
                   this participant's.

    Either bib may be None: clearing a bib only has a ``leaving`` side, and
    assigning a fresh one only an ``arriving`` side.
    """
    if old_bib == new_bib:
        return None
    leaving = runs_recorded_for(competition, old_bib).count()
    arriving = runs_recorded_for(competition, new_bib).count()
    if not leaving and not arriving:
        return None
    return {
        "old_bib": old_bib,
        "new_bib": new_bib,
        "leaving": leaving,
        "arriving": arriving,
    }
