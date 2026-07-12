from django.db import models


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
