from django.db import migrations

OLD_BOOLEAN_FIELDS = [
    ("class_1", "1"),
    ("class_2", "2"),
    ("class_3", "3"),
    ("class_4", "4"),
    ("class_5", "5"),
    ("class_6", "6"),
    ("class_e", "E"),
]


def backfill(apps, schema_editor):
    Competition = apps.get_model("competitions", "Competition")
    CompetitionClass = apps.get_model("competitions", "CompetitionClass")
    for competition in Competition.objects.all():
        CompetitionClass.objects.bulk_create(
            CompetitionClass(
                competition=competition,
                code=code,
                is_running=getattr(competition, field_name),
            )
            for field_name, code in OLD_BOOLEAN_FIELDS
        )


def unbackfill(apps, schema_editor):
    CompetitionClass = apps.get_model("competitions", "CompetitionClass")
    CompetitionClass.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('competitions', '0003_split_classes'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
