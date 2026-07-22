from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _tune_sqlite(sender, connection, **kwargs):
    """Put every SQLite connection into WAL mode with a long busy timeout. WAL
    lets readers (the live views' polling) run without blocking the writer, and
    the busy timeout makes a contended write wait rather than fail — together they
    keep an incoming timing signal from being lost when the DB is briefly locked.
    Runs for the CP540 reader thread's own connection too."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")


class TimingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.timing'
    label = 'timing'

    def ready(self):
        connection_created.connect(_tune_sqlite, dispatch_uid="timing_sqlite_wal")
