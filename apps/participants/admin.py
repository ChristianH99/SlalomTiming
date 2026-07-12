from django.contrib import admin

from apps.competitions.models import Competition

from .models import EventEntry, Participant


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "first_name",
        "last_name",
        "competition_type",
        "date_of_birth",
        "current_class",
        "club",
        "license_number",
        "email",
    )
    list_filter = ("competition_type",)
    search_fields = ("first_name", "last_name", "club", "license_number", "email")
    ordering = ("last_name", "first_name")

    @admin.display(description="Class")
    def current_class(self, obj):
        competition = Competition.get_current()
        if not competition or not obj.date_of_birth:
            return "—"
        competition_class = competition.class_for_birth_year(obj.date_of_birth.year)
        return competition_class.get_code_display() if competition_class else "—"


@admin.register(EventEntry)
class EventEntryAdmin(admin.ModelAdmin):
    list_display = ("bib_number", "participant", "competition", "status")
    list_filter = ("competition", "status")
    search_fields = ("bib_number", "participant__first_name", "participant__last_name")
    ordering = ("competition", "bib_number")
