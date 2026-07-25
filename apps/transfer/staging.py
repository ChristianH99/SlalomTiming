"""Where an uploaded archive waits while the operator reviews it.

The import is a wizard — upload, review, commit — so the file has to outlive the
request that brought it. It is parked in a temp directory under a random token
that only the uploader's session knows; nothing user-supplied ever reaches the
filesystem path. Archives carry personal data, so a staged file is deleted the
moment the import is committed or abandoned, and stale ones are swept on use.
"""

import secrets
import tempfile
import time
from pathlib import Path

# How long an abandoned upload survives before it's swept (seconds).
MAX_AGE = 24 * 60 * 60

SESSION_KEY = "transfer_import_token"


def _directory():
    path = Path(tempfile.gettempdir()) / "slalomtiming-imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(token):
    # The token is ours (secrets.token_hex), but check anyway: this is the only
    # thing standing between a session value and a filesystem path.
    if not token or not all(ch in "0123456789abcdef" for ch in token):
        return None
    return _directory() / f"{token}.zip"


def store(upload):
    """Park an uploaded file and return its token."""
    sweep()
    token = secrets.token_hex(16)
    with open(_path(token), "wb") as handle:
        for chunk in upload.chunks():
            handle.write(chunk)
    return token


def read(token):
    """The staged bytes, or None if the token is unknown or expired."""
    path = _path(token)
    if path is None or not path.exists():
        return None
    return path.read_bytes()


def discard(token):
    path = _path(token)
    if path is not None:
        path.unlink(missing_ok=True)


def sweep(now=None):
    """Delete staged uploads nobody came back for."""
    now = now if now is not None else time.time()
    for path in _directory().glob("*.zip"):
        try:
            if now - path.stat().st_mtime > MAX_AGE:
                path.unlink(missing_ok=True)
        except OSError:
            continue
