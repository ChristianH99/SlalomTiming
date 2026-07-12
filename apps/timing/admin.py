from django.contrib import admin

from .models import TimingEvent


@admin.register(TimingEvent)
class TimingEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "channel", "bib_number", "participant", "connector")
    list_filter = ("channel", "connector")
    search_fields = ("bib_number",)
    ordering = ("-received_at",)
