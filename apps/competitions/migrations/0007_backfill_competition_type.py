from django.db import migrations


def backfill(apps, schema_editor):
    CompetitionType = apps.get_model("competitions", "CompetitionType")
    Competition = apps.get_model("competitions", "Competition")
    general, _ = CompetitionType.objects.get_or_create(name="General")
    Competition.objects.filter(competition_type__isnull=True).update(competition_type=general)


def unbackfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('competitions', '0006_add_types_and_events'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
