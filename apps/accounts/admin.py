from django.contrib import admin

from .models import RoleAccess


@admin.register(RoleAccess)
class RoleAccessAdmin(admin.ModelAdmin):
    list_display = ("group", "pages")
