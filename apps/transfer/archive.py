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
            payload = bundle.read(DATA_NAME)
        except KeyError:
            raise TransferError(
                _("The archive has no %(name)s — it is not a Slalom Timing export.")
                % {"name": DATA_NAME}
            )
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
        media = {
            name[len(prefix):]: bundle.read(name)
            for name in bundle.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        }

    return document, media
