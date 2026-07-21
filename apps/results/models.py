from django.db import models

# The results column vocabulary. Each key -> (label, availability, group):
#   * availability: the CompetitionType flag that must be on for the column to be
#     offered; None means the detail is always collected (name / date of birth) and
#     "__training__" is the special case "any running class has practice runs".
#   * group: which fixed layout column the value renders into — a multi-line "name",
#     "address" or "licence" block, a single "vehicle" cell, or the "runs" region.
# The order here is the canonical column order the settings and tables follow.
RESULT_COLUMNS = {
    "driver_name": ("Driver name", None, "name"),
    "co_driver": ("Co-driver", "requires_co_driver", "name"),
    "club": ("Club", "requires_club", "name"),
    "email": ("E-Mail", "requires_email", "name"),
    "phone": ("Phone", "requires_phone", "name"),
    "street": ("Street", "requires_address", "address"),
    "city": ("City", "requires_address", "address"),
    "vehicle": ("Vehicle", "requires_vehicle", "vehicle"),
    "license": ("Licence number", "requires_license", "licence"),
    "birthday": ("Birthday", None, "licence"),
    "birth_year": ("Birth year", None, "licence"),
    "training": ("Training runs", "__training__", "runs"),
}


class ResultColumnSettings(models.Model):
    """Which optional participant-info columns a competition's results tables show.

    One *General* row per competition (``competition_class`` null) holds the default
    column set and the ``show_overall`` toggle; an optional row per class *adds* to
    the General set (a class can only enable columns General doesn't already show —
    its ``columns`` list is additions, never an override). The configurable columns
    are exactly the details a discipline collects (see ``RESULT_COLUMNS``) — Rank and
    Bib are always shown and never stored here."""

    competition = models.ForeignKey(
        "competitions.Competition", on_delete=models.CASCADE, related_name="result_columns"
    )
    # Null means the competition-wide *General* defaults; a class makes it an override.
    competition_class = models.ForeignKey(
        "competitions.CompetitionClass", null=True, blank=True,
        on_delete=models.CASCADE, related_name="result_columns",
    )
    columns = models.JSONField(default=list, blank=True)
    # General row only: whether the cross-class Overall pages are shown.
    show_overall = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competition"],
                condition=models.Q(competition_class__isnull=True),
                name="unique_general_result_columns",
            ),
            models.UniqueConstraint(
                fields=["competition", "competition_class"],
                name="unique_class_result_columns",
            ),
        ]

    def __str__(self):
        scope = self.competition_class.name if self.competition_class_id else "General"
        return f"{self.competition} – results columns ({scope})"

    # ----- column vocabulary -----

    @staticmethod
    def available_keys(competition):
        """The result-column keys this competition can show, in canonical order —
        each detail its type collects, plus a Training column when any running class
        has practice runs. Name and date-of-birth columns are always available."""
        ctype = competition.competition_type
        keys = []
        for key, (_, setting, _) in RESULT_COLUMNS.items():
            if setting is None:
                keys.append(key)
            elif setting == "__training__":
                if any(cc.practice_runs for cc in competition._running_classes_ordered()):
                    keys.append(key)
            elif getattr(ctype, setting):
                keys.append(key)
        return keys

    @staticmethod
    def label_for(key):
        return RESULT_COLUMNS[key][0]

    @staticmethod
    def group_for(key):
        return RESULT_COLUMNS[key][2]

    # ----- effective settings -----

    @classmethod
    def general_columns(cls, competition):
        """The General column set, restricted to what the type still collects.
        Defaults to every available column when no General row is saved yet."""
        available = cls.available_keys(competition)
        row = cls.objects.filter(
            competition=competition, competition_class__isnull=True
        ).first()
        if row is None:
            return list(available)
        return [key for key in available if key in (row.columns or [])]

    @classmethod
    def class_additions(cls, competition, competition_class):
        """The extra columns a class enables on top of General (restricted to what's
        available and not already in General)."""
        general = set(cls.general_columns(competition))
        available = cls.available_keys(competition)
        row = cls.objects.filter(
            competition=competition, competition_class=competition_class
        ).first()
        saved = set(row.columns) if row is not None else set()
        return [k for k in available if k in saved and k not in general]

    @classmethod
    def columns_for(cls, competition, competition_class):
        """The columns to show for one class: the General set plus the class's own
        additions, in canonical order."""
        general = set(cls.general_columns(competition))
        additions = set(cls.class_additions(competition, competition_class))
        available = cls.available_keys(competition)
        return [k for k in available if k in general or k in additions]

    @classmethod
    def overall_enabled(cls, competition):
        """Whether the cross-class Overall pages are shown (default on)."""
        row = cls.objects.filter(
            competition=competition, competition_class__isnull=True
        ).first()
        return row.show_overall if row is not None else True


class ManualTieResolution(models.Model):
    """A timekeeper's manual ordering of an otherwise-unbreakable tie group.

    ``scope`` names the results table the tie sits in — ``class:<pk>`` or
    ``overall:<method>:<runs>``. ``members`` is the resolved group, an ordered list
    of ``[entry_pk, occurrence, rank]``: the display order plus the rank each
    competitor was given (equal ranks express an intentional shared placing). A
    resolution applies only while the same set of competitors is still tied — the
    member set is matched on recompute, so a stale one is simply ignored."""

    competition = models.ForeignKey(
        "competitions.Competition", on_delete=models.CASCADE, related_name="tie_resolutions"
    )
    scope = models.CharField(max_length=50)
    members = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.competition} – tie ({self.scope})"

    def member_keys(self):
        return frozenset((m[0], m[1]) for m in self.members)
