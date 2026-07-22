"""Render results tables to A4 PDF with a configurable header/footer.

A *section* is one results table (a class or an Overall group) as produced by
``views.class_section`` / ``views.overall_section`` — its ``layout`` (column
structure) plus the ranked/unranked row dicts. ``render_results_pdf`` lays each
section on its own page(s):

  * the same columns and values shown on screen, in a ReportLab table that repeats
    its header row, always spans the full page width, and only ever breaks *between*
    rows (never through one). Every body row is exactly the same height — each info
    field takes one line (fitted to its column so it never wraps to a second),
    run/total cells always two lines, all vertically centred — matching the web
    results table's regulations;
  * a per-page header (two optional logos top-left/top-right, each a configurable
    height, and the configurable rich header text centred *between* them) and footer
    (configurable centred rich text, the export date/time in the computer's local
    timezone bottom-left, ``page / total`` bottom-right);
  * A4 in the orientation chosen on the settings page (or auto-fit).

The wildcard ``#class`` resolves to the table on the page, so each section gets its
own page template with its own resolved header/footer.
"""

from datetime import datetime
from functools import partial
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate,
    Paragraph, Table, TableStyle,
)
from reportlab.lib.utils import ImageReader

from . import pdfmarkup
from .models import ResultsPdfLayout

# Relative column weights; the table is scaled to span the full usable page width
# (up or down), so the weights only set the *proportions* between columns.
_COLW = {
    "rank": 11, "bib": 11, "class": 22, "name": 46, "address": 38,
    "vehicle": 34, "licence": 28, "run": 18, "total": 24,
}

_MARGIN_X = 12 * mm
_LOGO_MAX_W = 70 * mm  # cap a very wide logo so it can't crowd out the header
_LOGO_GAP = 6 * mm     # clearance between a logo and the header text
_BOTTOM_EDGE = 8 * mm  # baseline of the export-date / page-number info line

# Body text is sized so a portrait page shows roughly 17 result rows.
_BODY_FONT = 11
_BODY_LEAD = 13.5
_CELL_VPAD = 3
_CELL_HPAD = 3.5

_GRAY = colors.HexColor("#7c8492")
_PEN = colors.HexColor("#C93912")
_HEADER_BG = colors.HexColor("#eef1f5")
_GRID = colors.HexColor("#d5d9df")
_INK = colors.HexColor("#1B2333")


# ---------------------------------------------------------------------------
# Paragraph styles
# ---------------------------------------------------------------------------

_cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=_BODY_FONT,
                       leading=_BODY_LEAD, alignment=TA_LEFT, textColor=_INK)
_cell_c = ParagraphStyle("cellc", parent=_cell, alignment=TA_CENTER)
_head = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=8.5,
                       leading=10, alignment=TA_LEFT, textColor=colors.HexColor("#5B6472"))
_head_c = ParagraphStyle("headc", parent=_head, alignment=TA_CENTER)
_title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=13.5, leading=17,
                        spaceAfter=4, textColor=_INK, keepWithNext=True)
_subhead = ParagraphStyle("subhead", fontName="Helvetica-Bold", fontSize=10,
                          leading=13, spaceBefore=8, spaceAfter=3, textColor=_INK,
                          keepWithNext=True)
_summary = ParagraphStyle("summary", fontName="Helvetica", fontSize=9, leading=12,
                          spaceBefore=6, textColor=colors.HexColor("#444"))
_footer_style = ParagraphStyle("ftr", fontName="Helvetica", fontSize=9.5,
                               leading=12, alignment=TA_CENTER, textColor=_INK)


def _header_style():
    return ParagraphStyle("hdr", fontName="Helvetica", fontSize=pdfmarkup.DEFAULT_HEADER_SIZE,
                          leading=pdfmarkup.DEFAULT_HEADER_SIZE * 1.25, alignment=TA_CENTER,
                          textColor=_INK)


# ---------------------------------------------------------------------------
# Single-line fitting (keeps every row the same height)
# ---------------------------------------------------------------------------

def _fit(text, max_width, bold=False, size=_BODY_FONT):
    """Truncate ``text`` with an ellipsis so it renders on one line within
    ``max_width`` points — so a long name/club/vehicle never wraps and every row
    keeps the same height."""
    text = text or ""
    font = "Helvetica-Bold" if bold else "Helvetica"
    if stringWidth(text, font, size) <= max_width:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if stringWidth(text[:mid] + ell, font, size) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo else ell


def _numeric_font(layout, rows, col_widths):
    """The font size for time/total cells: as large as ``_BODY_FONT`` but shrunk
    (never below 7pt) so the widest time still fits its column on one line — keeps
    times from wrapping whatever the precision, run count or orientation."""
    cols = _columns(layout)
    run_inner = [col_widths[i] - 2 * _CELL_HPAD
                 for i, (kind, _) in enumerate(cols) if kind in ("run", "total")]
    if not run_inner:
        return _BODY_FONT
    target = min(run_inner)
    widest = 0.0
    for row in rows or []:
        times = [c.get("time") or "" for c in row.get("training", [])]
        times += [c.get("time") or "" for c in row.get("counted", [])]
        for t in times:
            widest = max(widest, stringWidth(t, "Helvetica", 10))
        widest = max(widest, stringWidth(row.get("total") or "", "Helvetica-Bold", 10))
    if widest <= 0:
        return _BODY_FONT
    return max(7.0, min(_BODY_FONT, target * 10 / widest))


def _max_chars(text, max_width, font, size):
    """The largest number of leading characters of ``text`` that fit ``max_width``."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if stringWidth(text[:mid], font, size) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _wrap_to_lines(text, max_width, max_lines, bold=False, size=_BODY_FONT):
    """Word-wrap ``text`` into at most ``max_lines`` lines within ``max_width``.
    Overly long words are hard-broken and any overflow is ellipsised onto the last
    line. Every returned line already fits ``max_width`` so a Paragraph rendering
    them (joined by <br/>) never re-wraps and the row height stays fixed."""
    font = "Helvetica-Bold" if bold else "Helvetica"
    text = (text or "").strip()
    if not text:
        return ['\xa0']
    tokens = []
    for word in text.split():
        while stringWidth(word, font, size) > max_width and len(word) > 1:
            n = max(1, _max_chars(word, max_width, font, size))
            tokens.append(word[:n])
            word = word[n:]
        tokens.append(word)
    lines, cur = [], ""
    for tok in tokens:
        trial = tok if not cur else cur + " " + tok
        if not cur or stringWidth(trial, font, size) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        head = lines[:max_lines - 1]
        tail = " ".join(lines[max_lines - 1:])
        head.append(_fit(tail, max_width, bold=bold, size=size))
        lines = head
    return lines


# ---------------------------------------------------------------------------
# Table building
# ---------------------------------------------------------------------------

def _columns(layout):
    """The ordered ``(kind, weight)`` columns for a table layout — same order as the
    on-screen head/mid-cell partials."""
    cols = [("rank", _COLW["rank"]), ("bib", _COLW["bib"])]
    if layout["include_class"]:
        cols.append(("class", _COLW["class"]))
    if layout["name_keys"]:
        cols.append(("name", _COLW["name"]))
    if layout["address_keys"]:
        cols.append(("address", _COLW["address"]))
    if layout["has_vehicle"]:
        cols.append(("vehicle", _COLW["vehicle"]))
    if layout["licence_keys"]:
        cols.append(("licence", _COLW["licence"]))
    for _ in layout["training_labels"]:
        cols.append(("run", _COLW["run"]))
    for _ in layout["counted_labels"]:
        cols.append(("run", _COLW["run"]))
    cols.append(("total", _COLW["total"]))
    return cols


def _col_widths(layout, usable_w):
    """Absolute column widths that always sum to ``usable_w`` — the weights scaled
    (up or down) equally so the table spans the whole page width."""
    cols = _columns(layout)
    total = sum(w for _, w in cols)
    return [usable_w * w / total for _, w in cols]


def _row_lines(layout):
    """The fixed number of text lines every body row occupies: one per field in the
    tallest info block, but never fewer than the two lines a run/total cell uses."""
    return max(
        len(layout["name_keys"]),
        len(layout["address_keys"]),
        len(layout["licence_keys"]),
        2,
    )


def _header_cells(layout):
    cells = [Paragraph("Rank", _head_c), Paragraph("Bib", _head_c)]
    if layout["include_class"]:
        cells.append(Paragraph("Class", _head))
    if layout["name_keys"]:
        cells.append(Paragraph(escape(layout["name_header"]), _head))
    if layout["address_keys"]:
        cells.append(Paragraph(escape(layout["address_header"]), _head))
    if layout["has_vehicle"]:
        cells.append(Paragraph("Vehicle", _head))
    if layout["licence_keys"]:
        cells.append(Paragraph(escape(layout["licence_header"]), _head))
    for t in layout["training_labels"]:
        cells.append(Paragraph(escape(t), _head_c))
    for c in layout["counted_labels"]:
        cells.append(Paragraph(escape(c), _head_c))
    cells.append(Paragraph(escape(layout["score_heading"]), _head_c))
    return cells


def _block_markup(lines, inner_w, row_lines):
    """A multi-line info block (name/address/licence). Each field takes one line
    so fields stay aligned across rows; a *single*-field block (e.g. just the
    driver name) may wrap to use the row's spare lines when it is too long. The
    block is never padded — vertical centring within the fixed row height is left
    to the cell's middle valign, so a lone value sits centred, not at the top."""
    if len(lines) == 1:
        field = lines[0]
        bold = bool(field.get("bold"))
        wrapped = _wrap_to_lines(field.get("text") or "", inner_w, row_lines, bold=bold)
        wrapped = [escape(w) or " " for w in wrapped]
        if bold:
            wrapped = [f"<b>{w}</b>" for w in wrapped]
        return "<br/>".join(wrapped)
    out = []
    for line in lines:
        text = _fit(line.get("text") or "", inner_w, bold=line.get("bold"))
        esc = escape(text) or " "
        out.append(f"<b>{esc}</b>" if line.get("bold") else esc)
    return "<br/>".join(out)


def _run_markup(cell, num_size, inner_w):
    time = escape(_fit(cell.get("time") or " ", inner_w, size=num_size))
    pen = escape(cell.get("penalty") or "")
    small = round(num_size * 0.78, 1)
    pen_line = (f'<font size="{small}" color="#C93912">{pen}</font>' if pen else " ")
    return f'<font size="{num_size}">{time}</font><br/>{pen_line}'


def _row_cells(layout, row, ranked, col_widths, num_size):
    """The Paragraph cells for one body row, in column order. Each info field is
    fitted to its column so nothing wraps; run/total cells are two lines."""
    def inner(idx):
        return col_widths[idx] - 2 * _CELL_HPAD

    rank = row.get("rank")
    cells = [
        Paragraph(str(rank) if rank else "–", _cell_c),
        Paragraph(escape(str(row.get("bib", ""))), _cell_c),
    ]
    i = 2
    if layout["include_class"]:
        name = escape(_fit(row.get("class_name") or "", inner(i)))
        occ = row.get("occurrence")
        if occ:
            name += f' <font color="#7c8492">({occ + 1})</font>'
        cells.append(Paragraph(name, _cell)); i += 1
    lines = _row_lines(layout)
    if layout["name_keys"]:
        cells.append(Paragraph(_block_markup(row.get("name_lines", []), inner(i), lines), _cell)); i += 1
    if layout["address_keys"]:
        cells.append(Paragraph(_block_markup(row.get("address_lines", []), inner(i), lines), _cell)); i += 1
    if layout["has_vehicle"]:
        wrapped = _wrap_to_lines(row.get("vehicle") or "", inner(i), lines)
        cells.append(Paragraph("<br/>".join(escape(w) for w in wrapped), _cell)); i += 1
    if layout["licence_keys"]:
        cells.append(Paragraph(_block_markup(row.get("licence_lines", []), inner(i), lines), _cell)); i += 1
    for cell in row.get("training", []):
        cells.append(Paragraph(_run_markup(cell, num_size, inner(i)), _cell_c)); i += 1
    for cell in row.get("counted", []):
        cells.append(Paragraph(_run_markup(cell, num_size, inner(i)), _cell_c)); i += 1
    small = round(num_size * 0.78, 1)
    if ranked:
        total = escape(_fit(row.get("total") or "–", inner(i), bold=True, size=num_size))
        gap = escape(row.get("gap") or "")
        gap_line = f'<font size="{small}" color="#7c8492">{gap}</font>' if gap else " "
        markup = f'<b><font size="{num_size}">{total}</font></b><br/>{gap_line}'
    else:
        markup = f'<font size="{num_size}" color="#7c8492">{escape((row.get("status") or "").upper())}</font><br/> '
    cells.append(Paragraph(markup, _cell_c))
    return cells


def _make_table(layout, rows, ranked, usable_w):
    """A ReportLab table for a set of rows: full page width, uniform body-row height,
    repeating header, splitting only between rows."""
    col_widths = _col_widths(layout, usable_w)
    row_h = _row_lines(layout) * _BODY_LEAD + 2 * _CELL_VPAD
    num_size = _numeric_font(layout, rows, col_widths)

    data = [_header_cells(layout)]
    heights = [None]  # header row auto-sizes
    if rows:
        for row in rows:
            data.append(_row_cells(layout, row, ranked, col_widths, num_size))
            heights.append(row_h)
    else:
        data.append([Paragraph("No complete results yet.",
                                ParagraphStyle("e", parent=_cell_c, textColor=_GRAY))]
                    + [""] * (len(col_widths) - 1))
        heights.append(row_h)

    table = Table(data, colWidths=col_widths, rowHeights=heights, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, _GRID),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, _GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), _CELL_HPAD),
        ("RIGHTPADDING", (0, 0), (-1, -1), _CELL_HPAD),
        ("TOPPADDING", (0, 0), (-1, -1), _CELL_VPAD),
        ("BOTTOMPADDING", (0, 0), (-1, -1), _CELL_VPAD),
    ]
    if not rows:
        style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(style))
    return table


def _section_flowables(section, usable_w):
    """Title + ranked table (+ unranked block) + summary for one section."""
    layout = section["layout"]
    headline = f'{section["title"]} · Results · {section["scoring_label"]}'
    flow = [Paragraph(escape(headline), _title)]
    flow.append(_make_table(layout, section["ranked"], True, usable_w))
    if section["unranked"]:
        flow.append(Paragraph("Not yet ranked", _subhead))
        flow.append(_make_table(layout, section["unranked"], False, usable_w))
    summary = layout["summary"]
    flow.append(Paragraph(
        f'<b>Starters:</b> {summary["starters"]} &nbsp;·&nbsp; '
        f'<b>Classified:</b> {summary["classified"]} &nbsp;·&nbsp; '
        f'<b>Not Classified:</b> {summary["not_classified"]}',
        _summary,
    ))
    return flow


# ---------------------------------------------------------------------------
# Logos + page decorations (header / footer)
# ---------------------------------------------------------------------------

def _logo_draw_size(reader, height_mm):
    """The drawn ``(w, h)`` of a logo at the configured height, aspect preserved and
    width capped so it can't crowd out the header."""
    iw, ih = reader.getSize()
    if not iw or not ih:
        return None
    h = (height_mm or ResultsPdfLayout.DEFAULT_LOGO_HEIGHT_MM) * mm
    w = iw * (h / ih)
    if w > _LOGO_MAX_W:
        w = _LOGO_MAX_W
        h = ih * (w / iw)
    return w, h


def _draw_page(canv, doc, geom, header_markup, footer_markup, export_dt):
    """onPage: logos flanking the top with the centred header text between them, the
    centred footer text, and the bottom-left export date/time (the page number is
    drawn by NumberedCanvas)."""
    w, h = geom["pagesize"]
    band_center = h - geom["top_pad"] - geom["band_h"] / 2

    left = geom["left"]
    right = geom["right"]
    if left:
        canv.drawImage(left["reader"], _MARGIN_X, band_center - left["h"] / 2,
                       width=left["w"], height=left["h"], preserveAspectRatio=True, mask="auto")
    if right:
        canv.drawImage(right["reader"], w - _MARGIN_X - right["w"], band_center - right["h"] / 2,
                       width=right["w"], height=right["h"], preserveAspectRatio=True, mask="auto")

    if header_markup:
        avail = geom["header_avail"]
        x0 = _MARGIN_X + (left["w"] + _LOGO_GAP if left else 0)
        x1 = w - _MARGIN_X - (right["w"] + _LOGO_GAP if right else 0)
        center_x = (x0 + x1) / 2
        para = Paragraph(header_markup, _header_style())
        _, ph = para.wrap(avail, geom["band_h"])
        para.drawOn(canv, center_x - avail / 2, band_center - ph / 2)

    if footer_markup:
        para = Paragraph(footer_markup, _footer_style)
        _, ph = para.wrap(w - 2 * _MARGIN_X, geom["footer_h"])
        para.drawOn(canv, _MARGIN_X, geom["footer_bottom"])

    canv.setFont("Helvetica", 8)
    canv.setFillColor(_GRAY)
    canv.drawString(_MARGIN_X, _BOTTOM_EDGE, export_dt)
    canv.setFillColor(colors.black)


class NumberedCanvas(canvas.Canvas):
    """Deferred page save so the footer can show ``page / total``."""

    _page_x = A4[0] - _MARGIN_X
    _page_y = _BOTTOM_EDGE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states = []

    def showPage(self):
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self.setFont("Helvetica", 8)
            self.setFillColor(_GRAY)
            self.drawRightString(self._page_x, self._page_y,
                                 f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _pick_pagesize(layout, sections):
    """A4 in the configured orientation; ``auto`` goes landscape only when a table
    needs more than portrait's usable width."""
    if layout.orientation == ResultsPdfLayout.Orientation.LANDSCAPE:
        return landscape(A4)
    if layout.orientation == ResultsPdfLayout.Orientation.PORTRAIT:
        return A4
    portrait_usable = A4[0] - 2 * _MARGIN_X
    widest = max((sum(w for _, w in _columns(s["layout"])) for s in sections), default=0)
    # ~2.0 pt per weight unit is a comfortable minimum column scale.
    return landscape(A4) if widest * 2.0 > portrait_usable else A4


def render_results_pdf(competition, layout, sections):
    """Return the PDF bytes for a list of result ``sections`` under this
    competition's saved ``ResultsPdfLayout``."""
    pagesize = _pick_pagesize(layout, sections)
    page_w, page_h = pagesize
    content_w = page_w - 2 * _MARGIN_X

    # Logos (shared across pages), each at its configured height.
    left = _logo_entry(layout.image_left, layout.image_left_height)
    right = _logo_entry(layout.image_right, layout.image_right_height)

    # Header text is centred between the logos; measure it at that reduced width.
    header_avail = content_w
    if left:
        header_avail -= left["w"] + _LOGO_GAP
    if right:
        header_avail -= right["w"] + _LOGO_GAP
    header_avail = max(header_avail, content_w * 0.4)

    hstyle = _header_style()
    section_markup = []
    header_h_max = footer_h_max = 0
    for section in sections:
        resolver = partial(
            pdfmarkup.resolve_wildcards, competition=competition,
            layout=layout, class_label=section.get("class_label", ""),
        )
        hmarkup = pdfmarkup.to_reportlab_markup(layout.header_html, resolver)
        fmarkup = pdfmarkup.to_reportlab_markup(layout.footer_html, resolver)
        if hmarkup:
            _, hh = Paragraph(hmarkup, hstyle).wrap(header_avail, page_h)
            header_h_max = max(header_h_max, hh)
        if fmarkup:
            _, fh = Paragraph(fmarkup, _footer_style).wrap(content_w, page_h)
            footer_h_max = max(footer_h_max, fh)
        section_markup.append((hmarkup, fmarkup))

    # Vertical geometry. The top band holds the logos and the header text side by
    # side, sized to whichever is tallest.
    top_pad = 8 * mm
    band_h = max(header_h_max, left["h"] if left else 0, right["h"] if right else 0)
    top_margin = top_pad + band_h + 7 * mm
    info_band = _BOTTOM_EDGE + 4 * mm
    footer_bottom = info_band + (3 * mm if footer_h_max else 0)
    bottom_margin = footer_bottom + footer_h_max + 5 * mm

    geom = {
        "pagesize": pagesize, "left": left, "right": right,
        "top_pad": top_pad, "band_h": band_h, "header_avail": header_avail,
        "footer_bottom": footer_bottom, "footer_h": footer_h_max,
    }
    # Export stamp in the computer's local timezone (not the app's stored TZ).
    export_dt = "Exported " + datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")

    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=pagesize, leftMargin=_MARGIN_X, rightMargin=_MARGIN_X,
        topMargin=top_margin, bottomMargin=bottom_margin,
        title="Results", author="Slalom Timing",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, content_w,
                  page_h - top_margin - bottom_margin, id="body",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    templates = []
    for i, (hmarkup, fmarkup) in enumerate(section_markup):
        templates.append(PageTemplate(
            id=f"sec{i}", frames=[frame],
            onPage=partial(_draw_page, geom=geom, header_markup=hmarkup,
                           footer_markup=fmarkup, export_dt=export_dt),
        ))
    doc.addPageTemplates(templates)
    NumberedCanvas._page_x = page_w - _MARGIN_X

    story = []
    for i, section in enumerate(sections):
        if i > 0:
            story.append(NextPageTemplate(f"sec{i}"))
            story.append(PageBreak())
        story.extend(_section_flowables(section, content_w))
    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def _logo_entry(image_field, height_mm):
    """``{reader, w, h}`` for an uploaded logo at its configured height, or None."""
    reader = _reader(image_field)
    if reader is None:
        return None
    size = _logo_draw_size(reader, height_mm)
    if size is None:
        return None
    return {"reader": reader, "w": size[0], "h": size[1]}


def _reader(image_field):
    """An ImageReader for an uploaded logo (saved file or a just-uploaded one), or
    None when unset/unreadable."""
    if not image_field:
        return None
    try:
        return ImageReader(image_field.path)
    except (ValueError, OSError, NotImplementedError):
        pass
    try:
        image_field.seek(0)
        return ImageReader(BytesIO(image_field.read()))
    except (ValueError, OSError, AttributeError):
        return None
