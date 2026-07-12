import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('competitions', '0007_backfill_competition_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='competition',
            name='competition_type',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='competitions', to='competitions.competitiontype'),
        ),
    ]
