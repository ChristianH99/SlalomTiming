from django.db import migrations


def clear_derived_counts(apps, schema_editor):
    """Auto-bound runs in marshal-mode competitions used to cache the marshal-post
    totals on their own pylon/task/stop-line counts. Penalties are now computed
    live from the posts, so those cached counts are stale — and would double-count
    if such a run were later taken over as an operator entry. Zero them; genuine
    operator entries live on manual_entry runs, which are left alone."""
    TimedRun = apps.get_model("timing", "TimedRun")
    Competition = apps.get_model("competitions", "Competition")
    marshal_ids = Competition.objects.filter(
        penalties_by_marshal_posts=True
    ).values_list("id", flat=True)
    TimedRun.objects.filter(
        competition_id__in=list(marshal_ids), manual_entry=False
    ).update(pylon_count=0, task_count=0, stopline_count=0)


class Migration(migrations.Migration):
    dependencies = [
        ("timing", "0009_timedrun_stopline_adjust"),
    ]

    operations = [
        migrations.RunPython(clear_derived_counts, migrations.RunPython.noop),
    ]
