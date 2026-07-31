"""Keeping the data directory to the account that runs the server.

Everything this app knows lives in ``DATA_DIR``: the event database, the uploaded
logos, the audit trail, the recovery file for times the database refused, and the
signing key a development checkout generated for itself. Between them that is
every competitor's name, date of birth, address, e-mail, phone and licence number
— and a key that forges sessions.

Whether that should be encrypted at rest has been asked and answered: it is not,
deliberately. SQLCipher means a different driver and a passphrase somebody has to
supply on race morning, and a passphrase kept beside the database protects nothing. What is worth
doing is the part that costs nothing — making sure the files are readable by the
account running the server and nobody else, rather than by every account on the
machine.

Two mechanisms, because the platforms don't share one:

* **POSIX** — a ``0o077`` umask so *everything* the process creates from now on is
  user-only (SQLite's ``-wal`` and ``-shm`` companions included, which is the case
  an explicit chmod at startup always misses), plus a chmod pass over what is
  already there.
* **Windows** — ``icacls`` on the directory: inheritance broken, then full control
  granted to the current user, SYSTEM and Administrators and to nobody else. New
  files inherit that. ``os.chmod`` is not an alternative here; on Windows it only
  toggles the read-only bit and says nothing about who may read.

Nothing here may stop the server coming up. A data directory on a filesystem that
has no concept of any of this (a network share, a FAT stick) is a reason to log
and carry on, not to refuse to time the event.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Directory 0o700, file 0o600: the owner, nobody else.
DIR_MODE = 0o700
FILE_MODE = 0o600


def harden(data_dir, enabled=True):
    """Restrict ``data_dir`` and its contents to the current account.

    Returns a short description of what was done, for the log. Never raises.
    """
    if not enabled:
        return 'skipped (DJANGO_HARDEN_DATA_DIR is off)'
    path = Path(data_dir)
    if not path.is_dir():
        return f'skipped ({path} is not a directory)'
    try:
        if sys.platform == 'win32':
            return _harden_windows(path)
        return _harden_posix(path)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning('Could not restrict permissions on %s', path, exc_info=True)
        return 'failed (see the log)'


def _harden_posix(path):
    # The umask covers everything created *later*, which is the half a chmod pass
    # cannot reach: SQLite writes db.sqlite3-wal and -shm on its own, the logging
    # handlers roll new files over, and an upload lands whenever it lands.
    os.umask(0o077)
    changed = 0
    for entry in [path, *path.rglob('*')]:
        wanted = DIR_MODE if entry.is_dir() else FILE_MODE
        try:
            if (entry.stat().st_mode & 0o777) != wanted:
                entry.chmod(wanted)
                changed += 1
        except OSError:
            continue  # a filesystem that doesn't do modes; the umask still stands
    return f'umask 0o077, {changed} path(s) tightened'


def _harden_windows(path):
    """``icacls``: break inheritance, then grant the three principals that need it.

    Run on the directory only. Children with no explicit permissions of their own
    inherit from it, which is all of ours — and it keeps this to one fast call at
    startup rather than a walk of a database and a season of logs.
    """
    account = os.environ.get('USERNAME')
    if not account:
        return 'skipped (no USERNAME to grant to)'
    grants = [f'{account}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F']
    # SYSTEM and Administrators by SID, not by name: those names are localised, and
    # this app is run on German-language Windows more often than not.
    result = subprocess.run(
        ['icacls', str(path), '/inheritance:r',
         *(arg for grant in grants for arg in ('/grant:r', grant))],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        logger.warning('icacls on %s failed: %s', path, result.stderr.strip()[:200])
        return 'failed (icacls)'
    return f'restricted to {account}, SYSTEM and Administrators'
