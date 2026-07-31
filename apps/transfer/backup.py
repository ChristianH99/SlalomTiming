"""Copying the database to somewhere else, on a timer.

The event *is* the database. Until now the only backup was a line in the run-book
asking the operator to run ``VACUUM INTO`` between runs and copy the result to a
USB stick — which is a thing to remember while timing a race, and the person who
forgets is the person who most needed it.

Two things worth knowing about how the copy is made:

* **SQLite's own online-backup API**, not a file copy. The database is being
  written to while this runs (the CP540 reader thread records times through it),
  and copying the file underneath that gives you a torn database — worse than
  none, because it looks like a backup. A plain file copy is wrong for a second
  reason too: in WAL mode the recent writes are in the ``-wal`` file beside the
  database, so the copy carried off on a stick is missing them. The backup API
  writes one self-contained, consistent file.
* **In a single step** (``pages=-1``), which is not the obvious choice and is the
  important one. A *batched* backup gives up its read lock between batches — and
  SQLite restarts the whole copy whenever another connection writes in the
  meantime. Under sustained writes (an import, a burst of marshal taps) it
  restarts for ever and the thread spins without ever producing a file. One step
  cannot be restarted. It costs nothing here because **WAL readers do not block
  the writer**: the rig goes on recording times throughout.
* **Written to a temporary name and renamed** when it is complete. A copy
  interrupted half-way (the stick pulled out, the disk full) must not be sitting
  there under a plausible name looking like a backup.

The thread is process state, like the CP540 reader, so it is started from
``config/asgi.py`` — the one entry point that is actually a server.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from django.db import close_old_connections

logger = logging.getLogger(__name__)

PREFIX = "slalomtiming-"
SUFFIX = ".sqlite3"
PARTIAL = ".partial"
# How often the thread wakes to see whether it is due. Short, so turning the
# setting off (or changing the interval) takes effect now rather than in ten
# minutes, and so shutdown is immediate.
TICK = 2.0


def copy_database(source, destination_dir, keep, now=None):
    """Write one snapshot of ``source`` into ``destination_dir``.

    Returns the ``Path`` written. Raises whatever went wrong — the caller records
    it; there is nobody here to tell.
    """
    now = now or datetime.now()
    destination_dir = Path(destination_dir)
    name = f"{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}"
    final = destination_dir / name
    partial = destination_dir / (name + PARTIAL)

    source_conn = _open_source(source)
    try:
        target_conn = sqlite3.connect(partial, timeout=30)
        try:
            # One step. Batching looks kinder to the rig and is the opposite: a
            # backup that yields its read lock is restarted by SQLite every time
            # another connection writes, so under a run of writes it never
            # finishes. See the module docstring.
            source_conn.backup(target_conn, pages=-1)
        finally:
            target_conn.close()
    finally:
        source_conn.close()

    partial.replace(final)      # atomic on both platforms, same filesystem
    prune(destination_dir, keep)
    return final


def _open_source(source):
    """A read-only connection to the live database.

    ``source`` is normally a path, but it can already be a SQLite *URI* — the test
    suite runs on ``file:memorydb_default?mode=memory&cache=shared`` — and wrapping
    one URI in another gives "no such cache mode". So a URI is passed through as it
    is and only a path gets the read-only wrapper.
    """
    source = str(source)
    if source.startswith("file:"):
        return sqlite3.connect(source, uri=True, timeout=30)
    return sqlite3.connect(f"file:{Path(source).as_posix()}?mode=ro",
                           uri=True, timeout=30)


def prune(destination_dir, keep):
    """Delete all but the ``keep`` newest snapshots, and any partial left behind.

    One a minute over an eight-hour event is 480 copies of a growing database.
    A full stick means the *newest* copy is the one that failed to write, which is
    the one you wanted.
    """
    destination_dir = Path(destination_dir)
    for leftover in destination_dir.glob(f"{PREFIX}*{SUFFIX}{PARTIAL}"):
        try:
            leftover.unlink()
        except OSError:
            pass
    snapshots = sorted(destination_dir.glob(f"{PREFIX}*{SUFFIX}"))
    for old in snapshots[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            logger.warning("Could not delete the old backup %s", old)


class BackupRunner:
    """The timer. One per process (``runner`` below)."""

    def __init__(self):
        self._thread = None
        self._running = threading.Event()
        self._due_now = threading.Event()

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="backup-runner")
        self._thread.start()

    def stop(self):
        self._running.clear()
        self._due_now.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def run_soon(self):
        """Make the next tick due immediately — for a settings change, so the
        operator finds out straight away whether the destination works."""
        self._due_now.set()

    def _worker(self):
        while self._running.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - a backup must never end the thread
                logger.exception("Backup tick failed")
            self._due_now.wait(TICK)
            self._due_now.clear()
        close_old_connections()

    def _tick(self):
        from django.conf import settings as django_settings
        from django.utils import timezone

        from .models import BackupSettings

        settings = BackupSettings.load()
        if not settings.enabled:
            return
        if settings.last_run_at is not None:
            due_at = settings.last_run_at.timestamp() + settings.interval_minutes * 60
            if timezone.now().timestamp() < due_at:
                return

        problem = settings.destination_problem()
        now = timezone.now()
        if problem:
            _record(settings, now, error=problem)
            return
        try:
            written = copy_database(
                django_settings.DATABASES["default"]["NAME"],
                settings.destination_path, settings.keep,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backup to %s failed", settings.destination)
            _record(settings, now, error=str(exc)[:300])
            return
        _record(settings, now, path=written)
        logger.info("Database backed up to %s", written)


def _record(settings, when, path=None, error=""):
    settings.last_run_at = when
    settings.last_error = error
    if not error:
        settings.last_ok_at = when
        settings.last_file = str(path)
        settings.last_bytes = path.stat().st_size if path.exists() else None
    settings.save(update_fields=["last_run_at", "last_ok_at", "last_error",
                                 "last_file", "last_bytes"])
    close_old_connections()


runner = BackupRunner()


def autostart():
    """Start the timer if backups are configured. From config/asgi.py.

    Never raises: an unmigrated database means no backups yet, not a server that
    won't boot — the same rule cp540.autostart follows.
    """
    try:
        from .models import BackupSettings

        if not BackupSettings.load().enabled:
            return False
        runner.start()
    except Exception:  # noqa: BLE001
        logger.warning("Could not start the backup timer", exc_info=True)
        return False
    logger.info("Backup timer started")
    return True
