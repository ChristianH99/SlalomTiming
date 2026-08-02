"""Deciding what an imported participant means on *this* system.

The same person imported twice must not become two rows, and two different
people who happen to share a name must not be silently fused. So every incoming
participant is classified against what's already registered under the same
competition type:

* ``NEW``        — nobody recognisable; create them.
* ``IDENTICAL``  — an existing participant matches on identity *and* every
                   detail; reuse that row untouched, no operator decision.
* ``CONFLICT``   — someone recognisable, but the records disagree (or more than
                   one candidate matches). The operator resolves it: keep them
                   apart, or merge and pick a value per differing field.

"Recognisable" is deliberately two rules rather than one: the same name and date
of birth, or the same licence number. A licence number is the stronger identity
(it survives a misspelled name or a marriage), so it is offered as a candidate
even when the name doesn't line up — but never auto-merged, because the operator
is the one who can tell a typo from a different person.
"""

from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

from apps.participants.models import Participant

from . import schema

# How each comparable participant field is named to the operator. Spelled out
# rather than taken from the model's verbose_name so the merge screen reads in
# the operator's language (the app ships German by default).
FIELD_LABELS = {
    "first_name": _("First name"),
    "last_name": _("Last name"),
    "date_of_birth": _("Date of birth"),
    "license_number": _("Licence number"),
    "co_driver_first_name": _("Co-driver first name"),
    "co_driver_last_name": _("Co-driver last name"),
    "vehicle": _("Vehicle"),
    "address_street": _("Street address"),
    "address_zip_code": _("ZIP code"),
    "address_city": _("City"),
    "club": _("Club"),
    "email": _("E-Mail"),
    "phone_number": _("Phone"),
}

NEW = "new"
IDENTICAL = "identical"
CONFLICT = "conflict"

# What the operator may choose to do with one incoming participant.
ACTION_CREATE = "create"   # register them as a separate participant
ACTION_MERGE = "merge"     # fold them into an existing participant
ACTION_REUSE = "reuse"     # an identical match: use the existing row as-is

# Which side of a field disagreement wins.
KEEP_EXISTING = "existing"
KEEP_IMPORTED = "imported"


@dataclass
class Difference:
    """One field an existing participant and an incoming row disagree on."""

    field: str
    existing: object
    imported: object

    @property
    def label(self):
        return FIELD_LABELS.get(self.field, self.field)


@dataclass
class Candidate:
    """An existing participant an incoming row might be."""

    participant: Participant
    reason: str                       # "identity" | "license"
    diffs: list = field(default_factory=list)   # [Difference]

    @property
    def pk(self):
        return self.participant.pk

    @property
    def is_exact(self):
        return not self.diffs

    @property
    def reason_label(self):
        """Why this person was suggested — the operator needs to see it to judge
        a licence match whose name doesn't line up."""
        if self.reason == "license":
            return _("same licence number")
        return _("same name and date of birth")


@dataclass
class Match:
    """One incoming participant and what this system makes of them."""

    ref: int
    values: dict                      # typed Participant field values
    status: str
    candidates: list = field(default_factory=list)

    @property
    def name(self):
        return f"{self.values.get('first_name', '')} {self.values.get('last_name', '')}".strip()

    @property
    def exact(self):
        """The identical existing participant, when there is one."""
        return next((c for c in self.candidates if c.is_exact), None)

    @property
    def needs_decision(self):
        return self.status == CONFLICT

    def candidate(self, pk):
        return next((c for c in self.candidates if c.pk == pk), None)

    def default_resolution(self):
        """What happens if the operator changes nothing: reuse an identical
        match, merge into the single best candidate, otherwise create."""
        if self.status == IDENTICAL:
            return {"action": ACTION_REUSE, "target": self.exact.pk, "fields": {}}
        if self.status == CONFLICT:
            best = self.candidates[0]
            return {
                "action": ACTION_MERGE,
                "target": best.pk,
                # Imported data is what the operator just chose to bring in, so it
                # leads; every field is individually overridable in the UI.
                "fields": {diff.field: KEEP_IMPORTED for diff in best.diffs},
            }
        return {"action": ACTION_CREATE, "target": None, "fields": {}}


def _normalize(value):
    """Compare text case- and whitespace-insensitively; other types as-is. A
    blank string and None are the same absence of a value."""
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value if value is not None else ""


def _same(left, right):
    return _normalize(left) == _normalize(right)


def _diffs(participant, values):
    """Fields where an existing participant and an incoming row disagree."""
    return [
        Difference(name, getattr(participant, name), values[name])
        for name in schema.PARTICIPANT_FIELDS
        if name in values and not _same(getattr(participant, name), values[name])
    ]


def _identity(values):
    return tuple(_normalize(values.get(name)) for name in schema.PARTICIPANT_IDENTITY_FIELDS)


def match_participants(rows, competition_type):
    """Classify every incoming participant row against the participants already
    registered under *competition_type* (None — the type doesn't exist here yet —
    means everyone is new).

    Rows are matched against each other's outcomes too: two identical rows in one
    file both resolve to the same existing person rather than to two copies.
    """
    existing = (
        list(Participant.objects.filter(competition_type=competition_type))
        if competition_type is not None
        else []
    )
    by_identity = {}
    by_license = {}
    for participant in existing:
        by_identity.setdefault(
            _identity({f: getattr(participant, f) for f in schema.PARTICIPANT_IDENTITY_FIELDS}),
            [],
        ).append(participant)
        if participant.license_number.strip():
            by_license.setdefault(_normalize(participant.license_number), []).append(participant)

    matches = []
    for row in rows:
        values = schema.load(Participant, row, schema.PARTICIPANT_FIELDS)
        candidates = []
        seen = set()

        for participant in by_identity.get(_identity(values), []):
            seen.add(participant.pk)
            candidates.append(Candidate(participant, "identity", _diffs(participant, values)))

        license_number = values.get("license_number") or ""
        if license_number.strip():
            for participant in by_license.get(_normalize(license_number), []):
                if participant.pk in seen:
                    continue
                seen.add(participant.pk)
                candidates.append(Candidate(participant, "license", _diffs(participant, values)))

        if not candidates:
            status = NEW
        elif any(c.is_exact for c in candidates):
            # An exact match settles it even if a weaker candidate also turned up.
            status = IDENTICAL
            candidates.sort(key=lambda c: (not c.is_exact, c.pk))
        else:
            status = CONFLICT
            # Fewest disagreements first: the likeliest "same person" leads.
            candidates.sort(key=lambda c: (c.reason != "identity", len(c.diffs), c.pk))

        matches.append(Match(ref=row["ref"], values=values, status=status, candidates=candidates))

    return matches


def apply(match, resolution, competition_type):
    """Carry out one resolution and return the Participant the import should
    wire this row's entries, assignments and runs to."""
    action = resolution.get("action")

    if action == ACTION_REUSE:
        candidate = match.candidate(resolution.get("target"))
        if candidate is not None:
            return candidate.participant

    if action == ACTION_MERGE:
        candidate = match.candidate(resolution.get("target"))
        if candidate is not None:
            participant = candidate.participant
            choices = resolution.get("fields") or {}
            changed = []
            for diff in candidate.diffs:
                if choices.get(diff.field, KEEP_IMPORTED) == KEEP_IMPORTED:
                    setattr(participant, diff.field, diff.imported)
                    changed.append(diff.field)
            if changed:
                participant.save(update_fields=changed + ["updated_at"])
            return participant

    return Participant.objects.create(competition_type=competition_type, **match.values)


def read_resolutions(post, matches, leads=KEEP_IMPORTED):
    """The operator's per-participant decisions, read off a review form.

    Each conflict renders one radio group ``choice-<ref>`` — "create", or
    "merge:<pk>" naming the existing participant — plus, per candidate, a
    per-field group ``field-<ref>-<pk>-<name>`` choosing which side wins. The
    field groups are keyed by candidate so picking a different candidate cannot
    inherit the previous one's choices.

    Only conflicts render inputs, so anything absent keeps that match's default —
    which is exactly what an untouched review screen should mean.

    ``leads`` is which side a *missing* field answer falls back to, and the two
    callers genuinely differ. An **import** leads with the incoming values: the
    operator went and fetched that file, so it is the newer truth. **Duplicating
    an archived competition** leads with what is on file: those incoming values
    are however many years old, the record here is the current truth about a
    living person, and the archived event keeps its own copy either way. A shared
    reader with one hardcoded default would silently rewrite somebody's address
    from a 2019 event.

    It lives here rather than beside either screen because there are two screens
    now: the import wizard, and duplicating an archived competition
    (apps/competitions/duplication.py). They ask the same question about the same
    ``Match`` objects, and two readers of one form would be two chances to read
    it differently.
    """
    resolutions = {}
    for match in matches:
        if not match.needs_decision:
            continue

        choice = post.get(f"choice-{match.ref}") or ""
        if choice == ACTION_CREATE:
            resolutions[match.ref] = {"action": ACTION_CREATE, "target": None, "fields": {}}
            continue

        candidate = match.candidate(_as_int(choice.split(":", 1)[1])) if ":" in choice else None
        if candidate is None:
            continue  # unreadable choice: fall back to this match's default

        resolutions[match.ref] = {
            "action": ACTION_MERGE,
            "target": candidate.pk,
            "fields": {
                diff.field: _side(
                    post.get(f"field-{match.ref}-{candidate.pk}-{diff.field}"), leads
                )
                for diff in candidate.diffs
            },
        }
    return resolutions


def _side(answer, leads):
    """Which side one field answer picks; anything unreadable takes ``leads``."""
    return answer if answer in (KEEP_EXISTING, KEEP_IMPORTED) else leads


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summarize(matches):
    return {
        "total": len(matches),
        NEW: sum(1 for m in matches if m.status == NEW),
        IDENTICAL: sum(1 for m in matches if m.status == IDENTICAL),
        CONFLICT: sum(1 for m in matches if m.status == CONFLICT),
    }
