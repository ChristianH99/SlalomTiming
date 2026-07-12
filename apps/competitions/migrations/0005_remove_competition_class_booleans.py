from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('competitions', '0004_backfill_competition_classes'),
    ]

    operations = [
        migrations.RemoveField(model_name='competition', name='class_1'),
        migrations.RemoveField(model_name='competition', name='class_2'),
        migrations.RemoveField(model_name='competition', name='class_3'),
        migrations.RemoveField(model_name='competition', name='class_4'),
        migrations.RemoveField(model_name='competition', name='class_5'),
        migrations.RemoveField(model_name='competition', name='class_6'),
        migrations.RemoveField(model_name='competition', name='class_e'),
    ]
