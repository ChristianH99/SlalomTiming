from collections import Counter

from django.db import models

from . import startpattern
from .assignment import (
    ASSIGNMENT_METHOD_CHOICES,
    DEFAULT_ASSIGNMENT_METHOD,
    get_assignment_method,
)


class CompetitionType(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Competition(models.Model):
    competition_type = models.ForeignKey(
        CompetitionType, on_delete=models.PROTECT, related_name="competitions"
    )
    name = models.CharField(max_length=150)
    date = models.DateField()
    is_active = models.BooleanField(default=False, help_text="The competition currently being run.")
    assignment_method = models.CharField(
        max_length=20,
        choices=ASSIGNMENT_METHOD_CHOICES,
        default=DEFAULT_ASSIGNMENT_METHOD,
        help_text="How participants are assigned to classes.",
    )
    allow_multiple_classes = models.BooleanField(
        default=False,
        help_text="Manual assignment only: may a participant be in more than one class.",
    )
    start_pattern = models.JSONField(
        default=list,
        blank=True,
        help_text="The order participants take their runs, as pattern blocks "
        "(see apps/competitions/startpattern.py). Replayed for every run.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.name} ({self.date:%Y-%m-%d})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            CompetitionClass.objects.bulk_create(
                CompetitionClass(competition=self, name=name, position=position)
                for position, name in enumerate(CompetitionClass.DEFAULT_NAMES)
            )

    def active_classes(self):
        return [cc.name for cc in self._running_classes_ordered()]

    def _running_classes_ordered(self):
        """Running classes in run order: by run_position (unplaced last), then
        by list position."""
        return sorted(
            self.classes.filter(is_running=True),
            key=lambda cc: (
                cc.run_position is None,
                cc.run_position if cc.run_position is not None else 0,
                cc.position,
            ),
        )

    def run_groups(self):
        """Ordered runs as a list of lists of running CompetitionClass.
        Classes sharing a run_position start together; runs execute in ascending
        run_position. A running class with run_position=None becomes its own
        single-class run, appended in list order after the placed runs."""
        placed = {}
        unplaced = []
        for cc in self._running_classes_ordered():
            if cc.run_position is None:
                unplaced.append([cc])
            else:
                placed.setdefault(cc.run_position, []).append(cc)
        groups = [placed[key] for key in sorted(placed)]
        groups.extend(unplaced)
        return groups

    def start_pattern_blocks(self):
        """The stored start pattern as startpattern.Block values."""
        return startpattern.parse(self.start_pattern)

    def starters_by_class(self):
        """Map class pk -> the Starters entered in it, in bib order. One Starter
        per entry-in-a-class, so a participant entered into a class twice (or into
        two classes) yields a Starter each time."""
        entries = (
            self.entries.select_related("participant")
            .prefetch_related("participant__class_assignments__competition_class")
            .order_by("bib_number")
        )
        by_class = {}
        repeats = Counter()  # (entry, class) -> Starters already made, to key repeats apart
        for entry in entries:
            for cc in self.classes_for_participant(entry.participant):
                occurrence = repeats[(entry.pk, cc.pk)]
                repeats[(entry.pk, cc.pk)] += 1
                by_class.setdefault(cc.pk, []).append(
                    startpattern.Starter(
                        key=(entry.pk, cc.pk, occurrence),
                        bib=entry.bib_number,
                        name=str(entry.participant),
                        class_name=cc.name,
                        practice_runs=cc.practice_runs,
                        counted_runs=cc.counted_runs,
                    )
                )
        return by_class

    def starters_by_run(self):
        """``(run, starters)`` for every run in ``run_groups()``. A run's starters
        are those of all its classes merged into one start list ordered by bib —
        classes sharing a run start together, so they interleave."""
        by_class = self.starters_by_class()
        runs = []
        for run in self.run_groups():
            starters = []
            for cc in run:
                starters.extend(by_class.get(cc.pk, []))
            starters.sort(key=lambda starter: (starter.bib, starter.class_name))
            runs.append((run, starters))
        return runs

    def start_lists(self):
        """``(run, slots)`` for every run: the start pattern played out over each
        run's starters — who starts, in which run type, in order."""
        blocks = self.start_pattern_blocks()
        return [
            (run, startpattern.expand(blocks, starters))
            for run, starters in self.starters_by_run()
        ]

    def class_for_birth_year(self, birth_year):
        if birth_year is None:
            return None
        age = self.date.year - birth_year
        for competition_class in self.classes.filter(is_running=True):
            if (
                competition_class.age_from is not None
                and competition_class.age_to is not None
                and competition_class.age_from <= age <= competition_class.age_to
            ):
                return competition_class
        return None

    def assignment(self):
        """The AssignmentMethod strategy for this competition."""
        return get_assignment_method(self.assignment_method)

    def allows_multiple_classes_effective(self):
        """Whether a participant may be in multiple distinct classes, honouring
        both the method (must support it) and the competition's toggle."""
        return self.assignment().configurable_multiple and self.allow_multiple_classes

    def classes_for_participant(self, participant):
        """Resolve the class(es) a participant belongs to under the current
        assignment method (may repeat for manual multi-entry)."""
        return self.assignment().classes_for(self, participant)

    @classmethod
    def get_current(cls):
        return cls.objects.filter(is_active=True).first()


class CompetitionClass(models.Model):
    # Starter classes seeded on a brand-new competition; fully editable afterwards.
    DEFAULT_NAMES = ["1", "2", "3", "4", "5", "6", "E"]

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="classes")
    name = models.CharField(max_length=50)
    position = models.PositiveIntegerField(default=0, help_text="Display order in the classes list.")
    is_running = models.BooleanField(default=False)
    age_from = models.PositiveIntegerField(null=True, blank=True, help_text="Starting age, e.g. 6")
    age_to = models.PositiveIntegerField(null=True, blank=True, help_text="Ending age, e.g. 7")
    practice_runs = models.PositiveIntegerField(default=1)
    counted_runs = models.PositiveIntegerField(default=2)
    allow_multiple_entries = models.BooleanField(
        default=False,
        help_text="Manual assignment only: may a participant be entered into this class more than once.",
    )
    run_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="The run this class belongs to. Classes sharing a run_position start "
        "together; runs execute in ascending order. Null when not placed.",
    )

    class Meta:
        ordering = ["position", "name"]
        unique_together = ("competition", "name")

    def __str__(self):
        return f"{self.competition} – {self.name}"

    def birth_year_range(self):
        """Birth years spanned by this class's age range, oldest (from age_to)
        first. Deliberately not reordered by magnitude: age_to always drives
        the first value and age_from always drives the second, so changing
        just one age field only ever moves the number it corresponds to."""
        if self.age_from is None or self.age_to is None:
            return None
        year = self.competition.date.year
        return (year - self.age_to, year - self.age_from)
