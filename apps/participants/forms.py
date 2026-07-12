from django import forms

from .models import EventEntry, Participant


class ParticipantForm(forms.ModelForm):
    class Meta:
        model = Participant
        fields = [
            "competition_type",
            "first_name",
            "last_name",
            "gender",
            "date_of_birth",
            "category",
            "club",
            "nationality",
            "notes",
        ]
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"})}


class EventEntryForm(forms.ModelForm):
    class Meta:
        model = EventEntry
        fields = ["bib_number", "status"]

    def __init__(self, *args, participant, competition, **kwargs):
        super().__init__(*args, **kwargs)
        self.participant = participant
        self.competition = competition
        self.instance.participant = participant
        self.instance.competition = competition

    def clean(self):
        cleaned_data = super().clean()
        if self.participant.competition_type_id != self.competition.competition_type_id:
            raise forms.ValidationError(
                "This participant belongs to a different competition type than this event."
            )
        bib_number = cleaned_data.get("bib_number")
        if bib_number is not None:
            conflict = EventEntry.objects.filter(
                competition=self.competition, bib_number=bib_number
            ).exclude(pk=self.instance.pk)
            if conflict.exists():
                self.add_error("bib_number", "This bib number is already taken in this competition.")
        return cleaned_data
