from pathlib import Path

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class BackupSettings(models.Model):
    """Where the database is copied, and how often.

    The run-book used to tell the operator to take a snapshot between runs by
    hand — ``VACUUM INTO`` on a command line, then copy it to a USB stick. That
    was the app's only backup, and it asked the person who is timing a race to
    remember something every few minutes. It also sat awkwardly beside the rule
    that the database is not to be carried about: a copy made deliberately, to a
    place the operator chose, is a different thing from one they were told to make
    and then had nowhere to put.

    So the app does it: a destination, an interval, and a background thread (see
    backup.py). One row — there is one database to copy.
    """

    # A minute is the shortest that makes sense (a copy takes a moment and the
    # rig must not queue behind it); ten is about as long as anyone wants to lose.
    MIN_INTERVAL, MAX_INTERVAL = 1, 10
    # How many snapshots to keep at the destination. At one a minute over an
    # eight-hour event that would otherwise be 480 copies of a growing database —
    # a full USB stick, and the newest copy is the one that failed to write.
    MIN_KEEP, MAX_KEEP = 2, 100

    enabled = models.BooleanField(
        default=False,
        help_text=_("Copy the database to the destination on a timer."),
    )
    destination = models.CharField(
        max_length=500, blank=True,
        verbose_name=_("Destination folder"),
        help_text=_("Where the copies go — a USB stick or another drive, so a "
                    "copy survives whatever happens to this computer."),
    )
    interval_minutes = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(MIN_INTERVAL), MaxValueValidator(MAX_INTERVAL)],
        verbose_name=_("Every"),
        help_text=_("Minutes between copies (1–10)."),
    )
    keep = models.PositiveSmallIntegerField(
        default=12,
        validators=[MinValueValidator(MIN_KEEP), MaxValueValidator(MAX_KEEP)],
        verbose_name=_("Keep"),
        help_text=_("How many copies to keep. Older ones are deleted."),
    )

    # What happened last time, persisted rather than held in the thread: "when was
    # the last good copy" is the question this page exists to answer, and it has to
    # survive a restart to be worth anything.
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    last_file = models.CharField(max_length=500, blank=True)
    last_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "backup settings"
        verbose_name_plural = "backup settings"

    def __str__(self):
        return f"Backup ({'on' if self.enabled else 'off'})"

    def save(self, *args, **kwargs):
        self.pk = 1          # one database, one destination
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def destination_path(self):
        return Path(self.destination) if self.destination else None

    def destination_problem(self):
        """Why this destination can't be used, or "" when it can.

        Checked when it is saved *and* before every copy: a USB stick is unplugged
        far more often than a setting is changed, and a backup that has quietly
        been failing since lunchtime is worse than no backup, because nobody is
        looking for the fault.
        """
        path = self.destination_path
        if path is None:
            return str(_("No destination folder is set."))
        if not path.exists():
            return str(_("“%(path)s” does not exist — is the drive plugged in?")
                       % {"path": path})
        if not path.is_dir():
            return str(_("“%(path)s” is a file, not a folder.") % {"path": path})
        probe = path / ".slalomtiming-write-test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            return str(_("“%(path)s” can’t be written to (%(error)s).")
                       % {"path": path, "error": exc.strerror or exc})
        return ""
