from django.contrib import admin

from .models import MarshalPenalty, TimedRun, TimingEvent, TimingSettings, TimingSignal


@admin.register(TimingEvent)
class TimingEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "channel", "bib_number", "participant", "connector")
    list_filter = ("channel", "connector")
    search_fields = ("bib_number",)
    ordering = ("-received_at",)


@admin.register(TimingSettings)
class TimingSettingsAdmin(admin.ModelAdmin):
    list_display = ("device", "start_channel", "finish_channel", "ip_address")


@admin.register(TimingSignal)
class TimingSignalAdmin(admin.ModelAdmin):
    list_display = ("received_at", "competition", "running_number", "port", "is_manual", "ignored", "device_time")
    list_filter = ("is_manual", "ignored", "source")
    ordering = ("-received_at",)


@admin.register(TimedRun)
class TimedRunAdmin(admin.ModelAdmin):
    list_display = ("competition", "bib_number", "competition_class", "run_type", "run_number",
                    "pylon_count", "task_count", "stopline_count")
    list_filter = ("competition", "run_type")


@admin.register(MarshalPenalty)
class MarshalPenaltyAdmin(admin.ModelAdmin):
    list_display = ("timed_run", "marshal_post", "pylon_count", "task_count",
                    "stopline_count", "submitted", "updated_at")
    list_filter = ("submitted", "marshal_post")
