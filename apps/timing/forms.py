from django import forms

from .models import TimingSettings


class TimingSettingsForm(forms.ModelForm):
    """The local timing rig setup. The IP address is only collected for a
    networked device (the TP540); the simulator needs none, and the form clears
    a stale value when the device is switched away from the TP540."""

    class Meta:
        model = TimingSettings
        fields = ["device", "start_channel", "finish_channel", "ip_address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("start_channel", "finish_channel"):
            # Single digit only: the browser rejects anything outside 0–9.
            self.fields[name].widget.attrs.update({"min": "0", "max": "9", "inputmode": "numeric"})

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("device") == TimingSettings.Device.TP540:
            if not cleaned.get("ip_address"):
                self.add_error("ip_address", "Required to reach the Tag Heuer TP540.")
        else:
            # A device with no network address keeps none, even if one was typed
            # before the device was switched.
            cleaned["ip_address"] = None
        return cleaned
