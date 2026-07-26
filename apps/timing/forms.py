from django import forms
from django.utils.translation import gettext_lazy as _

from .models import TimingSettings


class TimingSettingsForm(forms.ModelForm):
    """The local timing rig setup. The IP address and port are only collected for
    a networked device (the CP540); the simulator needs neither, and the form
    clears stale values when the device is switched away from the CP540."""

    class Meta:
        model = TimingSettings
        fields = ["device", "start_channel", "finish_channel", "ip_address", "port"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("start_channel", "finish_channel"):
            # Every supported device has inputs 1–4; a channel outside that can
            # never match a signal, so the browser refuses it and the model's own
            # validators refuse it again on a post that skips the browser.
            self.fields[name].widget.attrs.update({
                "min": str(TimingSettings.MIN_CHANNEL),
                "max": str(TimingSettings.MAX_CHANNEL),
                "inputmode": "numeric",
            })
        self.fields["port"].widget.attrs.update({"min": "1", "max": "65535", "inputmode": "numeric"})

    def clean(self):
        cleaned = super().clean()
        # The CP540 needs both to connect; other devices don't, but the values are
        # kept regardless so they survive a switch away and back.
        if cleaned.get("device") == TimingSettings.Device.CP540:
            if not cleaned.get("ip_address"):
                self.add_error("ip_address", _("Required to reach the Tag Heuer CP540."))
            if not cleaned.get("port"):
                self.add_error("port", _("Required to reach the Tag Heuer CP540."))
        return cleaned
