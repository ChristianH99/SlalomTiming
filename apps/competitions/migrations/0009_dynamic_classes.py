from django.db import migrations, models


def set_positions(apps, schema_editor):
    """Give existing classes a stable list order (by their current name, which
    is the old code: 1..6 then E) so ordering survives the switch to positional
    ordering."""
    CompetitionClass = apps.get_model("competitions", "CompetitionClass")
    Competition = apps.get_model("competitions", "Competition")
    for competition in Competition.objects.all():
        classes = competition.classes.order_by("name")
        for position, cc in enumerate(classes):
            cc.position = position
            cc.save(update_fields=["position"])


class Migration(migrations.Migration):

    dependencies = [
        ("competitions", "0008_require_competition_type"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="competitionclass",
            unique_together=set(),
        ),
        migrations.RenameField(
            model_name="competitionclass",
            old_name="code",
            new_name="name",
        ),
        migrations.AlterField(
            model_name="competitionclass",
            name="name",
            field=models.CharField(max_length=50),
        ),
        migrations.AddField(
            model_name="competitionclass",
            name="position",
            field=models.PositiveIntegerField(
                default=0, help_text="Display order in the classes list."
            ),
        ),
        migrations.AddField(
            model_name="competitionclass",
            name="practice_runs",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="competitionclass",
            name="counted_runs",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="competitionclass",
            name="run_position",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                help_text="The run this class belongs to. Classes sharing a run_position start "
                "together; runs execute in ascending order. Null when not placed.",
            ),
        ),
        migrations.AlterModelOptions(
            name="competitionclass",
            options={"ordering": ["position", "name"]},
        ),
        migrations.AlterUniqueTogether(
            name="competitionclass",
            unique_together={("competition", "name")},
        ),
        migrations.RunPython(set_positions, migrations.RunPython.noop),
    ]
