from collections import Counter
from functools import lru_cache

from django.conf import settings
from django.db import models
from django.utils import translation
from django.utils.translation import gettext, gettext_lazy as _

from . import startpattern, taskspec
from .assignment import (
    ASSIGNMENT_METHOD_CHOICES,
    DEFAULT_ASSIGNMENT_METHOD,
    get_assignment_method,
)

# Seconds a marshal-post claim survives without a heartbeat before it's free again.
CLAIM_TTL = 30


class CompetitionType(models.Model):
    """A discipline (Motorcycle, Go-Cart, …) and the rules every competition of
    that discipline is run and evaluated under. The evaluation settings are
    recorded here only; the timing screen, the results calculation and the
    participant form each read them when those features are built.

    **A type is shared by every competition of that discipline, and its settings
    are read live.** So changing a penalty amount, the tie-break or the timing
    precision in November re-ranks July's event the next time anybody opens its
    results — the times are unchanged, the numbers over them are not. That is
    accepted for now: the settings are edited between events, not during one,
    and the alternative is versioning every field.

    The proper answer is an **archive**: a snapshot of a competition — its results
    as computed on the day, with the type settings that produced them — taken when
    the event is signed off, so a finished result stops depending on a live row.
    That is a feature, not a fix, and it belongs with the export machinery in
    apps/transfer/ when it is built.
    """

    class TieBreak(models.TextChoices):
        FASTEST_RUN = "fastest_run", _("Fastest run time")
        MANUAL = "manual", _("Manual")

    class Precision(models.IntegerChoices):
        """Decimal places the timing device resolves to."""

        # Translated: a device *name* ("Tag Heuer CP540") is the same word
        # everywhere, but these are units and belong in the reader's language.
        TENTHS = 1, _("1/10 s")
        HUNDREDTHS = 2, _("1/100 s")
        THOUSANDTHS = 3, _("1/1000 s")

    # Penalty amounts are only meaningful with penalties on, so they are nullable
    # at the DB level; the settings form makes them mandatory when the toggle is on.
    # Whole seconds only — a penalty is never a fraction of a second.
    PENALTY_FIELDS = ["pylon_penalty", "task_penalty", "stop_line_penalty", "max_penalty_per_task"]

    _penalty_amount = {"null": True, "blank": True}

    name = models.CharField(max_length=100, unique=True)

    penalties_enabled = models.BooleanField(
        default=True,
        help_text=_("Show the penalties screen during timing so penalties can be entered per run."),
    )
    pylon_penalty = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text=_("Seconds added per pylon hit."),
    )
    task_penalty = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text=_("Seconds added for a failed task."),
    )
    stop_line_penalty = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text=_("Seconds added for missing the stop line."),
    )
    max_penalty_per_task = models.PositiveSmallIntegerField(
        **_penalty_amount, help_text=_("Upper bound on the seconds a single task can add."),
    )

    tie_break = models.CharField(
        max_length=20,
        choices=TieBreak.choices,
        default=TieBreak.FASTEST_RUN,
        help_text=_("How equal results are separated."),
    )
    timing_precision = models.PositiveSmallIntegerField(
        choices=Precision.choices,
        default=Precision.HUNDREDTHS,
        help_text=_("Resolution of the timing device."),
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
            _("Co-driver"), False, ["co_driver_first_name", "co_driver_last_name"],
        ),
        "requires_vehicle": (_("Vehicle"), True, ["vehicle"]),
        "requires_address": (
            _("Address"), True, ["address_street", "address_zip_code", "address_city"],
        ),
        "requires_club": (_("Club"), True, ["club"]),
        "requires_license": (_("Licence number"), True, ["license_number"]),
        "requires_email": (_("E-Mail"), True, ["email"]),
        "requires_phone": (_("Phone"), False, ["phone_number"]),
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
            for _label, _mandatory, fields in cls.PARTICIPANT_INFO.values()
            for field in fields
        ]

    def participant_field_requirements(self):
        """``{Participant field: is_mandatory}`` for the details this type
        collects. Fields of a setting that's off are absent — the participant
        form hides those."""
        return {
            field: mandatory
            for setting, (_label, mandatory, fields) in self.PARTICIPANT_INFO.items()
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
    is_active = models.BooleanField(default=False, help_text=_("The competition currently being run."))
    assignment_method = models.CharField(
        max_length=20,
        choices=ASSIGNMENT_METHOD_CHOICES,
        default=DEFAULT_ASSIGNMENT_METHOD,
        help_text=_("How participants are assigned to classes."),
    )
    allow_multiple_classes = models.BooleanField(
        default=False,
        help_text=_("Manual assignment only: may a participant be in more than one class."),
    )
    penalties_by_marshal_posts = models.BooleanField(
        default=False,
        help_text=_("Marshal posts enter penalties for their own area, instead of the "
        "timekeeper entering every penalty."),
    )
    start_pattern = models.JSONField(
        default=list,
        blank=True,
        help_text="The order participants take their runs, as pattern blocks "
        "(see apps/competitions/startpattern.py). Replayed for every run. Empty "
        "means no pattern — Auto timing then asks for one and the other screens "
        "carry on without it.",
    )
    auto_timing_order = models.JSONField(
        default=list,
        blank=True,
        help_text="Manual override of the Auto timing start order, as a list of slot "
        "keys. Empty means the computed order (run order × start pattern) is used.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            # There is one active competition for the whole installation, and
            # `get_current()` is `filter(is_active=True).first()` — so with two
            # active rows the app silently serves whichever sorts first and
            # nobody can see the conflict. Every path that sets the flag clears
            # the others first, but "every path does the right thing" is a claim
            # about code, and this is a claim about the data.
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="only_one_active_competition",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.date:%Y-%m-%d})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        # No start pattern by default. A pattern is what Auto timing needs, and
        # Auto timing is a choice — plenty of events are timed on the Manual view
        # with competitors turning up at the line in any order, and neither the
        # dashboard nor the results read the pattern at all (they derive from the
        # entries and their classes). A competition once got a default seeded here
        # so the Auto page wouldn't look broken; the page now says it needs one and
        # links to where to build it, which is the honest version of the same fix.
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
        by list position.

        One query. Callers in a loop pass the result back down rather than asking
        again — see ``starters_by_class``.
        """
        return sorted(
            self.classes.filter(is_running=True),
            key=lambda cc: (
                cc.run_position is None,
                cc.run_position if cc.run_position is not None else 0,
                cc.position,
            ),
        )

    def run_groups(self, running=None):
        """Ordered runs as a list of lists of running CompetitionClass.
        Classes sharing a run_position start together; runs execute in ascending
        run_position. A running class with run_position=None becomes its own
        single-class run, appended in list order after the placed runs.

        ``running`` for a caller that has already read them."""
        placed = {}
        unplaced = []
        for cc in (self._running_classes_ordered() if running is None else running):
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

    def starters_by_class(self, running=None):
        """Map class pk -> the Starters entered in it, in bib order. One Starter
        per entry-in-a-class, so a participant entered into a class twice (or into
        two classes) yields a Starter each time.

        ``running`` for a caller that has already read the running classes."""
        entries = (
            self.entries.select_related("participant")
            .prefetch_related("participant__class_assignments__competition_class")
            .order_by("bib_number")
        )
        # Read once, not once per entry. Age-based assignment resolves a
        # participant's class by walking these, so asking inside the loop cost one
        # query per starter — 114 at 100 starters, on an endpoint every open
        # browser re-fetches on every incoming time.
        if running is None:
            running = self._running_classes_ordered()
        by_class = {}
        repeats = Counter()  # (entry, class) -> Starters already made, to key repeats apart
        for entry in entries:
            for cc in self.classes_for_participant(entry.participant, running=running):
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

    def starters_by_run(self, running=None):
        """``(run, starters)`` for every run in ``run_groups()``. A run's starters
        are those of all its classes merged into one start list ordered by bib —
        classes sharing a run start together, so they interleave."""
        if running is None:
            running = self._running_classes_ordered()
        by_class = self.starters_by_class(running=running)
        runs = []
        for run in self.run_groups(running=running):
            starters = []
            for cc in run:
                starters.extend(by_class.get(cc.pk, []))
            starters.sort(key=lambda starter: (starter.bib, starter.class_name))
            runs.append((run, starters))
        return runs

    def start_lists(self, running=None):
        """``(run, slots)`` for every run: the start pattern played out over each
        run's starters — who starts, in which run type, in order."""
        blocks = self.start_pattern_blocks()
        return [
            (run, startpattern.expand(blocks, starters))
            for run, starters in self.starters_by_run(running=running)
        ]

    def class_for_birth_year(self, birth_year, running=None):
        """The running class whose age range covers this birth year, or None.

        ``running`` lets a caller in a loop hand in the classes it has already
        read. Without it this is one query *per participant* — see
        ``starters_by_class``, which is where that cost was being paid.

        **Age is the competition year minus the birth year** — the age the
        competitor reaches during the season, not their age on the day. That is
        how slalom classes are normally written ("Jahrgang"), so somebody born in
        December is in the same class all year as somebody born in January. It
        has always worked this way and was never written down anywhere.

        With overlapping ranges the first match in class order wins. The Classes
        page says so (see ``age_range_problems``) rather than leaving it to be
        discovered from a competitor landing in the wrong class.
        """
        if birth_year is None:
            return None
        age = self.date.year - birth_year
        for competition_class in (self._running_classes_ordered()
                                  if running is None else running):
            if (
                competition_class.age_from is not None
                and competition_class.age_to is not None
                and competition_class.age_from <= age <= competition_class.age_to
            ):
                return competition_class
        return None

    def age_range_problems(self):
        """Age ranges that two running classes both claim, or that no class does.

        Only meaningful under age-based assignment, where these decide which class
        a competitor lands in — and both failure modes are silent: an overlap puts
        them in whichever class sorts first, a gap leaves them in no class at all
        and simply absent from every start list. Returned as sentences for the
        Classes page; nothing is refused, because a range being edited is
        half-finished more often than it is wrong.
        """
        if not self.assignment().uses_age:
            return []
        ranged = [
            cc for cc in self._running_classes_ordered()
            if cc.age_from is not None and cc.age_to is not None
        ]
        problems = []
        for i, first in enumerate(ranged):
            if first.age_from > first.age_to:
                problems.append(gettext(
                    "“%(name)s” runs from age %(a)s to %(b)s, which is backwards — "
                    "nobody falls in it."
                ) % {"name": first.name, "a": first.age_from, "b": first.age_to})
            for second in ranged[i + 1:]:
                lo = max(first.age_from, second.age_from)
                hi = min(first.age_to, second.age_to)
                if lo <= hi:
                    problems.append(gettext(
                        "“%(first)s” and “%(second)s” both cover age %(range)s — a "
                        "competitor that age goes into “%(first)s”, because it comes "
                        "first in the list."
                    ) % {
                        "first": first.name, "second": second.name,
                        "range": str(lo) if lo == hi else f"{lo}–{hi}",
                    })
        # Gaps between the ranges: an age nobody claims is a competitor with no class.
        covered = sorted((cc.age_from, cc.age_to) for cc in ranged)
        for (_a, end), (start, _b) in zip(covered, covered[1:]):
            if start > end + 1:
                missing = str(end + 1) if start == end + 2 else f"{end + 1}–{start - 1}"
                problems.append(gettext(
                    "No running class covers age %(range)s — a competitor that age "
                    "is assigned no class and appears in no start list."
                ) % {"range": missing})
        return problems

    def assignment(self):
        """The AssignmentMethod strategy for this competition."""
        return get_assignment_method(self.assignment_method)

    def allows_multiple_classes_effective(self):
        """Whether a participant may be in multiple distinct classes, honouring
        both the method (must support it) and the competition's toggle."""
        return self.assignment().configurable_multiple and self.allow_multiple_classes

    def classes_for_participant(self, participant, running=None):
        """Resolve the class(es) a participant belongs to under the current
        assignment method (may repeat for manual multi-entry).

        ``running`` is the running classes, for a caller resolving a whole field:
        without it an age-based competition re-reads them once per participant."""
        return self.assignment().classes_for(self, participant, running=running)

    def assigned_task_numbers(self):
        """Every task number watched by any marshal post, deduplicated and sorted."""
        numbers = set()
        for post in self.marshal_posts.all():
            numbers.update(post.task_numbers())
        return sorted(numbers)

    @classmethod
    def get_current(cls):
        return cls.objects.filter(is_active=True).first()


@lru_cache(maxsize=1)
def _class_words():
    """The word "Class" in every language the app is translated into, lower-cased.
    Language-independent, so it is computed once per process. Used by
    CompetitionClass.name_hint to spot a name that repeats the word the app adds
    itself — a name is typed in the organiser's language and read in whatever
    language the page is being read in, so one language is not enough."""
    words = set()
    for code, _label in settings.LANGUAGES:
        with translation.override(code):
            words.add(gettext("Class").lower())
    return frozenset(words)


class CompetitionClass(models.Model):
    # Starter classes seeded on a brand-new competition; fully editable afterwards.
    DEFAULT_NAMES = ["1", "2", "3", "4", "5", "6", "E"]

    class Scoring(models.TextChoices):
        """How a class's counted runs turn into a result. Recorded per class here;
        the calculation itself lives with the results feature."""

        AGGREGATE = "aggregate", _("Aggregate times")
        BEST_RUN = "best_run", _("Best run only")
        REGULARITY = "regularity", _("Regularity test")

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="classes")
    name = models.CharField(max_length=50)
    position = models.PositiveIntegerField(default=0, help_text=_("Display order in the classes list."))
    is_running = models.BooleanField(default=False)
    age_from = models.PositiveIntegerField(null=True, blank=True, help_text=_("Starting age, e.g. 6"))
    age_to = models.PositiveIntegerField(null=True, blank=True, help_text=_("Ending age, e.g. 7"))
    practice_runs = models.PositiveIntegerField(default=1)
    counted_runs = models.PositiveIntegerField(default=2)
    scoring_method = models.CharField(
        max_length=20,
        choices=Scoring.choices,
        default=Scoring.AGGREGATE,
        help_text=_("How this class's counted runs are turned into a result."),
    )
    allow_multiple_entries = models.BooleanField(
        default=False,
        help_text=_("Manual assignment only: may a participant be entered into this class more than once."),
    )
    run_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("The run this class belongs to. Classes sharing a run_position start "
        "together; runs execute in ascending order. Null when not placed."),
    )

    class Meta:
        ordering = ["position", "name"]
        unique_together = ("competition", "name")

    def __str__(self):
        return f"{self.competition} – {self.name}"

    def display_name(self):
        """The class as it is written where it has to be named as a class — the
        results index tiles, every results table heading, the sidebar's Results
        sub-list and every exported PDF.

        Classes are usually named just "1" or "E", so the word is prefixed —
        always, without inspecting the name. It used to be conditional: a name
        that already opened with the word (in *any* shipped language, since a
        name is typed in the organiser's language and read in the reader's) was
        shown alone. That made the heading depend on how somebody had typed a
        name, which is the kind of rule nobody can see working and everybody
        sees failing.

        So the division is: the organiser owns the name, the app owns the word.
        A class meant to read "Klasse 3" is named "3" — and `name_hint()` says
        so on the Classes page for a name that repeats the word, rather than
        this method quietly papering over it.
        """
        return gettext("Class %(name)s") % {"name": self.name.strip()}

    def name_hint(self):
        """Why this class's name will read oddly — or "" when it won't.

        The word is added by `display_name()`, so a class *named* "Klasse 3"
        comes out as "Klasse Klasse 3" on every heading and PDF. It is a legal
        name and might be deliberate, so this is a hint on the Classes page
        rather than a refusal — the same shape as `scoring_warning()`.
        """
        name = self.name.strip()
        lowered = name.lower()
        for word in _class_words():
            if not lowered.startswith(word):
                continue
            # What the name would be with the word taken off the front; the
            # separators are what people put between it and the number.
            suggested = name[len(word):].strip(" -–—:.") or name
            return gettext(
                "The word is added automatically, so this reads “%(shown)s”. "
                "Name the class “%(suggested)s”."
            ) % {"shown": self.display_name(), "suggested": suggested}
        return ""

    def scoring_warning(self):
        """Why this class, as configured, can't produce a ranking — or "" when it
        can. Both combinations below are legal to set up and silently wrong
        afterwards, which is the whole problem: nothing refuses them and the
        results table simply comes out empty or meaningless.

        * No counted runs: ``compute_class_results`` can never call anybody
          complete, so the class ranks nobody, for ever.
        * A regularity test over a single counted run: the score is max − min of
          one number, so every competitor scores exactly 0, the whole field ties
          and the tie-break orders them by fastest run — the class is quietly
          scored as "best run" instead of as a regularity test.

        Only a running class produces results, so only a running class is flagged.
        """
        if not self.is_running:
            return ""
        counted = self.counted_runs or 0
        if counted == 0:
            return _("No counted runs, so nobody in this class can be ranked.")
        if self.scoring_method == self.Scoring.REGULARITY and counted == 1:
            return _(
                "A regularity test needs at least two counted runs — over a single "
                "run every competitor scores 0 and the class is ranked by fastest run."
            )
        return ""

    def birth_year_range(self):
        """Birth years spanned by this class's age range, oldest (from age_to)
        first. Deliberately not reordered by magnitude: age_to always drives
        the first value and age_from always drives the second, so changing
        just one age field only ever moves the number it corresponds to."""
        if self.age_from is None or self.age_to is None:
            return None
        year = self.competition.date.year
        return (year - self.age_to, year - self.age_from)


class MarshalPost(models.Model):
    """One marshal post of a competition that enters penalties for its own area.
    Only present when Competition.penalties_by_marshal_posts is on. Numbered 1..n
    within a competition; each watches a set of numbered tasks and at most one
    post is responsible for the stop line. The tasks are stored as the raw spec
    the operator typed (see taskspec) so it round-trips exactly."""

    competition = models.ForeignKey(
        Competition, on_delete=models.CASCADE, related_name="marshal_posts"
    )
    number = models.PositiveSmallIntegerField(help_text=_("1-based post number, in setup order."))
    tasks = models.CharField(
        max_length=200,
        blank=True,
        help_text=_("Task numbers this post watches, e.g. “1, 5, 11-15”."),
    )
    handles_stop_line = models.BooleanField(
        default=False,
        help_text=_("This post also judges the stop line (at most one post per competition)."),
    )
    # A soft claim so only one device edits a post at a time: the token of the
    # device that confirmed it on the Marshal Posts page, kept alive by a
    # heartbeat (claim_seen). A claim older than CLAIM_TTL is treated as free.
    claim_token = models.CharField(max_length=64, blank=True)
    claim_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["number"]
        unique_together = ("competition", "number")

    def __str__(self):
        return f"{self.competition} – Marshal Post {self.number}"

    def claimed_by_other(self, token, now):
        """Whether a *different, still-live* device holds this post."""
        from datetime import timedelta

        if not self.claim_token or self.claim_token == token:
            return False
        return self.claim_seen is not None and (now - self.claim_seen) < timedelta(seconds=CLAIM_TTL)

    def task_numbers(self):
        """The task numbers this post watches, sorted. Malformed stored specs
        (shouldn't happen — the form validates) yield an empty list."""
        try:
            return taskspec.parse(self.tasks)
        except taskspec.TaskSpecError:
            return []
