from django.contrib import admin

from .models import Competition, CompetitionClass, CompetitionType


@admin.register(CompetitionType)
class CompetitionTypeAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


class CompetitionClassInline(admin.TabularInline):
    model = CompetitionClass
    extra = 0
    can_delete = True
    fields = (
        "name", "position", "is_running", "age_from", "age_to",
        "practice_runs", "counted_runs", "run_position",
    )
    ordering = ("position",)


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "competition_type", "date", "classes_running", "is_active")
    list_filter = ("competition_type", "date", "is_active")
    search_fields = ("name",)
    ordering = ("-date",)
    inlines = [CompetitionClassInline]

    @admin.display(description="Classes")
    def classes_running(self, obj):
        return ", ".join(obj.active_classes()) or "—"
