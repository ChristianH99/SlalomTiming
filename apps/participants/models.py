import datetime

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.competitions.models import Competition, CompetitionClass, CompetitionType

# The oldest date of birth this app will take. Not a judgement about anybody’s
# age — a bound, so a slipped keystroke in a date field is refused instead of
# stored. A year-1200 birth date sailed straight in and then gave every age-based
# class a nonsense age.
EARLIEST_BIRTH_YEAR = 1900


def validate_birth_date(value):
    """A date of birth that could belong to a living competitor.

    A validator on the model rather than a rule in the form, so it also covers
    the CSV import and the transfer document — apps/transfer/schema.py runs each
    field’s own validators on the way in, which is what turns a damaged file into
    a sentence instead of a row every later read chokes on.
    """
    if value is None:
        return
    if value > timezone.localdate():
        raise ValidationError(
            _("A date of birth can’t be in the future."), code="future_birth_date")
    if value.year < EARLIEST_BIRTH_YEAR:
        raise ValidationError(
            _("A date of birth before %(year)s is a typo — please check it."),
            code="ancient_birth_date", params={"year": EARLIEST_BIRTH_YEAR},
        )


class Participant(models.Model):
    competition_type = models.ForeignKey(
        CompetitionType,
        on_delete=models.PROTECT,
        related_name="participants",
        help_text=_("Discipline this participant is registered under (e.g. Motorcycle, Go-Cart). "
        "A participant can only ever belong to one type."),
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField(validators=[validate_birth_date])

    # Everything below is optional at the DB level: which of these a participant
    # must supply is decided per discipline by CompetitionType.PARTICIPANT_INFO,
    # and enforced by the participant form, not here. A type that stops
    # collecting a detail leaves any value already recorded untouched.
    license_number = models.CharField(max_length=50, blank=True)
    co_driver_first_name = models.CharField(max_length=100, blank=True)
    co_driver_last_name = models.CharField(max_length=100, blank=True)
    vehicle = models.CharField(max_length=150, blank=True)
    address_street = models.CharField(max_length=200, blank=True)
    address_zip_code = models.CharField(max_length=20, blank=True)
    address_city = models.CharField(max_length=100, blank=True)
    club = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone_number = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When this record was last edited *or* entered into a competition. Not the
    # same question as updated_at, which only moves when the row itself is
    # written: a competitor who has raced every year since 2019 and never
    # changed their address has an updated_at of 2019 and is not stale at all.
    # A personal record kept because it might be needed again stops being kept
    # for that reason once it stops being used, so this is the field a retention
    # sweep has to age off — indexed because that sweep asks the whole table.
    last_used_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    def save(self, *args, **kwargs):
        """Every write is a use — an edit, an import, a merge.

        The other half (a bib assigned, in a competition) can't go here: an entry
        is a different row, and is written without touching this one. It comes
        through touch_last_used() below.
        """
        self.last_used_at = timezone.now()
        if (fields := kwargs.get("update_fields")) is not None:
            kwargs["update_fields"] = {*fields, "last_used_at"}
        super().save(*args, **kwargs)


class ClassAssignment(models.Model):
    """A manual assignment of a participant to a competition class. This is an
    explicit join model (not a M2M) so the same participant can be entered into
    the same class more than once when the class allows repeat entries — a
    set-based M2M could not represent that. Which competition the assignment
    belongs to is implied by ``competition_class.competition``."""

    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="class_assignments"
    )
    competition_class = models.ForeignKey(
        CompetitionClass, on_delete=models.CASCADE, related_name="assignments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.participant} → {self.competition_class}"


class EventEntry(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "registered", _("Registered")
        DNS = "dns", _("Did Not Start")
        DNF = "dnf", _("Did Not Finish")
        DSQ = "dsq", _("Disqualified")
        FINISHED = "finished", _("Finished")

    participant = models.ForeignKey(Participant, on_delete=models.CASCADE, related_name="entries")
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="entries")
    bib_number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REGISTERED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["bib_number"]
        constraints = [
            models.UniqueConstraint(fields=["competition", "bib_number"], name="unique_bib_per_competition"),
            models.UniqueConstraint(fields=["competition", "participant"], name="unique_participant_per_competition"),
        ]

    def __str__(self):
        return f"#{self.bib_number} {self.participant} @ {self.competition}"


@receiver(post_save, sender=EventEntry)
def touch_last_used(sender, instance, **kwargs):
    """Being given a bib is the loudest possible "this record is still in use".

    A queryset update rather than participant.save(): this is not an edit of the
    participant, so it must not move updated_at, and it must not fire whatever
    else a save might come to do. post_save rather than an override of
    EventEntry.save() so it covers create(), get_or_create() and the CSV and
    archive imports without each of them having to remember.
    """
    Participant.objects.filter(pk=instance.participant_id).update(
        last_used_at=timezone.now())
