"""When a participant record was last edited or entered into a competition.

The field is new, so every existing row would come out null — and a retention
sweep reads null as "never used", which is exactly backwards for a club's
existing members. So it is filled in from what the database already knows: the
later of the participant's own updated_at and the most recent entry written for
them.
"""

from django.db import migrations, models
from django.db.models.functions import Coalesce, Greatest


def backfill(apps, schema_editor):
    Participant = apps.get_model("participants", "Participant")
    # Coalesce, not just Greatest: a participant who has never been entered into
    # a competition has no entry timestamp, and Greatest(x, NULL) is NULL on
    # SQLite — which would leave exactly the rows the sweep then deletes first.
    Participant.objects.update(
        last_used_at=Greatest(
            models.F("updated_at"),
            Coalesce(
                models.Subquery(
                    apps.get_model("participants", "EventEntry").objects
                    .filter(participant=models.OuterRef("pk"))
                    .order_by("-updated_at").values("updated_at")[:1]
                ),
                models.F("updated_at"),
            ),
        )
    )


def unfill(apps, schema_editor):
    """Nothing to undo — the column goes with it."""


class Migration(migrations.Migration):

    dependencies = [
        ('participants', '0010_alter_participant_date_of_birth'),
    ]

    operations = [
        migrations.AddField(
            model_name='participant',
            name='last_used_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill, unfill),
    ]
