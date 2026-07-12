import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('participants', '0003_backfill_participant_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='participant',
            name='competition_type',
            field=models.ForeignKey(help_text='Discipline this participant is registered under (e.g. Motorcycle, Go-Cart). A participant can only ever belong to one type.', on_delete=django.db.models.deletion.PROTECT, related_name='participants', to='competitions.competitiontype'),
        ),
        migrations.RemoveField(
            model_name='participant',
            name='bib_number',
        ),
        migrations.RemoveField(
            model_name='participant',
            name='status',
        ),
        migrations.RemoveField(
            model_name='participant',
            name='year_of_birth',
        ),
    ]
