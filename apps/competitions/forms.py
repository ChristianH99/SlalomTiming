from django import forms
from django.forms import modelformset_factory

from .models import Competition, CompetitionClass, CompetitionType


class CompetitionForm(forms.ModelForm):
    competition_type = forms.ModelChoiceField(
        queryset=CompetitionType.objects.all(), empty_label="Select a type…"
    )

    class Meta:
        model = Competition
        fields = ["competition_type", "name", "date"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}


class CompetitionTypeForm(forms.ModelForm):
    class Meta:
        model = CompetitionType
        fields = ["name"]


CompetitionClassFormSet = modelformset_factory(
    CompetitionClass,
    fields=["is_running", "age_from", "age_to"],
    extra=0,
    can_delete=False,
)
