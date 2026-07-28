from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class TimingSettings(models.Model):
    """The local timing setup for this machine: which device receives signals,
    how its channels map to start/finish, and (for a networked device) where to
    reach it. A single row — there is one timing rig per installation."""

    class Device(models.TextChoices):
        CP540 = "cp540", "Tag Heuer CP540"
        SIMULATOR = "simulator", _("Simulator")

    device = models.CharField(
        max_length=20, choices=Device.choices, default=Device.SIMULATOR,
        verbose_name=_("Device"),
        help_text=_("Which timing device signals are received from."),
    )
    # Which physical channel carries the start pulse and which the finish. Every
    # supported device (and the simulator) has inputs 1–4, so anything outside
    # that can never match a signal: a finish channel of 7 silently meant no
    # signal was ever a finish, with the setting sitting there looking valid.
    # Both may name the *same* channel — that is a single light barrier doing
    # both jobs (see arrangement.effective_role).
    MIN_CHANNEL, MAX_CHANNEL = 1, 4
    _channel = {"validators": [MinValueValidator(MIN_CHANNEL), MaxValueValidator(MAX_CHANNEL)]}

    start_channel = models.PositiveSmallIntegerField(
        default=1, **_channel,
        help_text=_("Device channel that carries the start signal (1–4)."),
    )
    finish_channel = models.PositiveSmallIntegerField(
        default=2, **_channel,
        help_text=_("Device channel that carries the finish signal (1–4)."),
    )
    # Where to reach the CP540. Kept even while another device is selected, so the
    # address doesn't have to be re-typed when switching back (default 192.168.1.50).
    ip_address = models.GenericIPAddressField(
        null=True, blank=True, default="192.168.1.50",
        verbose_name=_("IP address"),
        help_text=_("Network address of the Tag Heuer CP540."),
    )
    # TCP port the CP540 streams its time lines on (default 7000).
    port = models.PositiveIntegerField(
        null=True, blank=True, default=7000, validators=[MaxValueValidator(65535)],
        verbose_name=_("Port"),
        help_text=_("TCP port of the Tag Heuer CP540."),
    )
    # Operator "lock" toggled from the timing pages: while on, every incoming time
    # (from any device or the simulator) is sent straight to the ignore list rather
    # than into a run — a way to pause capture without disconnecting. Off = normal.
    ignore_incoming = models.BooleanField(default=False)
    # Whether the operator left the device connected. The reader thread is process
    # state, so a restart used to leave the CP540 selected and nothing reading it,
    # with nobody told — this is the bit that survives, and config/asgi.py brings
    # the reader back up from it (cp540.autostart). Cleared by Disconnect and by
    # selecting any other device.
    reader_enabled = models.BooleanField(default=False)

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
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class TimingSignal(models.Model):
    """A raw signal as it arrives from a timing device (real or simulated): a
    running number, the channel/port it came in on, and the wall-clock time it
    fired. This is the timing system's inbox; the live timing view arranges these
    into runs (see apps/timing/arrangement.py)."""

    class Role(models.TextChoices):
        START = "start", _("Start")
        FINISH = "finish", _("Finish")

    # Which competition was being timed when this fired (stamped at ingestion);
    # the live view is scoped to it. Null when no competition was active.
    competition = models.ForeignKey(
        "competitions.Competition", null=True, blank=True,
        on_delete=models.CASCADE, related_name="timing_signals",
    )
    running_number = models.PositiveIntegerField()
    port = models.PositiveSmallIntegerField(help_text=_("Physical channel the signal came in on (1–4)."))
    # A manual trigger (the simulator's M-buttons, a hand button on the device)
    # fires on the same port as its light barrier but is flagged so downstream
    # code can tell an operator press from an automatic beam break.
    is_manual = models.BooleanField(default=False)
    device_time = models.TimeField(help_text=_("Wall-clock time the signal fired (hh:mm:ss.mmm)."))
    source = models.CharField(max_length=20, default="simulator")
    # A wrong measurement (someone walked through a beam): kept, but removed from
    # its run and listed separately, greyed out.
    ignored = models.BooleanField(default=False)
    # Operator typed this time in by hand (device failed), rather than it arriving
    # from a device/simulator. Shown with a distinct highlight so a keyed-in time
    # is never mistaken for a measured one. Distinct from is_manual, which marks a
    # manual *trigger* (hand button) that the device still measured.
    entered = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            # The Ignored-times panel, re-read by both timing pages on every
            # nudge: filter(competition, ignored).order_by("-received_at").
            models.Index(
                fields=["competition", "ignored", "-received_at"],
                name="timingsignal_comp_ign_idx",
            ),
        ]

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
        PRACTICE = "practice", _("Practice")
        COUNTED = "counted", _("Counted")

    class Status(models.TextChoices):
        """How a run ended when it did not end in a time.

        A status *replaces* the run's time in the result — a run carrying one is
        never scored, whatever times happen to sit on it (a disqualified run is
        usually a measured one). Blank means the ordinary case: the run is either
        timed or still to come. Which of these makes a competitor DNS/DNC/DSQ
        overall is the class's business, not the run's — see
        apps/results/resultscalc.py.
        """

        DNF = "dnf", _("Did Not Finish")
        DNC = "dnc", _("Did Not Classify")
        DNS = "dns", _("Did Not Start")
        DSQ = "dsq", _("Disqualified")

    competition = models.ForeignKey(
        "competitions.Competition", on_delete=models.CASCADE, related_name="timed_runs"
    )
    # RESTRICT, not SET_NULL: deleting a TimingSignal used to silently blank the
    # run's time — the row stayed, the measurement vanished, and nothing said so.
    # For an app whose rule is "this app does not delete recorded times" that is
    # the wrong default. RESTRICT rather than PROTECT because deleting a whole
    # *competition* cascades to both tables at once, and that must still work;
    # RESTRICT allows exactly that case and refuses the lone delete.
    start_signal = models.OneToOneField(
        TimingSignal, null=True, blank=True, on_delete=models.RESTRICT, related_name="+"
    )
    finish_signal = models.OneToOneField(
        TimingSignal, null=True, blank=True, on_delete=models.RESTRICT, related_name="+"
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
    # The run ended in a state code rather than a time (DNF/DNC/DNS/DSQ), set by
    # the timekeeper on either timing view or — for DNS — from the results table.
    # A run carrying one is never scored; blank is the ordinary case.
    status = models.CharField(max_length=3, choices=Status.choices, blank=True)
    # The operator owns this run's identity (entered/edited it on the Manual timing
    # view). Such a run claims its matching slot in the Auto timing order and is
    # skipped by the positional binding, instead of being an auto-bound row whose
    # identity is (re)derived from the start order.
    manual_entry = models.BooleanField(default=False)
    # Operator-typed run time (seconds) used when the device gave no usable
    # start/finish pair; overrides the computed elapsed time. Null = compute it.
    manual_run_time = models.DecimalField(
        max_digits=9, decimal_places=3, null=True, blank=True
    )
    pylon_count = models.PositiveSmallIntegerField(default=0)
    task_count = models.PositiveSmallIntegerField(default=0)
    stopline_count = models.PositiveSmallIntegerField(default=0)
    # Auto timing (marshal mode only): the timekeeper's manual +/- to the total
    # pylon/task/stop-line counts (signed — can pull the marshal-post totals up or
    # down). Applied on top of the summed MarshalPenalty counts; the grand total is
    # clamped at zero. In non-marshal mode (or on an operator-owned run) the Auto
    # steppers edit the run's own counts directly, so these stay zero.
    pylon_adjust = models.IntegerField(default=0)
    task_adjust = models.IntegerField(default=0)
    stopline_adjust = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Every incoming signal scans this table before it can be placed:
            # arrangement.ingest looks for the oldest empty placeholder and
            # _oldest_open_run/effective_role for a run still waiting on its
            # finish. That happens on the timing rig's own thread while browsers
            # are reading, so it is the one lookup worth an index even at club
            # field sizes.
            models.Index(
                fields=["competition", "start_signal", "finish_signal"],
                name="timedrun_comp_slots_idx",
            ),
            # Resolving a competitor's run by identity (a start-order slot, a
            # run already recorded for a bib).
            models.Index(
                fields=["competition", "bib_number", "competition_class",
                        "class_occurrence", "run_type"],
                name="timedrun_comp_identity_idx",
            ),
        ]

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
        START = "start", _("Start")
        FINISH = "finish", _("Finish")
        INTERMEDIATE = "intermediate", _("Intermediate")

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
