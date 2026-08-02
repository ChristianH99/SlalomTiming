"""Wildcards and the limited rich text used by the results-PDF header/footer.

The header/footer editor stores a *restricted* HTML fragment: only ``<b>``,
``<br>`` and size markers ``<span class="pdf-sz-{small,medium,large}">`` survive
(the footer keeps no sizes). Free text may contain wildcard tokens such as
``#name`` or ``#increment`` which are resolved at export time.

Two consumers share this module:
  * the settings form sanitises submitted editor HTML before saving, and lists the
    live wildcard values shown under the editor;
  * the PDF builder resolves wildcards and translates the stored HTML into the
    mini-markup ReportLab's ``Paragraph`` understands (``<b>``, ``<br/>``,
    ``<font size="…">``).
"""

from html import escape
from html.parser import HTMLParser
from xml.sax.saxutils import escape as xml_escape

from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

# Size marker class -> point size used in the PDF header.
SIZE_POINTS = {
    "pdf-sz-small": 9,
    "pdf-sz-medium": 13,
    "pdf-sz-large": 19,
}
DEFAULT_HEADER_SIZE = SIZE_POINTS["pdf-sz-medium"]


# ---------------------------------------------------------------------------
# Wildcards
# ---------------------------------------------------------------------------

# Token -> human description (shown in the editor's insert list).
WILDCARDS = [
    ("#name", _("Competition name")),
    ("#date", _("Competition date (21.07.2026)")),
    ("#event_date_long", _("Competition date, long (21. July 2026)")),
    ("#year", _("Competition year")),
    ("#increment", _("Edition number, counted from the start year")),
    ("#discipline", _("Discipline / competition type")),
    ("#class", _("This page's class or Overall table")),
]


def _long_date(d):
    # Locale-aware long date: the active language's DATE_FORMAT resolves the month
    # name (e.g. "21 July 2026" in English, "21. Juli 2026" in German).
    return date_format(d, format="DATE_FORMAT", use_l10n=True)


def wildcard_values(competition, layout, class_label=""):
    """Ordered ``[{token, label, value}, …]`` — the resolved value of every
    wildcard for this competition. ``class_label`` fills ``#class`` (the results
    table being exported); ``increment_start_year`` on ``layout`` drives
    ``#increment`` (competition year − start year + 1, or ``?`` when unset)."""
    date = competition.date
    if layout.increment_start_year and date and date.year >= layout.increment_start_year:
        increment = str(date.year - layout.increment_start_year + 1)
    else:
        increment = "?"
    resolved = {
        "#name": competition.name or "",
        "#date": date.strftime("%d.%m.%Y") if date else "",
        "#event_date_long": _long_date(date) if date else "",
        "#year": str(date.year) if date else "",
        "#increment": increment,
        # `rules`, not `competition_type`: an archived event prints the discipline
        # it was run under, even if that type has since been renamed.
        "#discipline": _discipline(competition),
        "#class": class_label or "",
    }
    return [
        {"token": token, "label": label, "value": resolved[token]}
        for token, label in WILDCARDS
    ]


def _discipline(competition):
    """The competition's discipline name, or "" when it has no type yet (a
    transient competition built for the settings page's sample PDF has none)."""
    if competition.archived_rules:
        return competition.rules.name or ""
    return competition.competition_type.name if competition.competition_type_id else ""


def resolve_wildcards(text, competition, layout, class_label=""):
    """Replace every wildcard token in a plain-text run with its value. Longer
    tokens are tried first so ``#event_date_long`` isn't eaten by ``#event``-style
    prefixes (there are none today, but the ordering keeps it safe)."""
    values = {w["token"]: w["value"] for w in wildcard_values(competition, layout, class_label)}
    for token in sorted(values, key=len, reverse=True):
        value = values[token]
        # An unset #increment renders as nothing in the actual PDF.
        text = text.replace(token, "" if value == "?" and token == "#increment" else value)
    return text


# ---------------------------------------------------------------------------
# Sanitising submitted editor HTML
# ---------------------------------------------------------------------------

class _Sanitizer(HTMLParser):
    """Rebuild a submitted fragment keeping only the allowed tags. ``<strong>`` is
    normalised to ``<b>``; ``<div>``/``<p>`` boundaries become ``<br>`` so the
    contenteditable's block splits survive as line breaks; unknown tags are
    dropped (their text is kept)."""

    def __init__(self, allow_sizes):
        super().__init__(convert_charrefs=True)
        self.allow_sizes = allow_sizes
        self.parts = []
        self._open = []  # stack of emitted tags needing a close

    def handle_starttag(self, tag, attrs):
        if tag in ("b", "strong"):
            self.parts.append("<b>")
            self._open.append("b")
        elif tag == "span" and self.allow_sizes:
            cls = dict(attrs).get("class", "")
            size = next((c for c in cls.split() if c in SIZE_POINTS), None)
            if size:
                self.parts.append(f'<span class="{size}">')
                self._open.append("span")
            else:
                self._open.append("")  # tracked so the matching close is a no-op
        elif tag == "br":
            self.parts.append("<br>")
        elif tag in ("div", "p"):
            # A block start after existing content is a line break.
            if any(p not in ("<br>",) for p in self.parts):
                self.parts.append("<br>")
            self._open.append("")
        else:
            self._open.append("")

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if self._open:
            emitted = self._open.pop()
            if emitted:
                self.parts.append(f"</{emitted}>")

    def handle_data(self, data):
        self.parts.append(escape(data))

    def result(self):
        while self._open:
            emitted = self._open.pop()
            if emitted:
                self.parts.append(f"</{emitted}>")
        html = "".join(self.parts)
        # Trim leading/trailing line breaks left by block wrappers.
        while html.startswith("<br>"):
            html = html[4:]
        while html.endswith("<br>"):
            html = html[:-4]
        return html.strip()


def sanitize_header(html):
    parser = _Sanitizer(allow_sizes=True)
    parser.feed(html or "")
    return parser.result()


def sanitize_footer(html):
    parser = _Sanitizer(allow_sizes=False)
    parser.feed(html or "")
    return parser.result()


# ---------------------------------------------------------------------------
# Stored HTML -> ReportLab Paragraph markup
# ---------------------------------------------------------------------------

class _ToReportlab(HTMLParser):
    """Translate stored header/footer HTML into ReportLab mini-markup, resolving
    wildcards inside text runs and XML-escaping their values."""

    def __init__(self, resolver):
        super().__init__(convert_charrefs=True)
        self.resolver = resolver
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in ("b", "strong"):
            self.out.append("<b>")
        elif tag == "span":
            cls = dict(attrs).get("class", "")
            size = next((c for c in cls.split() if c in SIZE_POINTS), None)
            if size:
                self.out.append(f'<font size="{SIZE_POINTS[size]}">')
        elif tag == "br":
            self.out.append("<br/>")

    def handle_endtag(self, tag):
        if tag in ("b", "strong"):
            self.out.append("</b>")
        elif tag == "span":
            self.out.append("</font>")

    def handle_data(self, data):
        self.out.append(xml_escape(self.resolver(data)))

    def markup(self):
        return "".join(self.out)


def to_reportlab_markup(html, resolver):
    """``html`` (sanitised header/footer) -> ReportLab Paragraph markup with
    wildcards resolved. Returns an empty string when there's no content."""
    if not (html or "").strip():
        return ""
    parser = _ToReportlab(resolver)
    parser.feed(html)
    return parser.markup()
