"""Browsing the *host's* folders, so a backup destination can be picked.

A file input can't do this job. The browser would offer the folders of whichever
machine is displaying the page — and the point of this app is that other people
open it over the venue network, so more often than not that is the wrong machine
entirely. `C:\\Users\\marshal\\Desktop` from a phone means nothing to the laptop
doing the writing. The path has to come from the server, so the listing does too.

That makes this a directory-listing endpoint, which is worth being deliberate
about. Three limits, and none of them is decoration:

* **Folders only.** Names of directories, never files, never contents. Somebody
  who can reach this can learn that `D:\\Fotos` exists, and nothing about what is
  in it.
* **No path is trusted.** What comes back is resolved and checked to be a real
  directory before it is listed, so `..` walks and junk both come out as "that
  isn't a folder" rather than as an error with a filesystem path in it.
* **It is gated.** Same page key as the rest of the Backup section
  (`import_export`), so it is exactly as reachable as the page it serves.

Roots differ by platform: Windows has drive letters (which is the case that
matters — the destination is nearly always a USB stick), POSIX has one tree.
"""

import os
import string
from pathlib import Path

from django.utils.translation import gettext as _

# A folder full of thousands of entries is a scroll list nobody can use, and
# serialising it is work nobody asked for. The picker says how many it left out.
MAX_ENTRIES = 300


def roots():
    """Where browsing starts: the drives on Windows, "/" everywhere else."""
    if _is_windows():
        found = []
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            # exists() on an empty CD drive or a disconnected mapping blocks for
            # a moment and then says no, which is the answer we want anyway.
            try:
                if drive.exists():
                    found.append(drive)
            except OSError:
                continue
        return found
    return [Path("/")]


def _is_windows():
    return os.name == "nt"


def listing(raw_path=None):
    """What the picker renders: this folder, its parent, and the folders in it.

    ``raw_path`` empty (or unusable) means the roots, which is also the answer to
    anything that isn't a folder — the caller gets somewhere useful rather than an
    error to interpret.
    """
    if not raw_path:
        return _roots_listing()

    try:
        path = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return _roots_listing(problem=_("That is not a folder on this computer."))
    if not path.is_dir():
        return _roots_listing(problem=_("“%(path)s” is not a folder on this "
                                        "computer.") % {"path": raw_path})

    try:
        entries, truncated = _folders_in(path)
    except PermissionError:
        return {
            **_here(path),
            "entries": [],
            "truncated": 0,
            "problem": _("This computer won’t let the server read “%(path)s”.")
                       % {"path": path},
        }
    except OSError:
        return {
            **_here(path),
            "entries": [],
            "truncated": 0,
            "problem": _("“%(path)s” can’t be read — is the drive still "
                         "connected?") % {"path": path},
        }
    return {**_here(path), "entries": entries, "truncated": truncated, "problem": ""}


def _folders_in(path):
    """The sub-folders, by name. Anything that can't be inspected is skipped:
    a junction to a disconnected share raises when asked whether it is a
    directory, and one of those must not empty the whole list."""
    found = []
    truncated = 0
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            if len(found) >= MAX_ENTRIES:
                truncated += 1
                continue
            found.append({"name": entry.name, "path": str(Path(entry.path))})
    found.sort(key=lambda item: item["name"].lower())
    return found, truncated


def _here(path):
    """This folder, its label and its parent.

    Deliberately *not* whether it can be written to. Answering that means writing
    a probe file (BackupSettings.destination_problem), and browsing must not leave
    a trail of them through every folder the operator clicks past. The one folder
    that matters is the one they choose, and the form checks it on Save with a
    message that names the reason — see forms.BackupSettingsForm.
    """
    parent = path.parent
    return {
        "path": str(path),
        "label": _label(path),
        # A root's parent is itself, which would be a button that does nothing;
        # from a root the way out is the drive list.
        "parent": str(parent) if parent != path else "",
        "at_root": parent == path,
    }


def _label(path):
    """What to call this folder in the header. A drive has no name of its own."""
    return path.name or str(path)


def _roots_listing(problem=""):
    return {
        "path": "",
        "label": _("This computer"),
        "parent": "",
        "at_root": True,
        "entries": [{"name": _label(root), "path": str(root)} for root in roots()],
        "truncated": 0,
        "problem": problem,
    }
