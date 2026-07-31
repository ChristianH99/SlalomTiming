"""What may become a results-PDF logo, and under what name.

There are two doors into ``ResultsPdfLayout.image_left/right`` and they used to be
guarded very differently:

* the **settings page** assigns straight from ``request.FILES`` (there is no
  ModelForm there), so it needed its own size + image check;
* the **import** wrote whatever bytes the archive carried, under whatever name the
  archive asked for, with no check at all.

That second door was the hole. Media is served from the app's own origin, so an
archive could plant ``evil.html`` under ``media/`` and have the app serve it as
``text/html`` — stored script in the operator's session, from a file the operator
merely *imported*. The archive's filename was equally trusted, which put
``SuspiciousFileOperation`` one ``../`` away.

So both doors come through here now: the bytes are verified as an image by Pillow,
and the name is our own — an extension we recognise on a stem we generated.
"""

import secrets
from pathlib import PurePosixPath

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.translation import gettext as _

# A logo is a club emblem printed at ~18 mm high. Nothing legitimate is larger.
MAX_LOGO_BYTES = 4 * 1024 * 1024

# Extensions we are willing to write into MEDIA_ROOT. The bytes are verified
# independently — this list is about what the *filename* may claim, because that is
# what decides the Content-Type when the file is served back.
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def clean_upload(upload):
    """A picked logo, or ``(None, message)`` saying why it was refused.

    Size first, then Pillow's own verification via ``forms.ImageField`` — the same
    check a ModelForm would have run, which the settings page bypasses by assigning
    ``request.FILES`` onto the model directly."""
    if upload is None:
        return None, None
    if upload.size > MAX_LOGO_BYTES:
        return None, _(
            "The logo “%(name)s” is too large (limit %(limit)s MB)."
        ) % {"name": upload.name, "limit": MAX_LOGO_BYTES // (1024 * 1024)}
    try:
        return forms.ImageField().clean(upload), None
    except ValidationError:
        return None, _("“%(name)s” is not an image file.") % {"name": upload.name}


def clean_bytes(name, content):
    """A logo that arrived inside an import archive, as an ``UploadedFile`` ready
    to hand to ``FileField.save()`` — or ``None`` when it is not an image.

    Returns ``None`` rather than raising: a refused logo is a competition without a
    logo, not a refused import. Everything else about the event is still worth
    having, and the operator can upload the emblem again in ten seconds.
    """
    if not content or len(content) > MAX_LOGO_BYTES:
        return None
    upload = SimpleUploadedFile(safe_name(name), content)
    cleaned, _problem = clean_upload(upload)
    return cleaned


def safe_name(name):
    """A filename of *our* choosing that keeps only the claimed image type.

    The document's name is used for nothing but its suffix: it decides the served
    Content-Type, and it is the string that would otherwise reach the filesystem.
    A random stem also means two imports of the same archive can't collide.
    """
    suffix = PurePosixPath(str(name or "").replace("\\", "/")).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        suffix = ".png"
    return f"logo-{secrets.token_hex(8)}{suffix}"
