from django import forms

from apps.competitions.models import Competition

from .models import EventEntry, Participant


class ParticipantForm(forms.ModelForm):
    class Meta:
        model = Participant
        fields = [
            "competition_type",
            "first_name",
            "last_name",
            "date_of_birth",
            "address_street",
            "address_zip_code",
            "address_city",
            "club",
            "license_number",
            "email",
            "phone_number",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            # autocomplete off: these fields get custom dropdowns (club memory
            # base, email-domain completion) that the browser's native autofill
            # would otherwise overlap.
            "club": forms.TextInput(attrs={"autocomplete": "off"}),
            "email": forms.EmailInput(attrs={"autocomplete": "off"}),
        }
        labels = {
            "address_street": "Street address",
            "address_zip_code": "ZIP code",
            "address_city": "City",
        }


class ParticipantCreateForm(ParticipantForm):
    bib_number = forms.IntegerField(
        required=False, min_value=1, label="Bib number",
        help_text="Optional — assigns this participant a bib for the current competition right away.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition = Competition.get_current()
        if self.competition is None:
            self.fields["bib_number"].disabled = True
            self.fields["bib_number"].help_text = (
                "No competition is currently selected, so a bib can't be assigned yet."
            )

    def clean(self):
        cleaned_data = super().clean()
        bib_number = cleaned_data.get("bib_number")
        if bib_number is None or self.competition is None:
            return cleaned_data
        competition_type = cleaned_data.get("competition_type")
        if competition_type and competition_type != self.competition.competition_type:
            self.add_error(
                "bib_number",
                "This participant's type doesn't match the current competition, so a bib can't be assigned.",
            )
        elif EventEntry.objects.filter(competition=self.competition, bib_number=bib_number).exists():
            self.add_error("bib_number", "This bib number is already taken in the current competition.")
        return cleaned_data


class ParticipantUpdateForm(ParticipantForm):
    bib_number = forms.IntegerField(required=False, min_value=1, label="Bib number")

    def __init__(self, *args, competition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.competition = competition
        self.entry = None

        if competition is None:
            self._disable_bib_field("No competition is currently selected, so a bib can't be assigned.")
            return

        if self.instance.pk and self.instance.competition_type_id == competition.competition_type_id:
            self.entry = EventEntry.objects.filter(
                participant=self.instance, competition=competition
            ).first()
            if self.entry:
                self.fields["bib_number"].initial = self.entry.bib_number
        else:
            self._disable_bib_field(
                "This participant's type doesn't match the current competition, so a bib can't be assigned."
            )

    def _disable_bib_field(self, reason):
        self.fields["bib_number"].disabled = True
        self.fields["bib_number"].help_text = reason

    def clean(self):
        cleaned_data = super().clean()
        bib_number = cleaned_data.get("bib_number")
        if bib_number is not None and self.competition is not None:
            conflict = EventEntry.objects.filter(competition=self.competition, bib_number=bib_number)
            if self.entry:
                conflict = conflict.exclude(pk=self.entry.pk)
            if conflict.exists():
                self.add_error("bib_number", "This bib number is already taken in the current competition.")
        return cleaned_data
