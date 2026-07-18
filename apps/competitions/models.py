from collections import Counter

from django.db import models

from . import startpattern
from .assignment import (
    ASSIGNMENT_METHOD_CHOICES,
    DEFAULT_ASSIGNMENT_METHOD,
    get_assignment_method,
)


class CompetitionType(models.Model):
    """A discipline (Motorcycle, Go-Cart, …) and the rules every competition of
    that discipline is run and evaluated under. The evaluation settings are
    recorded here only; the timing screen, the results calculation and the
    participant form each read them when those features are built."""

    class TieBreak(models.TextChoices):
        FASTEST_RUN = "fastest_run", "Fastest run time"
        MANUAL = "manual", "Manual"

    class Precision(models.IntegerChoices):
        """Decimal places the timing device resolves to."""

        TENTHS = 1, "1/10 s"
        HUNDREDTHS = 2, "1/100 s"
        THOUSANDTHS = 3, "1/1000 s"

    # Penalty amounts are only meaningful with penalties on, so they are nullable
    # at the DB level; the settings form makes them mandatory when the toggle is on.
    # Whole seconds only — a penalty is never a fraction of a second.
    PENALTY_FIELDS = ["pylon_penalty", "task_penalty", "stop_line_penalty", "max_penalty_per_task"]

    _penalty_amount = {"null": True, "blank": True}

    name = models.CharField(max_length=100, unique=True)

    penalties_enabled = models.BooleanField(
        default=True,
        help_text="Show the penalties screen during timing so penalties can be entered per run.",
    )
    pylon_penalty = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text="Seconds added per pylon hit.",
    )
    task_penalty = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text="Seconds added for a failed task.",
    )
    stop_line_penalty = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text="Seconds added for missing the stop line.",
    )
    max_penalty_per_task = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text="Upper bound on the seconds a single task can add.",
    )

    tie_break = models.CharField(
        max_length=20,
        choices=TieBreak.choices,
        default=TieBreak.FASTEST_RUN,
        help_text="How equal results are separated.",
    )
    timing_precision = models.PositiveSmallIntegerField(
        choices=Precision.choices,
        default=Precision.HUNDREDTHS,
        help_text="Resolution of the timing device.",
    )

    # Which optional participant details this discipline collects. Whether a collected
    # field is mandatory is fixed per setting (see PARTICIPANT_INFO), not chosen here.
    requires_co_driver = models.BooleanField(default=False)
    requires_vehicle = models.BooleanField(default=False)
    requires_address = models.BooleanField(default=True)
    requires_club = models.BooleanField(default=True)
    requires_license = models.BooleanField(default=True)
    requires_email = models.BooleanField(default=True)
    requires_phone = models.BooleanField(default=True)

    # setting name -> (label, mandatory, the Participant fields it controls). The
    # participant form builds itself from this: a setting that's off hides its
    # fields, one that's on shows them and marks them per `mandatory`.
    PARTICIPANT_INFO = {
        "requires_co_driver": (
            "Co-driver", False, ["co_driver_first_name", "co_driver_last_name"],
        ),
        "requires_vehicle": ("Vehicle", True, ["vehicle"]),
        "requires_address": (
            "Address", True, ["address_street", "address_zip_code", "address_city"],
        ),
        "requires_club": ("Club", True, ["club"]),
        "requires_license": ("Licence number", True, ["license_number"]),
        "requires_email": ("E-mail", True, ["email"]),
        "requires_phone": ("Phone", False, ["phone_number"]),
    }

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def optional_participant_fields(cls):
        """Every Participant field whose presence a type decides."""
        return [
            field
            for _, _, fields in cls.PARTICIPANT_INFO.values()
            for field in fields
        ]

    def participant_field_requirements(self):
        """``{Participant field: is_mandatory}`` for the details this type
        collects. Fields of a setting that's off are absent — the participant
        form hides those."""
        return {
            field: mandatory
            for setting, (_, mandatory, fields) in self.PARTICIPANT_INFO.items()
            if getattr(self, setting)
            for field in fields
        }

    def format_time(self, seconds):
        """A time in seconds rendered at this type's device precision."""
        if seconds is None:
            return ""
        return f"{seconds:.{self.timing_precision}f}"


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

    class Scoring(models.TextChoices):
        """How a class's counted runs turn into a result. Recorded per class here;
        the calculation itself lives with the results feature."""

        AGGREGATE = "aggregate", "Aggregate times"
        REGULARITY = "regularity", "Regularity test"

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="classes")
    name = models.CharField(max_length=50)
    position = models.PositiveIntegerField(default=0, help_text="Display order in the classes list.")
    is_running = models.BooleanField(default=False)
    age_from = models.PositiveIntegerField(null=True, blank=True, help_text="Starting age, e.g. 6")
    age_to = models.PositiveIntegerField(null=True, blank=True, help_text="Ending age, e.g. 7")
    practice_runs = models.PositiveIntegerField(default=1)
    counted_runs = models.PositiveIntegerField(default=2)
    scoring_method = models.CharField(
        max_length=20,
        choices=Scoring.choices,
        default=Scoring.AGGREGATE,
        help_text="How this class's counted runs are turned into a result.",
    )
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
