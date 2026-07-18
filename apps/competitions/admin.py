from django.contrib import admin

from .models import Competition, CompetitionClass, CompetitionType, MarshalPost


@admin.register(CompetitionType)
class CompetitionTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "penalties_enabled", "tie_break", "timing_precision")
    list_filter = ("penalties_enabled", "tie_break", "timing_precision")
    search_fields = ("name",)
    fieldsets = (
        (None, {"fields": ("name",)}),
        ("Penalties", {"fields": ("penalties_enabled", *CompetitionType.PENALTY_FIELDS)}),
        ("Evaluation", {"fields": ("tie_break", "timing_precision")}),
        ("Required participant info", {"fields": tuple(CompetitionType.PARTICIPANT_INFO)}),
    )


class CompetitionClassInline(admin.TabularInline):
    model = CompetitionClass
    extra = 0
    can_delete = True
    fields = (
        "name", "position", "is_running", "age_from", "age_to",
        "practice_runs", "counted_runs", "run_position",
    )
    ordering = ("position",)


class MarshalPostInline(admin.TabularInline):
    model = MarshalPost
    extra = 0
    can_delete = True
    fields = ("number", "tasks", "handles_stop_line")
    ordering = ("number",)


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "competition_type", "date", "classes_running", "is_active")
    list_filter = ("competition_type", "date", "is_active")
    search_fields = ("name",)
    ordering = ("-date",)
    inlines = [CompetitionClassInline, MarshalPostInline]

    @admin.display(description="Classes")
    def classes_running(self, obj):
        return ", ".join(obj.active_classes()) or "—"
