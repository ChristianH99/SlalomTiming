"""Registering a list of participants for the active competition from a CSV.

A different job from the archive import next door: no pk remapping and no merge
wizard, because a spreadsheet is written by a human rather than exported by this
system. What it does need is to be *unforgiving up front* — the file is checked
in full and imported only if every row is good, so a half-registered field can't
happen. Every complaint carries the spreadsheet line number the operator sees.

Which columns the file must have is not fixed: it follows the active
competition's type, exactly like the participant form does
(CompetitionType.PARTICIPANT_INFO). A discipline that doesn't collect phone
numbers neither needs nor accepts that column.

A participant already registered under this type with the same name and date of
birth is *reused* rather than duplicated — the same rule the participant list
works by. Bibs are optional: rows without one are given the next free number.
"""

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils.translation import gettext as _

from apps.competitions.models import CompetitionType
from apps.participants.models import EventEntry, Participant

from .merge import FIELD_LABELS
from .schema import PARTICIPANT_IDENTITY_FIELDS

BIB_COLUMN = "bib_number"

# The header row a spreadsheet actually arrives with is never quite the one we
# asked for, so headers are normalised (case, spaces, punctuation) and these
# spellings are accepted for the canonical column names.
ALIASES = {
    "firstname": "first_name",
    "forename": "first_name",
    "givenname": "first_name",
    "lastname": "last_name",
    "surname": "last_name",
    "familyname": "last_name",
    "dob": "date_of_birth",
    "birthdate": "date_of_birth",
    "dateofbirth": "date_of_birth",
    "birthday": "date_of_birth",
    "licence_number": "license_number",
    "licence": "license_number",
    "license": "license_number",
    "mail": "email",
    "e_mail": "email",
    "phone": "phone_number",
    "telephone": "phone_number",
    "mobile": "phone_number",
    "street": "address_street",
    "address": "address_street",
    "zip": "address_zip_code",
    "zip_code": "address_zip_code",
    "postcode": "address_zip_code",
    "postal_code": "address_zip_code",
    "city": "address_city",
    "town": "address_city",
    "bib": BIB_COLUMN,
    "start_number": BIB_COLUMN,
    "startnumber": BIB_COLUMN,
    "number": BIB_COLUMN,
}

# Date formats accepted, in the order tried: ISO first, then the German and
# slash-separated forms a spreadsheet in this part of the world produces.
DATE_FORMATS = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"]

# What each column looks like in the downloadable sample.
EXAMPLES = {
    "first_name": ["Max", "Erika"],
    "last_name": ["Mustermann", "Musterfrau"],
    "date_of_birth": ["2010-04-23", "2011-09-05"],
    "license_number": ["L-12345", "L-12346"],
    "co_driver_first_name": ["Anna", ""],
    "co_driver_last_name": ["Mustermann", ""],
    "vehicle": ["Kart 125 ccm", "Kart 125 ccm"],
    "address_street": ["Hauptstraße 1", "Bahnhofweg 12"],
    "address_zip_code": ["12345", "12347"],
    "address_city": ["Musterstadt", "Beispieldorf"],
    "club": ["MSC Musterstadt", "MSC Beispieldorf"],
    "email": ["max@example.org", "erika@example.org"],
    "phone_number": ["+49 160 1234567", "+49 160 7654321"],
    BIB_COLUMN: ["1", "2"],
}

# The sample is written with semicolons: that is what a German-locale Excel opens
# into columns without an import dialog. Reading accepts , ; and tab alike.
SAMPLE_DELIMITER = ";"


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    mandatory: bool

    @property
    def example(self):
        return EXAMPLES.get(self.key, [""])[0]


def columns_for(competition):
    """The columns a file for this competition may carry, in sample order: the
    always-collected identity, then whatever the type collects, then the bib."""
    columns = [
        Column("first_name", FIELD_LABELS["first_name"], True),
        Column("last_name", FIELD_LABELS["last_name"], True),
        Column("date_of_birth", FIELD_LABELS["date_of_birth"], True),
    ]
    ctype = competition.competition_type
    for setting, (_label, mandatory, fields) in CompetitionType.PARTICIPANT_INFO.items():
        if not getattr(ctype, setting):
            continue
        for name in fields:
            columns.append(Column(name, FIELD_LABELS.get(name, name), mandatory))
    columns.append(
        Column(BIB_COLUMN, _("Bib number"), False)
    )
    return columns


def sample_csv(competition):
    """A ready-to-fill example file for this competition's columns."""
    columns = columns_for(competition)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=SAMPLE_DELIMITER, lineterminator="\r\n")
    writer.writerow([column.key for column in columns])
    for index in range(2):
        writer.writerow([EXAMPLES.get(c.key, ["", ""])[index] for c in columns])
    # A BOM so Excel opens the file as UTF-8 and doesn't mangle the umlauts.
    return "﻿" + buffer.getvalue()


# --- reading -----------------------------------------------------------------


@dataclass
class Row:
    """One validated line, ready to register."""

    line: int
    values: dict
    bib: int = None


@dataclass
class Report:
    """The verdict on a file. ``ok`` only when nothing at all is wrong — a file
    with a single bad row is refused whole, so an operator never ends up with a
    partly registered start list they have to reconcile by hand."""

    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)      # [(line or None, message)]
    ignored_columns: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors and bool(self.rows)

    def fail(self, message, line=None):
        self.errors.append((line, message))


def _decode(raw):
    """Text from an uploaded spreadsheet, whatever it was saved as."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _delimiter(text):
    """Comma, semicolon or tab — whichever the header row uses most."""
    header = next((line for line in text.splitlines() if line.strip()), "")
    return max(",;\t", key=header.count)


def _normalize_header(name):
    cleaned = "".join(
        ch if ch.isalnum() else "_" for ch in (name or "").strip().lower()
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("_")
    return ALIASES.get(cleaned, ALIASES.get(cleaned.replace("_", ""), cleaned))


def _parse_date(value):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def read(raw, competition):
    """Check a whole uploaded file against *competition*, returning a Report."""
    report = Report()
    columns = columns_for(competition)
    by_key = {column.key: column for column in columns}
    mandatory = [column for column in columns if column.mandatory]

    text = _decode(raw)
    if not text.strip():
        report.fail(_("The file is empty."))
        return report

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text))
    try:
        raw_header = next(reader)
    except StopIteration:
        report.fail(_("The file is empty."))
        return report

    header = [_normalize_header(name) for name in raw_header]
    report.ignored_columns = sorted(
        {name for name in header if name and name not in by_key}
    )

    missing = [column for column in mandatory if column.key not in header]
    if missing:
        report.fail(
            _("The file is missing these required columns: %(columns)s.")
            % {"columns": ", ".join(f"{c.label} ({c.key})" for c in missing)}
        )
        return report

    duplicates = {name for name in header if name in by_key and header.count(name) > 1}
    if duplicates:
        report.fail(
            _("These columns appear more than once: %(columns)s.")
            % {"columns": ", ".join(sorted(duplicates))}
        )
        return report

    _read_rows(reader, header, by_key, mandatory, competition, report)
    if not report.rows and not report.errors:
        report.fail(_("The file has a header but no participants."))
    return report


def _read_rows(reader, header, by_key, mandatory, competition, report):
    taken_bibs = set(
        EventEntry.objects.filter(competition=competition).values_list("bib_number", flat=True)
    )
    entered = {
        _identity_of(entry.participant)
        for entry in EventEntry.objects.filter(competition=competition).select_related("participant")
    }
    seen_bibs = {}
    seen_people = {}

    for line, raw_row in enumerate(reader, start=2):  # line 1 is the header
        if not any((cell or "").strip() for cell in raw_row):
            continue  # a blank separator line is not an error

        values = {}
        for index, name in enumerate(header):
            if name in by_key:
                values[name] = (raw_row[index] if index < len(raw_row) else "").strip()

        blank = [c for c in mandatory if not values.get(c.key)]
        if blank:
            report.fail(
                # str(): the labels are lazily translated, and join needs real strings.
                _("Missing %(fields)s.") % {"fields": ", ".join(str(c.label) for c in blank)},
                line,
            )
            continue

        # Every check runs even once one has failed, so a single upload tells the
        # operator everything wrong with the file rather than making them fix it
        # one round-trip at a time. Only the identity check depends on the date
        # having parsed.
        row = Row(line=line, values=values)
        date_ok = _check_date(row, report)
        _check_email(row, by_key, report)
        if date_ok:
            _check_person(row, entered, seen_people, report)
        _check_bib(row, taken_bibs, seen_bibs, report)
        if date_ok:
            report.rows.append(row)


def _identity_of(participant):
    return tuple(
        _key(getattr(participant, name)) for name in PARTICIPANT_IDENTITY_FIELDS
    )


def _key(value):
    return " ".join(str(value).split()).casefold() if isinstance(value, str) else value


def _check_date(row, report):
    parsed = _parse_date(row.values["date_of_birth"])
    if parsed is None:
        report.fail(
            _("“%(value)s” is not a date the import understands (use 2010-04-23 "
              "or 23.04.2010).") % {"value": row.values["date_of_birth"]},
            row.line,
        )
        return False
    row.values["date_of_birth"] = parsed
    return True


def _check_email(row, by_key, report):
    address = row.values.get("email")
    if not address or "email" not in by_key:
        return
    try:
        validate_email(address)
    except ValidationError:
        report.fail(
            _("“%(value)s” is not a valid e-mail address.") % {"value": address}, row.line
        )


def _check_person(row, entered, seen_people, report):
    identity = tuple(_key(row.values[name]) for name in PARTICIPANT_IDENTITY_FIELDS)
    if identity in entered:
        report.fail(
            _("%(name)s is already entered in this competition.")
            % {"name": f"{row.values['first_name']} {row.values['last_name']}"},
            row.line,
        )
    elif identity in seen_people:
        report.fail(
            _("Same person as line %(line)s.") % {"line": seen_people[identity]}, row.line
        )
    else:
        seen_people[identity] = row.line


def _check_bib(row, taken_bibs, seen_bibs, report):
    raw_bib = row.values.get(BIB_COLUMN, "")
    if not raw_bib:
        return
    try:
        bib = int(raw_bib)
    except ValueError:
        report.fail(
            _("Bib “%(value)s” is not a whole number.") % {"value": raw_bib}, row.line
        )
        return
    if bib < 1:
        report.fail(_("Bib numbers start at 1."), row.line)
        return
    if bib in taken_bibs:
        report.fail(
            _("Bib %(bib)s is already taken in this competition.") % {"bib": bib}, row.line
        )
        return
    if bib in seen_bibs:
        report.fail(
            _("Bib %(bib)s is also used on line %(line)s.")
            % {"bib": bib, "line": seen_bibs[bib]},
            row.line,
        )
        return
    seen_bibs[bib] = row.line
    row.bib = bib


# --- registering -------------------------------------------------------------


@dataclass
class Result:
    created: int = 0
    reused: int = 0
    entries: int = 0
    auto_bibs: int = 0
    first_bib: int = None
    last_bib: int = None


@transaction.atomic
def commit(report, competition):
    """Register every row. Only call this on a report that is ``ok``."""
    result = Result()
    ctype = competition.competition_type
    fields = {column.key for column in columns_for(competition)} - {BIB_COLUMN}

    existing = {
        _identity_of(participant): participant
        for participant in Participant.objects.filter(competition_type=ctype)
    }
    used = set(
        EventEntry.objects.filter(competition=competition).values_list("bib_number", flat=True)
    )
    used.update(row.bib for row in report.rows if row.bib)
    next_free = _bib_allocator(used)

    bibs = []
    for row in report.rows:
        identity = tuple(_key(row.values[name]) for name in PARTICIPANT_IDENTITY_FIELDS)
        participant = existing.get(identity)
        if participant is None:
            participant = Participant.objects.create(
                competition_type=ctype,
                **{key: value for key, value in row.values.items() if key in fields},
            )
            existing[identity] = participant
            result.created += 1
        else:
            result.reused += 1

        bib = row.bib
        if bib is None:
            bib = next(next_free)
            result.auto_bibs += 1
        EventEntry.objects.create(
            participant=participant, competition=competition, bib_number=bib
        )
        result.entries += 1
        bibs.append(bib)

    if bibs:
        result.first_bib, result.last_bib = min(bibs), max(bibs)
    return result


def _bib_allocator(used):
    """Yield the lowest bib numbers nobody has, so a file without bibs fills the
    gaps in the existing start list rather than starting after it."""
    candidate = 1
    while True:
        while candidate in used:
            candidate += 1
        used.add(candidate)
        yield candidate
