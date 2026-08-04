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
    # Every field a form renders carries a verbose_name. Without one Django
    # builds a label from the attribute name — in English, with nothing for the
    # catalogue to translate — which is why the participant form used to show
    # German page furniture around "First name", "Last name", "Date of birth".
    first_name = models.CharField(_("First name"), max_length=100)
    last_name = models.CharField(_("Last name"), max_length=100)
    date_of_birth = models.DateField(_("Date of birth"),
                                     validators=[validate_birth_date])

    # Everything below is optional at the DB level: which of these a participant
    # must supply is decided per discipline by CompetitionType.PARTICIPANT_INFO,
    # and enforced by the participant form, not here. A type that stops
    # collecting a detail leaves any value already recorded untouched.
    license_number = models.CharField(_("Licence number"), max_length=50, blank=True)
    co_driver_first_name = models.CharField(_("Co-driver first name"),
                                            max_length=100, blank=True)
    co_driver_last_name = models.CharField(_("Co-driver last name"),
                                           max_length=100, blank=True)
    vehicle = models.CharField(_("Vehicle"), max_length=150, blank=True)
    address_street = models.CharField(_("Street address"), max_length=200, blank=True)
    address_zip_code = models.CharField(_("Post code"), max_length=20, blank=True)
    address_city = models.CharField(_("City"), max_length=100, blank=True)
    club = models.CharField(_("Club"), max_length=150, blank=True)
    email = models.EmailField(_("E-Mail"), blank=True)
    phone_number = models.CharField(_("Phone number"), max_length=30, blank=True)
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


class DrawNumber(models.Model):
    """The number a participant drew at the registration desk, before anybody
    knows what bib they will wear.

    Deliberately **not** a field on ``EventEntry``. An entry *is* a bib — the
    column is not nullable and clearing a bib deletes the row — and the whole
    app reads it that way: the start lists, the results, the timing views and
    ``archiving.starter_snapshot`` all take "has an entry" to mean "has a number
    on the day". Making the bib nullable so a drawn-but-unassigned competitor
    could live in the same table would put a ``None`` bib into every one of
    those readers, for a state that lasts until the draw closes.

    So a drawn number is its own row, and it says exactly what it is: this
    person is registered for this event and is waiting for a bib. They become a
    starter at the moment ``apps/participants/draw.py`` gives them one — which
    is the same moment they would have become one by being typed a bib by hand.

    Both numbers are unique within the competition. The drawn one because two
    people holding ticket 14 is the argument the draw exists to prevent; the bib
    because it always was (``unique_bib_per_competition``).
    """

    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="draw_numbers"
    )
    competition = models.ForeignKey(
        Competition, on_delete=models.CASCADE, related_name="draw_numbers"
    )
    number = models.PositiveIntegerField(_("Draw number"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "number"], name="unique_draw_number_per_competition"
            ),
            models.UniqueConstraint(
                fields=["competition", "participant"],
                name="unique_draw_participant_per_competition",
            ),
        ]

    def __str__(self):
        return f"draw {self.number} – {self.participant} @ {self.competition}"


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
