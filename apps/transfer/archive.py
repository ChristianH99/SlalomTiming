"""Reading and writing the transfer ``.zip``.

The archive is the whole export: ``data.json`` at the root plus a ``media/``
folder for the few binary files a document references by name (results-PDF
logos). Keeping media *in* the file is what makes an export self-contained —
a system it is imported into has no access to the source's MEDIA_ROOT.
"""

import datetime
import io
import json
import zipfile

from django.core.serializers.json import DjangoJSONEncoder
from django.utils.translation import gettext as _

from .schema import DATA_NAME, FORMAT, MEDIA_DIR, VERSION, TransferError

# --- what an archive is allowed to weigh ------------------------------------
# A zip says how big each entry expands to, and reading one without looking first
# means a hand-picked 60 KiB file can decompress to as much memory as whoever wrote
# it chose. Nothing legitimate here is large: the document is text (a whole event
# with every recorded time is a few MiB) and media is two logos. So both are
# budgeted, and an entry that overruns comes back as a sentence like every other
# bad file. Checked against ZipInfo.file_size — the *declared* expanded size —
# before any entry is read, and again against what actually came out, because the
# header is only a claim.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024      # the .zip itself, on disk
MAX_DATA_BYTES = 128 * 1024 * 1024       # data.json expanded
MAX_MEDIA_ENTRY_BYTES = 16 * 1024 * 1024  # one logo expanded
MAX_MEDIA_TOTAL_BYTES = 64 * 1024 * 1024  # all media expanded


class _Encoder(DjangoJSONEncoder):
    """DjangoJSONEncoder, minus its ECMA-262 truncation of times.

    Django's encoder cuts microseconds down to milliseconds. That is fine for a
    web API and wrong here: this is a *timing* system, and both a signal's
    device_time and its received_at (which orders the runs) would come back
    rounded, so an export could not reproduce the event it came from.
    """

    def default(self, o):
        if isinstance(o, (datetime.datetime, datetime.time)):
            return o.isoformat()
        return super().default(o)


def write(document, media=None):
    """The archive bytes for *document*. ``media`` is ``{name: bytes}``, stored
    under ``media/`` and referenced from the document by name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            DATA_NAME, json.dumps(document, cls=_Encoder, indent=1, ensure_ascii=False)
        )
        for name, content in (media or {}).items():
            bundle.writestr(f"{MEDIA_DIR}/{name}", content)
    return buffer.getvalue()


def read(raw):
    """``(document, media)`` from archive bytes (or an uploaded file object).

    Raises TransferError with a message meant for the operator — this is the one
    place a hand-picked file meets the app, so every way it can be wrong (not a
    zip, not one of ours, from a newer version) has to come back as a sentence
    rather than a traceback.
    """
    try:
        bundle = zipfile.ZipFile(raw if hasattr(raw, "read") else io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError):
        raise TransferError(_("That is not a Slalom Timing export file (not a .zip archive)."))

    with bundle:
        try:
            info = bundle.getinfo(DATA_NAME)
        except KeyError:
            raise TransferError(
                _("The archive has no %(name)s — it is not a Slalom Timing export.")
                % {"name": DATA_NAME}
            )
        payload = _read_entry(bundle, info, MAX_DATA_BYTES)
        try:
            document = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise TransferError(_("The export data is damaged and could not be read."))

        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise TransferError(_("That file is not a Slalom Timing export."))

        version = document.get("version")
        if not isinstance(version, int):
            raise TransferError(_("The export does not say which version it is."))
        if version > VERSION:
            raise TransferError(
                _("This export was written by a newer version of Slalom Timing "
                  "(file version %(file)s, this system reads up to %(ours)s). "
                  "Update this system to import it.")
                % {"file": version, "ours": VERSION}
            )

        prefix = f"{MEDIA_DIR}/"
        media = {}
        budget = MAX_MEDIA_TOTAL_BYTES
        for info in bundle.infolist():
            if not info.filename.startswith(prefix) or info.filename.endswith("/"):
                continue
            content = _read_entry(bundle, info, min(MAX_MEDIA_ENTRY_BYTES, budget))
            budget -= len(content)
            media[info.filename[len(prefix):]] = content

    return document, media


def _read_entry(bundle, info, limit):
    """One zip entry's bytes, refusing anything over *limit*.

    Both the declared size and the real one are checked: the header is what the
    writer claims, so a truthful-looking small number is not a promise. A header
    that doesn't match its data (a damaged file, or one edited to get past the
    budget) fails the CRC inside zipfile — caught here, because every way a
    hand-picked file can be wrong has to come back as a sentence."""
    if info.file_size > limit:
        raise _too_big()
    try:
        with bundle.open(info) as entry:
            content = entry.read(limit + 1)
    except (zipfile.BadZipFile, OSError, EOFError):
        raise TransferError(_("The export file is damaged and could not be read."))
    if len(content) > limit:
        raise _too_big()
    return content


def _too_big():
    return TransferError(
        _("That export file is too large to read (it expands to more than this "
          "system will accept). It is either damaged or not a Slalom Timing export.")
    )
