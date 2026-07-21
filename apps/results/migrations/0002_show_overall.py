from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('results', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='resultcolumnsettings',
            name='show_overall',
            field=models.BooleanField(default=True),
        ),
        migrations.RemoveField(
            model_name='resultcolumnsettings',
            name='inherit_general',
        ),
    ]
