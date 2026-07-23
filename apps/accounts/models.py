from django.contrib.auth.models import Group
from django.db import models

from . import pages


class RoleAccess(models.Model):
    """The allowed-pages set for a role. A role is a Django Group; this one-to-one
    row carries which of the coarse page keys (apps.accounts.pages.PAGE_KEYS) that
    role grants. A user's access is the union of their groups' page sets."""

    group = models.OneToOneField(
        Group, on_delete=models.CASCADE, related_name="access"
    )
    pages = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"{self.group.name} access"

    def page_labels(self):
        """The granted pages as human labels, in registry order (for display)."""
        granted = set(self.pages or [])
        return [label for key, label in pages.PAGES if key in granted]
