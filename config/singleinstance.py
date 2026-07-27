"""Refuse to start a second server process against the same checkout.

Three pieces of state in this app live in the process, not in the database:

* ``InMemoryChannelLayer`` — the "a time came in, re-fetch" nudges the live timing
  views run on. A second process has its own layer, so half the open browsers
  simply stop updating.
* ``apps.timing.cp540.reader`` — the module-level device reader thread. Two of them
  would fight over one TCP socket to the timing rig.
* ``apps.timing.services._server_loop`` — the event loop background threads schedule
  their ``group_send`` onto.

None of that fails loudly; it degrades silently, mid-event. One timing rig means one
server process, so that requirement is enforced here rather than left to convention:
the server takes an exclusive OS lock on ``run/server.lock`` at startup (from
``config/asgi.py``, i.e. once per server process — management commands and the test
suite never touch it). A second start is refused with an explanation in a deployment;
with ``DEBUG`` on it only warns, since running two checkouts side by side is a normal
thing to do while developing.

If the channel layer is ever swapped for a cross-process one (``channels_redis``)
this steps aside on its own; ``DJANGO_ALLOW_MULTIPLE_SERVERS=1`` forces it to.
"""

import os
import sys
from pathlib import Path

# Kept module-level on purpose: the lock lives exactly as long as this file object,
# and the OS drops it when the process ends (including a hard kill).
_lock_file = None


def _lock(handle):
    """Take an exclusive non-blocking lock on the first byte. False if held."""
    if sys.platform == 'win32':
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _holder(path):
    """The pid recorded by whoever holds the lock, for the error message."""
    try:
        return path.read_text(encoding='utf-8').split('\n', 1)[1].strip() or 'unknown'
    except (OSError, IndexError):
        return 'unknown'


def acquire():
    """Claim the single-server lock, or exit with an explanation."""
    from django.conf import settings

    if os.environ.get('DJANGO_ALLOW_MULTIPLE_SERVERS', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return

    backend = settings.CHANNEL_LAYERS.get('default', {}).get('BACKEND', '')
    if 'InMemoryChannelLayer' not in backend:
        # A cross-process channel layer is configured — multi-process is intended.
        return

    global _lock_file
    path = Path(settings.DATA_DIR) / 'run' / 'server.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = open(path, 'r+', encoding='utf-8')

    if not _lock(handle):
        holder = _holder(path)
        handle.close()
        sys.stderr.write(
            f'\nSlalom Timing is already running against this directory (pid {holder}).\n'
            'Only one server process may serve one event: live timing updates, the\n'
            'CP540 reader thread and the WebSocket nudges are all per-process, and a\n'
            'second process breaks them silently instead of failing.\n\n'
            'Stop the running server first, or — if you have configured a cross-process\n'
            'channel layer — start with DJANGO_ALLOW_MULTIPLE_SERVERS=1.\n'
            f'Lock file: {path}\n\n'
        )
        if settings.DEBUG:
            # Development: two checkouts/ports side by side is a deliberate thing to
            # do while working on the code, so this is a warning, not a wall.
            sys.stderr.write('DEBUG is on — starting anyway; live updates will be split '
                             'between the two processes.\n\n')
            return
        raise SystemExit(1)

    # Byte 0 is the locked region (only its owner may write it); the pid goes on the
    # line after, where a second process can still read it to name the holder.
    handle.seek(0)
    handle.truncate()
    handle.write(f'#\n{os.getpid()}\n')
    handle.flush()
    _lock_file = handle
