"""Rendering the results-PDF header/footer back into its editor, safely.

The Results settings page loads the stored fragment into a ``contenteditable``
div, so it has to go out as markup rather than escaped text — which is what made
``|safe`` tempting, and what made an imported archive able to run script in the
importer's session (the import path didn't sanitise).

These filters run the *same* sanitiser the editor's save path uses, immediately
before rendering. Sanitising on the way in as well is the actual fix; this is the
half that holds even if a future write path forgets, because nothing reaches the
page without passing through here. Never go back to bare ``|safe`` on a field a
file can set.
"""

from django import template
from django.utils.safestring import mark_safe

from .. import pdfmarkup

register = template.Library()


@register.filter
def pdf_header(value):
    """The stored header fragment, re-sanitised, for the editor to load."""
    return mark_safe(pdfmarkup.sanitize_header(value or ""))


@register.filter
def pdf_footer(value):
    """The stored footer fragment, re-sanitised (no size markers)."""
    return mark_safe(pdfmarkup.sanitize_footer(value or ""))
