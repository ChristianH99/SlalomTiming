from django.db import models

from apps.competitions.models import Competition, CompetitionType


class Participant(models.Model):
    competition_type = models.ForeignKey(
        CompetitionType,
        on_delete=models.PROTECT,
        related_name="participants",
        help_text="Discipline this participant is registered under (e.g. Motorcycle, Go-Cart). "
        "A participant can only ever belong to one type.",
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
    address_street = models.CharField(max_length=200)
    address_zip_code = models.CharField(max_length=20)
    address_city = models.CharField(max_length=100)
    club = models.CharField(max_length=150)
    license_number = models.CharField(max_length=50)
    email = models.EmailField()
    phone_number = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class EventEntry(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Registered"
        DNS = "dns", "Did Not Start"
        DNF = "dnf", "Did Not Finish"
        DSQ = "dsq", "Disqualified"
        FINISHED = "finished", "Finished"

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
