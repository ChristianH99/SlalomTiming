from django.core.validators import MaxValueValidator
from django.db import models


class TimingSettings(models.Model):
    """The local timing setup for this machine: which device receives signals,
    how its channels map to start/finish, and (for a networked device) where to
    reach it. A single row — there is one timing rig per installation."""

    class Device(models.TextChoices):
        TP540 = "tp540", "Tag Heuer TP540"
        SIMULATOR = "simulator", "Simulator"

    device = models.CharField(
        max_length=20, choices=Device.choices, default=Device.SIMULATOR,
        help_text="Which timing device signals are received from.",
    )
    # Single-digit device channels (0–9): which physical channel carries the
    # start pulse and which carries the finish pulse.
    start_channel = models.PositiveSmallIntegerField(
        default=1, validators=[MaxValueValidator(9)],
        help_text="Device channel that carries the start signal (0–9).",
    )
    finish_channel = models.PositiveSmallIntegerField(
        default=2, validators=[MaxValueValidator(9)],
        help_text="Device channel that carries the finish signal (0–9).",
    )
    # Only meaningful for a networked device (the TP540); blank for the simulator.
    ip_address = models.GenericIPAddressField(
        null=True, blank=True,
        help_text="Network address of the Tag Heuer TP540.",
    )

    class Meta:
        verbose_name = "timing settings"
        verbose_name_plural = "timing settings"

    def __str__(self):
        return f"Timing settings ({self.get_device_display()})"

    def save(self, *args, **kwargs):
        # Pin to a single row: there is only ever one timing rig.
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The one settings row, created with defaults on first access."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class TimingSignal(models.Model):
    """A raw signal as it arrives from a timing device (real or simulated): a
    running number, the channel/port it came in on, and the wall-clock time it
    fired. This is the timing system's inbox; the live timing view arranges these
    into runs (see apps/timing/arrangement.py)."""

    class Role(models.TextChoices):
        START = "start", "Start"
        FINISH = "finish", "Finish"

    # Which competition was being timed when this fired (stamped at ingestion);
    # the live view is scoped to it. Null when no competition was active.
    competition = models.ForeignKey(
        "competitions.Competition", null=True, blank=True,
        on_delete=models.CASCADE, related_name="timing_signals",
    )
    running_number = models.PositiveIntegerField()
    port = models.PositiveSmallIntegerField(help_text="Physical channel the signal came in on (1–4).")
    # A manual trigger (the simulator's M-buttons, a hand button on the device)
    # fires on the same port as its light barrier but is flagged so downstream
    # code can tell an operator press from an automatic beam break.
    is_manual = models.BooleanField(default=False)
    device_time = models.TimeField(help_text="Wall-clock time the signal fired (hh:mm:ss.mmm).")
    source = models.CharField(max_length=20, default="simulator")
    # A wrong measurement (someone walked through a beam): kept, but removed from
    # its run and listed separately, greyed out.
    ignored = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        port = f"M{self.port}" if self.is_manual else str(self.port)
        return f"#{self.running_number} port {port} @ {self.device_time}"

    def role(self, settings):
        """Whether this signal is a start, a finish, or neither, per the timing
        rig's channel mapping."""
        if self.port == settings.start_channel:
            return self.Role.START
        if self.port == settings.finish_channel:
            return self.Role.FINISH
        return None


class TimedRun(models.Model):
    """One run: a start time paired with a finish time, plus what the operator
    assigns to it (bib, class, run, penalties). Either signal may be blank — an
    open run waiting for its finish, or a finish whose start hasn't been paired
    yet. Each signal is referenced by at most one run (OneToOne), so a time can
    never be used twice. Pairing is causal (arrangement.py): a finish only ever
    joins a start that came before it."""

    class RunType(models.TextChoices):
        PRACTICE = "practice", "Practice"
        COUNTED = "counted", "Counted"

    competition = models.ForeignKey(
        "competitions.Competition", on_delete=models.CASCADE, related_name="timed_runs"
    )
    start_signal = models.OneToOneField(
        TimingSignal, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    finish_signal = models.OneToOneField(
        TimingSignal, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    bib_number = models.PositiveIntegerField(null=True, blank=True)
    competition_class = models.ForeignKey(
        "competitions.CompetitionClass", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    # Which entry-in-a-class this run belongs to (0-based): a participant entered
    # into the same class more than once runs it that many times, each occurrence
    # tracked separately.
    class_occurrence = models.PositiveSmallIntegerField(default=0)
    run_type = models.CharField(max_length=10, choices=RunType.choices, blank=True)
    run_number = models.PositiveIntegerField(null=True, blank=True)
    pylon_count = models.PositiveSmallIntegerField(default=0)
    task_count = models.PositiveSmallIntegerField(default=0)
    stopline_count = models.PositiveSmallIntegerField(default=0)
    # Auto timing only: the timekeeper's manual +/- to the total pylon/task counts
    # (signed — can pull the marshal-post totals up or down). Applied on top of the
    # summed MarshalPenalty counts; the grand total is clamped at zero.
    pylon_adjust = models.IntegerField(default=0)
    task_adjust = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        anchor = self.start_signal or self.finish_signal
        return f"Run {anchor} (bib {self.bib_number})"


class MarshalPenalty(models.Model):
    """A marshal post's penalty entry for one run: the aggregate counts the post
    recorded for the competitor it was judging, plus whether the marshal has
    submitted (finalised) them. One row per (run, post). Written from the Marshal
    Posts page and shown on the Auto timing view as a per-post box that fills as
    the marshal taps and turns green once submitted."""

    timed_run = models.ForeignKey(
        TimedRun, on_delete=models.CASCADE, related_name="marshal_penalties"
    )
    marshal_post = models.ForeignKey(
        "competitions.MarshalPost", on_delete=models.CASCADE, related_name="penalties"
    )
    # Aggregated across the post's tasks: total pylon hits, number of tasks failed,
    # and whether the stop line was missed (0/1) — mirroring TimedRun's counts.
    pylon_count = models.PositiveSmallIntegerField(default=0)
    task_count = models.PositiveSmallIntegerField(default=0)
    stopline_count = models.PositiveSmallIntegerField(default=0)
    # The per-task breakdown the timekeeper's pop-up shows and the marshal resumes
    # from after an unlock: {"tasks": {"3": {"pylons": 2}, "5": {"task": true}},
    # "stop_line": true}. The counts above are its aggregate.
    detail = models.JSONField(default=dict, blank=True)
    # Submitted == locked: the marshal can't edit until a timekeeper unlocks it.
    submitted = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("timed_run", "marshal_post")

    def __str__(self):
        return f"Post {self.marshal_post.number} on run {self.timed_run_id}"


class TimingEvent(models.Model):
    class Channel(models.TextChoices):
        START = "start", "Start"
        FINISH = "finish", "Finish"
        INTERMEDIATE = "intermediate", "Intermediate"

    channel = models.CharField(max_length=20, choices=Channel.choices)
    bib_number = models.PositiveIntegerField(null=True, blank=True)
    device_time = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    connector = models.CharField(max_length=200)
    raw_payload = models.JSONField(null=True, blank=True)
    participant = models.ForeignKey(
        "participants.Participant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="timing_events",
    )

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["bib_number"])]

    def __str__(self):
        bib = self.bib_number if self.bib_number is not None else "?"
        return f"{self.channel} bib {bib} @ {self.received_at:%H:%M:%S}"
