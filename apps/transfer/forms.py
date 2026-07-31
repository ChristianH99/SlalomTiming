from django import forms
from django.utils.translation import gettext_lazy as _

from .models import BackupSettings


class BackupSettingsForm(forms.ModelForm):
    """The backup destination and interval.

    The destination is checked here rather than only when the timer next fires:
    an operator who types a path and presses Save has told the app where the
    backups go, and finding out at the next tick — from a line on a page they
    have navigated away from — is not telling them.
    """

    class Meta:
        model = BackupSettings
        fields = ["enabled", "destination", "interval_minutes", "keep"]
        widgets = {
            "destination": forms.TextInput(attrs={
                "placeholder": r"E:\SlalomTiming-Backups",
                "spellcheck": "false",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interval_minutes"].widget.attrs.update({
            "min": str(BackupSettings.MIN_INTERVAL),
            "max": str(BackupSettings.MAX_INTERVAL),
            "inputmode": "numeric",
        })
        self.fields["keep"].widget.attrs.update({
            "min": str(BackupSettings.MIN_KEEP),
            "max": str(BackupSettings.MAX_KEEP),
            "inputmode": "numeric",
        })

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("enabled"):
            return cleaned
        if not (cleaned.get("destination") or "").strip():
            self.add_error("destination", _("Where should the copies go?"))
            return cleaned
        probe = BackupSettings(destination=cleaned["destination"].strip())
        problem = probe.destination_problem()
        if problem:
            self.add_error("destination", problem)
        return cleaned
