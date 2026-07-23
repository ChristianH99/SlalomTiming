"""Pluggable strategies for deciding which class(es) a participant belongs to.

Adding a new assignment method is a code-only change: define an ``AssignmentMethod``
subclass and register it below. Nothing else (UI, model choices) needs editing —
the Classes page and participant form adapt from each method's flags.

Methods only ever touch objects passed in, so this module imports no models and
can be imported from ``models.py`` for the field choices without a cycle.
"""

from django.utils.translation import gettext_lazy as _


class AssignmentMethod:
    key = ""
    label = ""
    uses_age = False              # tiles show age_from/age_to + birth years
    configurable_multiple = False  # the top "allow multiple classes" toggle is usable
    manual = False                # participants pick classes on their form; per-class repeat toggle shown

    def classes_for(self, competition, participant):
        """Return the list of CompetitionClass this participant belongs to for the
        competition (may contain repeats for manual multi-entry)."""
        raise NotImplementedError


class ManualAssignment(AssignmentMethod):
    key = "manual"
    label = _("Manual")
    manual = True
    configurable_multiple = True

    def classes_for(self, competition, participant):
        # Prefetch-friendly: iterate the participant's assignments in Python rather
        # than filtering in the DB, so a prefetch on the list view avoids N+1.
        return [
            a.competition_class
            for a in participant.class_assignments.all()
            if a.competition_class.competition_id == competition.id
            and a.competition_class.is_running
        ]


class AgeAssignment(AssignmentMethod):
    key = "age"
    label = _("Based on age")
    uses_age = True

    def classes_for(self, competition, participant):
        if participant.date_of_birth is None:
            return []
        cc = competition.class_for_birth_year(participant.date_of_birth.year)
        return [cc] if cc else []


ASSIGNMENT_METHODS = {m.key: m for m in [ManualAssignment(), AgeAssignment()]}
ASSIGNMENT_METHOD_CHOICES = [(m.key, m.label) for m in ASSIGNMENT_METHODS.values()]
DEFAULT_ASSIGNMENT_METHOD = ManualAssignment.key


def get_assignment_method(key):
    return ASSIGNMENT_METHODS.get(key) or ASSIGNMENT_METHODS[DEFAULT_ASSIGNMENT_METHOD]


def assignment_methods_meta():
    """Per-method flags for the Classes page JS (drives which tile controls show)."""
    return {
        m.key: {
            "uses_age": m.uses_age,
            "configurable_multiple": m.configurable_multiple,
            "manual": m.manual,
        }
        for m in ASSIGNMENT_METHODS.values()
    }
