"""The single door every raw timing signal comes through, whether it arrives
over HTTP (the simulator) or straight from the CP540 reader thread. Records the
signal stamped with the active competition, folds it into the run arrangement,
keeps the two timing views in step, and nudges live views.

A time is never allowed to vanish:

* It is persisted (its own INSERT) *before* anything else and independently of any
  browser — closing a tab can't lose it; a reopened view reads it back from the DB.
* A briefly locked database is waited out (WAL + busy timeout, see apps.py) and
  the write is retried; if it still can't be written it is appended to a durable
  recovery file (``timing_unrecorded.log``) rather than dropped.
* Placing the signal into a run is a second, retried step; if it fails the signal
  is already saved and is re-placed on the next signal (see ``arrangement.reconcile``).
"""

import json
import logging
import time
from pathlib import Path

from django.conf import settings
from django.db import OperationalError

from apps.competitions.models import Competition

from . import arrangement, autotiming
from .models import TimingSettings, TimingSignal

logger = logging.getLogger(__name__)

# Last-resort durable capture for signals the DB refused even after retries.
UNRECORDED_LOG = Path(settings.BASE_DIR) / "timing_unrecorded.log"


def _is_locked(exc):
    return "locked" in str(exc).lower()


def _retry(fn, attempts=6, base_delay=0.25):
    """Run ``fn`` and, on a transient SQLite lock, wait and retry (bounded). Any
    other error, or a lock that never clears, is re-raised."""
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if not _is_locked(exc) or attempt == attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


def _capture_unrecorded(data, error):
    """Append a signal the DB wouldn't take to a durable file so it can be
    recovered/replayed later — the guarantee that a time is never simply lost."""
    try:
        with open(UNRECORDED_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({**data, "error": str(error)}) + "\n")
        logger.error("Timing signal could not be stored; captured to %s", UNRECORDED_LOG)
    except OSError:
        logger.exception("Timing signal lost: DB write and recovery-file capture both failed")


def record_signal(running_number, port, is_manual, device_time, source="device"):
    """Persist one raw signal and place it into the run arrangement. Returns the
    created ``TimingSignal``, or ``None`` if the DB was unavailable and the signal
    had to be written to the recovery file instead. Safe to call from a background
    thread."""
    from .views import broadcast_live

    competition = Competition.get_current()
    # Operator lock: when on, the time is still captured (never lost) but goes
    # straight to the ignore list instead of into a run.
    locked = TimingSettings.load().ignore_incoming

    # 1) Capture the time itself first — this is what must never be lost.
    try:
        signal = _retry(lambda: TimingSignal.objects.create(
            competition=competition,
            running_number=running_number,
            port=port,
            is_manual=is_manual,
            device_time=device_time,
            source=str(source)[:20],
            ignored=locked,
        ))
    except OperationalError as exc:
        _capture_unrecorded(
            {
                "running_number": running_number, "port": port,
                "is_manual": bool(is_manual), "device_time": device_time.isoformat(),
                "source": str(source)[:20],
            },
            exc,
        )
        return None

    # 2) Place it into a run — unless the input is locked, in which case it stays on
    #    the ignore list. The time is already saved, so a placement failure can't
    #    lose it: it (and any earlier orphan) is re-placed on the next signal.
    if competition is not None:
        if not locked:
            try:
                _retry(lambda: _place(competition, signal))
            except OperationalError:
                logger.exception("Signal %s saved but not yet placed into a run", signal.id)
        broadcast_live()

    return signal


def _place(competition, signal):
    """Fold ``signal`` into a run, sweep in any earlier saved-but-unplaced signals,
    then sync the start-order bindings. Run inside the retry so a transient lock is
    waited out. The current signal is ingested first so the reconcile sweep (which
    only touches signals not yet in a run) doesn't place it twice."""
    timing_settings = TimingSettings.load()
    arrangement.ingest(signal, timing_settings)
    arrangement.reconcile(competition, timing_settings)
    autotiming.sync_bindings(competition)
