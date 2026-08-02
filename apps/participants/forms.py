from django import forms
from django.utils.translation import gettext_lazy as _

from apps.common import DateInput
from apps.competitions.models import Competition, CompetitionType

from . import draw
from .models import DrawNumber, EventEntry, Participant


class ParticipantForm(forms.ModelForm):
    # The number drawn at the registration desk. Sits beside the bib rather than
    # replacing it: a drawn number decides what bib you get, a bib typed here is
    # the operator overriding that, and both screens show the two together.
    draw_number = forms.IntegerField(
        required=False, min_value=1, label=_("Draw number"),
        help_text=_("The number this participant drew at registration. Bibs are handed "
                    "out from it per class on the Bib assignment page."),
    )

    # Class-assignment state prepared by setup_classes() and consumed by the
    # template and the view. "classes" itself is not a Django field — a manual
    # multi-entry selection is a repeatable list of pks, which a set-based field
    # can't hold — so it's read from the raw POST and validated in clean().
    class_mode = "disabled"          # "manual" | "age" | "disabled"
    running_classes = ()
    initial_classes = ()
    allow_multiple_classes = False
    selected_classes = None          # None => don't touch assignments on save

    # {Participant field: is_mandatory} for this participant's type. Fields the
    # type doesn't collect are absent; the template renders only collected ones.
    field_requirements = {}
    # The PARTICIPANT_INFO setting names this type collects (co_driver, …), for
    # the template to decide which groups to render.
    collected_info = frozenset()
    competition = None

    def setup_type_fields(self):
        """Mark the type-optional fields required/not per this participant's
        competition type. Every field stays on the form (so the template can
        render the collected ones), but only the collected ones are required or
        saved; the rest are blanked on save by _clear_uncollected_fields."""
        competition_type = self._selected_type()
        self.field_requirements = (
            competition_type.participant_field_requirements() if competition_type else {}
        )
        self.collected_info = frozenset(
            setting
            for setting in CompetitionType.PARTICIPANT_INFO
            if competition_type and getattr(competition_type, setting)
        )
        for name in CompetitionType.optional_participant_fields():
            self.fields[name].required = self.field_requirements.get(name, False)

    def _selected_type(self):
        """The type this participant is registered under. It is never chosen on
        the form: an existing participant keeps its own type, and a new one takes
        the active competition's type. (The participant list only ever surfaces
        participants of the active competition's type, so an edit's own type and
        the active type coincide in practice.)"""
        if self.instance.pk:
            return self.instance.competition_type
        return self.competition.competition_type if self.competition else None

    def _clear_uncollected_fields(self, cleaned):
        """Blank anything the selected type doesn't collect, so a value typed
        before the type was switched can't be saved through a hidden field."""
        for name in CompetitionType.optional_participant_fields():
            if name not in self.field_requirements:
                cleaned[name] = ""
                self.errors.pop(name, None)

    def setup_classes(self, competition):
        self.competition = competition
        if competition is None:
            self.class_mode = "disabled"
            return
        method = competition.assignment()
        if not method.manual:
            self.class_mode = "age"   # display-only; nothing is stored
            return
        self.class_mode = "manual"
        self.running_classes = list(
            competition.classes.filter(is_running=True).order_by("position", "name")
        )
        self.allow_multiple_classes = competition.allows_multiple_classes_effective()
        if self.instance.pk:
            self.initial_classes = [
                a.competition_class
                for a in self.instance.class_assignments.select_related("competition_class")
                if a.competition_class.competition_id == competition.id
            ]

    def _resolve_class_selection(self, cleaned):
        """Validate the raw ``classes`` pk list (with duplicates) against the
        distinct-class and per-class repeat rules, storing the result on
        ``selected_classes`` for the view to persist."""
        self.selected_classes = None
        if self.class_mode != "manual":
            return
        ctype = self._selected_type()
        if ctype is None or ctype.pk != self.competition.competition_type_id:
            return  # type mismatch → leave any existing assignments untouched
        # Valid targets: running classes plus any already-assigned (possibly now
        # non-running) class, so resubmitting doesn't silently drop them.
        valid = {c.pk: c for c in self.running_classes}
        repeatable = {c.pk for c in self.running_classes if c.allow_multiple_entries}
        for cc in self.initial_classes:
            valid.setdefault(cc.pk, cc)
            if cc.allow_multiple_entries:
                repeatable.add(cc.pk)

        raw = self.data.getlist("classes") if hasattr(self.data, "getlist") else []
        chosen, counts, distinct = [], {}, set()
        for token in raw:
            try:
                pk = int(token)
            except (TypeError, ValueError):
                continue
            cc = valid.get(pk)
            if cc is None:
                continue
            counts[pk] = counts.get(pk, 0) + 1
            if counts[pk] > 1 and pk not in repeatable:
                self.add_error(None, _("“%(name)s” can’t be added more than once.") % {"name": cc.name})
                continue
            if pk not in distinct and distinct and not self.allow_multiple_classes:
                self.add_error(None, _("Only one class can be assigned to this participant."))
                continue
            distinct.add(pk)
            chosen.append(cc)
        self.selected_classes = chosen

    # --- drawn numbers (issue #11) -------------------------------------------
    #
    # The field is on the base form so both the add and the edit screen get it
    # in the same place, and is *removed* rather than hidden when the
    # competition does not draw numbers: a field nobody can see but the POST
    # still accepts is a field that can still be set.

    uses_draw_numbers = False

    def setup_draw_number(self, competition):
        """Show the drawn-number field, or take it off the form entirely."""
        self.uses_draw_numbers = bool(competition and competition.uses_draw_numbers)
        if not self.uses_draw_numbers:
            self.fields.pop("draw_number", None)
            return
        if self.instance.pk:
            existing = DrawNumber.objects.filter(
                competition=competition, participant=self.instance
            ).first()
            if existing:
                self.fields["draw_number"].initial = existing.number

    def _clean_draw_number(self, cleaned):
        number = cleaned.get("draw_number")
        if not self.uses_draw_numbers or number is None or self.competition is None:
            return
        holder = draw.draw_number_taken_by(
            self.competition, number,
            exclude_participant=self.instance if self.instance.pk else None,
        )
        if holder is not None:
            self.add_error("draw_number", _(
                "Draw number %(number)s already belongs to %(name)s."
            ) % {"number": number, "name": holder})

    def _clean_closed_classes(self, cleaned):
        """Once a class's bibs have been drawn there is no draw left to join, so
        somebody added to it afterwards has to be given a bib by hand.

        Only classes they are being *added* to are checked. A class they were
        already in was part of the draw, and refusing to save a corrected phone
        number because of it would make the whole screen unusable for the rest
        of the event.
        """
        if self.competition is None or not self.competition.uses_draw_numbers:
            return
        if cleaned.get("bib_number"):
            return
        closed = draw.closed_classes(self.competition)
        if not closed:
            return
        if self.class_mode == "manual":
            chosen = self.selected_classes or []
            # setup_classes only fills these for manual assignment.
            already = {cc.pk for cc in self.initial_classes}
        else:
            # Age assignment keeps no assignment rows, so "were they already in
            # it?" is asked of the class their *stored* date of birth resolves
            # to. `self.instance` still holds the database values here — a
            # ModelForm does not write the posted ones onto it until
            # `_post_clean`, which runs after this.
            born = cleaned.get("date_of_birth") or self.instance.date_of_birth
            cc = (self.competition.class_for_birth_year(born.year) if born else None)
            chosen = [cc] if cc else []
            was = (
                self.competition.class_for_birth_year(
                    self.instance.date_of_birth.year)
                if self.instance.pk and self.instance.date_of_birth else None
            )
            already = {was.pk} if was else set()
        blocked = [cc for cc in chosen if cc.pk in closed and cc.pk not in already]
        for cc in dict.fromkeys(blocked):
            self.add_error("bib_number", _(
                "Bibs for %(name)s have already been drawn, so a bib has to be "
                "entered here by hand."
            ) % {"name": cc.display_name()})

    def clean(self):
        cleaned = super().clean()
        self._clear_uncollected_fields(cleaned)
        self._resolve_class_selection(cleaned)
        self._clean_draw_number(cleaned)
        self._clean_closed_classes(cleaned)
        return cleaned

    class Meta:
        model = Participant
        fields = [
            "first_name",
            "last_name",
            "date_of_birth",
            "co_driver_first_name",
            "co_driver_last_name",
            "vehicle",
            "address_street",
            "address_zip_code",
            "address_city",
            "club",
            "license_number",
            "email",
            "phone_number",
        ]
        widgets = {
            # apps.common.DateInput, not forms.DateInput: a native date picker
            # only reads an ISO value and drops a localised one, which is how an
            # existing birthday came up empty on a German page.
            "date_of_birth": DateInput(),
            # autocomplete off: these fields get custom dropdowns (club memory
            # base, email-domain completion) that the browser's native autofill
            # would otherwise overlap.
            "club": forms.TextInput(attrs={"autocomplete": "off"}),
            "email": forms.EmailInput(attrs={"autocomplete": "off"}),
        }
        labels = {
            "co_driver_first_name": _("Co-driver first name"),
            "co_driver_last_name": _("Co-driver last name"),
            "address_street": _("Street address"),
            "address_zip_code": _("ZIP code"),
            "address_city": _("City"),
            "email": _("E-Mail"),
        }


class ParticipantCreateForm(ParticipantForm):
    bib_number = forms.IntegerField(
        required=False, min_value=1, label=_("Bib number"),
        help_text=_("Optional — assigns this participant a bib for the current competition right away."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition = Competition.get_current()
        # A new participant always takes the active competition's type — there is
        # no case for registering someone under a different discipline.
        if self.competition is not None:
            self.instance.competition_type = self.competition.competition_type
        self.setup_type_fields()
        self.setup_classes(self.competition)
        self.setup_draw_number(self.competition)
        if self.competition is None:
            self.fields["bib_number"].disabled = True
            self.fields["bib_number"].help_text = (
                _("No competition is currently selected, so a bib can't be assigned yet.")
            )
        elif self.uses_draw_numbers:
            # With a draw running, typing a bib here is the exception rather
            # than the routine — say so where the operator is looking.
            self.fields["bib_number"].help_text = _(
                "Optional — leave empty to let the draw decide, or set a bib here to "
                "keep this participant out of the automatic assignment."
            )

    def clean(self):
        cleaned_data = super().clean()
        bib_number = cleaned_data.get("bib_number")
        if bib_number is None or self.competition is None:
            return cleaned_data
        if EventEntry.objects.filter(competition=self.competition, bib_number=bib_number).exists():
            self.add_error("bib_number", _("This bib number is already taken in the current competition."))
        return cleaned_data


class ParticipantUpdateForm(ParticipantForm):
    bib_number = forms.IntegerField(required=False, min_value=1, label=_("Bib number"))

    def __init__(self, *args, competition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition = competition
        self.entry = None
        self.setup_type_fields()
        self.setup_classes(competition)
        self.setup_draw_number(competition)

        if competition is None:
            self._disable_bib_field(_("No competition is currently selected, so a bib can't be assigned."))
            return

        if self.instance.pk and self.instance.competition_type_id == competition.competition_type_id:
            self.entry = EventEntry.objects.filter(
                participant=self.instance, competition=competition
            ).first()
            if self.entry:
                self.fields["bib_number"].initial = self.entry.bib_number
        else:
            self._disable_bib_field(
                _("This participant's type doesn't match the current competition, so a bib can't be assigned.")
            )

    def _disable_bib_field(self, reason):
        self.fields["bib_number"].disabled = True
        self.fields["bib_number"].help_text = reason
        # Whatever stops a bib being assigned stops a number being drawn for the
        # same reason — both belong to the competition this participant is not
        # (or not yet) part of.
        if "draw_number" in self.fields:
            self.fields["draw_number"].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        bib_number = cleaned_data.get("bib_number")
        if bib_number is not None and self.competition is not None:
            conflict = EventEntry.objects.filter(competition=self.competition, bib_number=bib_number)
            if self.entry:
                conflict = conflict.exclude(pk=self.entry.pk)
            if conflict.exists():
                self.add_error("bib_number", _("This bib number is already taken in the current competition."))
        return cleaned_data
