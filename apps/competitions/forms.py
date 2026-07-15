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


class AssignmentForm(forms.ModelForm):
    """Competition-level class settings on the Classes page: how participants are
    assigned to classes, and (manual only) whether they may be in several."""

    class Meta:
        model = Competition
        fields = ["assignment_method", "allow_multiple_classes"]

    def clean(self):
        cleaned = super().clean()
        from .assignment import get_assignment_method

        method = get_assignment_method(cleaned.get("assignment_method"))
        # A method that doesn't support multiple distinct classes forces the flag off.
        if not method.configurable_multiple:
            cleaned["allow_multiple_classes"] = False
        return cleaned


CompetitionClassFormSet = modelformset_factory(
    CompetitionClass,
    fields=[
        "name", "is_running", "age_from", "age_to",
        "practice_runs", "counted_runs", "scoring_method", "allow_multiple_entries",
    ],
    extra=0,
    can_delete=True,
)
