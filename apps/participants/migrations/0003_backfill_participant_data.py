import datetime

from django.db import migrations


def backfill(apps, schema_editor):
    CompetitionType = apps.get_model("competitions", "CompetitionType")
    Competition = apps.get_model("competitions", "Competition")
    Participant = apps.get_model("participants", "Participant")
    EventEntry = apps.get_model("participants", "EventEntry")

    general, _ = CompetitionType.objects.get_or_create(name="General")
    Participant.objects.filter(competition_type__isnull=True).update(competition_type=general)

    for participant in Participant.objects.filter(year_of_birth__isnull=False, date_of_birth__isnull=True):
        participant.date_of_birth = datetime.date(participant.year_of_birth, 1, 1)
        participant.save(update_fields=["date_of_birth"])

    target_competition = (
        Competition.objects.filter(is_active=True).first()
        or Competition.objects.order_by("-date").first()
    )
    if target_competition is not None:
        for participant in Participant.objects.filter(bib_number__isnull=False):
            EventEntry.objects.get_or_create(
                participant=participant,
                competition=target_competition,
                defaults={"bib_number": participant.bib_number, "status": participant.status},
            )


def unbackfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('participants', '0002_add_types_and_events'),
        ('competitions', '0007_backfill_competition_type'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
