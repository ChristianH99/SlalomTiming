"""The transfer document: what an export file contains and how it is typed.

An export is a ``.zip`` holding ``data.json`` (this document) plus any binary
media the document points at (currently only the results-PDF logos), under
``media/``. The document is a plain dict of lists — no Django serialization
format — because an import has to *remap* every primary key onto the target
system's own, which Django's own deserializer explicitly does not do (it
restores the original pks and would clobber unrelated rows).

Every exported row therefore carries a ``ref``: its primary key on the *source*
system, used only as a local id to wire the document's rows to each other. The
importer builds ``ref -> new object`` maps and never writes a source pk.

Field values are encoded by ``DjangoJSONEncoder`` (dates, times and decimals
become ISO/­decimal strings) and decoded back through the model field's own
``to_python()``, so the two directions can never drift apart.
"""

FORMAT = "slalomtiming-export"
VERSION = 1

# Names inside the zip.
DATA_NAME = "data.json"
MEDIA_DIR = "media"

# What an export covers.
SCOPE_EVENT = "event"
SCOPE_TYPE = "competition_type"
SCOPES = (SCOPE_EVENT, SCOPE_TYPE)


# --- the fields carried per model -------------------------------------------
#
# Deliberately *not* every model field. Excluded throughout: primary keys (see
# `ref` above), auto timestamps that describe the source machine rather than the
# race (created_at/updated_at), and device-local state that must not travel —
# MarshalPost.claim_token/claim_seen (which device currently holds a post) and
# Competition.is_active (an import must never silently take over the running
# event). TimingSignal.received_at *is* carried: arrangement.py orders runs by
# arrival, so dropping it would reshuffle an imported event's timing.

COMPETITION_TYPE_FIELDS = [
    "name",
    "penalties_enabled",
    "pylon_penalty",
    "task_penalty",
    "stop_line_penalty",
    "max_penalty_per_task",
    "tie_break",
    "timing_precision",
    "requires_co_driver",
    "requires_vehicle",
    "requires_address",
    "requires_club",
    "requires_license",
    "requires_email",
    "requires_phone",
]

COMPETITION_FIELDS = [
    "name",
    "date",
    "assignment_method",
    "allow_multiple_classes",
    "penalties_by_marshal_posts",
    "start_pattern",
    "auto_timing_order",
]

CLASS_FIELDS = [
    "name",
    "position",
    "is_running",
    "age_from",
    "age_to",
    "practice_runs",
    "counted_runs",
    "scoring_method",
    "allow_multiple_entries",
    "run_position",
]

MARSHAL_POST_FIELDS = ["number", "tasks", "handles_stop_line"]

# The identity a participant is matched on when importing, and the details a
# merge lets the operator choose between. Together they are every Participant
# field an export carries (see apps/transfer/merge.py).
PARTICIPANT_IDENTITY_FIELDS = ["first_name", "last_name", "date_of_birth"]
PARTICIPANT_DETAIL_FIELDS = [
    "license_number",
    "co_driver_first_name",
    "co_driver_last_name",
    "vehicle",
    "address_street",
    "address_zip_code",
    "address_city",
    "club",
    "email",
    "phone_number",
]
PARTICIPANT_FIELDS = PARTICIPANT_IDENTITY_FIELDS + PARTICIPANT_DETAIL_FIELDS

ENTRY_FIELDS = ["bib_number", "status"]

SIGNAL_FIELDS = [
    "running_number",
    "port",
    "is_manual",
    "device_time",
    "source",
    "ignored",
    "entered",
    "received_at",
]

RUN_FIELDS = [
    "bib_number",
    "class_occurrence",
    "run_type",
    "run_number",
    "manual_entry",
    "manual_run_time",
    "pylon_count",
    "task_count",
    "stopline_count",
    "pylon_adjust",
    "task_adjust",
    "stopline_adjust",
]

MARSHAL_PENALTY_FIELDS = [
    "pylon_count",
    "task_count",
    "stopline_count",
    "detail",
    "submitted",
]

COLUMN_SETTINGS_FIELDS = ["columns", "show_overall"]

PDF_LAYOUT_FIELDS = [
    "header_html",
    "footer_html",
    "increment_start_year",
    "orientation",
    "image_left_height",
    "image_right_height",
]

TIE_RESOLUTION_FIELDS = ["scope", "members"]


class TransferError(Exception):
    """An archive that can't be read, or a document this version can't import."""


def dump(instance, field_names):
    """One row of the document: ``ref`` plus the named fields, straight off the
    instance. Typing is left to the JSON encoder."""
    row = {"ref": instance.pk}
    for name in field_names:
        row[name] = getattr(instance, name)
    return row


def load(model, row, field_names):
    """``**kwargs`` for building *model* from a document row, each value put back
    through the model field's own ``to_python()`` so an ISO string becomes a date
    again. Fields the row doesn't carry fall back to the model's default."""
    values = {}
    for name in field_names:
        if name not in row:
            continue
        values[name] = model._meta.get_field(name).to_python(row[name])
    return values
