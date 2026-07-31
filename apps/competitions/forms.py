from django import forms
from django.forms import modelformset_factory
from django.utils.translation import gettext_lazy as _

from apps.common import DateInput

from .models import Competition, CompetitionClass, CompetitionType


class CompetitionForm(forms.ModelForm):
    competition_type = forms.ModelChoiceField(
        queryset=CompetitionType.objects.all(), empty_label=_("Select a type…")
    )

    class Meta:
        model = Competition
        fields = ["competition_type", "name", "date"]
        # apps.common.DateInput — see there; a localised value never reaches the
        # native picker, so the event's own date read as empty on the General page.
        widgets = {"date": DateInput()}


class CompetitionTypeForm(forms.ModelForm):
    class Meta:
        model = CompetitionType
        fields = ["name"]


class CompetitionTypeSettingsForm(forms.ModelForm):
    """The rules a discipline is run under. Penalty amounts are nullable on the
    model (they mean nothing with penalties off) but mandatory here whenever the
    penalties toggle is on."""

    class Meta:
        model = CompetitionType
        fields = [
            "name",
            "penalties_enabled",
            *CompetitionType.PENALTY_FIELDS,
            "tie_break",
            "timing_precision",
            *CompetitionType.PARTICIPANT_INFO,
        ]
        labels = {
            "pylon_penalty": _("Pylon"),
            "task_penalty": _("Task"),
            "stop_line_penalty": _("Stop line"),
            "max_penalty_per_task": _("Max per task"),
            **{
                setting: label
                for setting, (label, _mandatory, _fields) in CompetitionType.PARTICIPANT_INFO.items()
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in CompetitionType.PENALTY_FIELDS:
            # Whole seconds: step=1 makes the browser reject a typed decimal
            # rather than silently rounding it. The unit is rendered next to the
            # input by the template, so it stays out of the label.
            self.fields[name].widget.attrs.update({"step": "1", "min": "0", "inputmode": "numeric"})

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("penalties_enabled"):
            for name in CompetitionType.PENALTY_FIELDS:
                if cleaned.get(name) is None and name not in self.errors:
                    self.add_error(name, _("Required when penalties are enabled."))
        else:
            # Amounts entered before the toggle was turned off aren't kept: with
            # penalties off there is nothing for them to apply to.
            for name in CompetitionType.PENALTY_FIELDS:
                cleaned[name] = None
        return cleaned


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
