from django.contrib import admin

from .models import Competition, CompetitionClass, CompetitionType


@admin.register(CompetitionType)
class CompetitionTypeAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


class CompetitionClassInline(admin.TabularInline):
    model = CompetitionClass
    extra = 0
    max_num = 7
    can_delete = False
    fields = ("code", "is_running", "age_from", "age_to")
    readonly_fields = ("code",)
    ordering = ("code",)


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
