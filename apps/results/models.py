from django.db import models

from apps.competitions.models import CompetitionType


class ResultColumnSettings(models.Model):
    """Which optional participant-info columns a competition's results tables show.

    One *General* row per competition (``competition_class`` null) holds the default
    column set; an optional row per class overrides it (unless ``inherit_general``).
    The configurable columns are exactly the details a discipline collects
    (``CompetitionType.PARTICIPANT_INFO``) — Bib and Name are always shown and never
    stored here. ``columns`` is the list of enabled PARTICIPANT_INFO setting keys."""

    competition = models.ForeignKey(
        "competitions.Competition", on_delete=models.CASCADE, related_name="result_columns"
    )
    # Null means the competition-wide *General* defaults; a class makes it an override.
    competition_class = models.ForeignKey(
        "competitions.CompetitionClass", null=True, blank=True,
        on_delete=models.CASCADE, related_name="result_columns",
    )
    columns = models.JSONField(default=list, blank=True)
    # Per-class only: when on, the class falls back to the General column set and its
    # own `columns` is ignored. Meaningless on the General row.
    inherit_general = models.BooleanField(default=True)

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

    # ----- column vocabulary (reuses the participant-info source of truth) -----

    @staticmethod
    def available_keys(competition):
        """The PARTICIPANT_INFO setting keys this competition's type collects, in
        their canonical order — the columns that may be shown."""
        ctype = competition.competition_type
        return [
            setting
            for setting in CompetitionType.PARTICIPANT_INFO
            if getattr(ctype, setting)
        ]

    @staticmethod
    def label_for(key):
        return CompetitionType.PARTICIPANT_INFO[key][0]

    @staticmethod
    def fields_for(key):
        return CompetitionType.PARTICIPANT_INFO[key][2]

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
    def columns_for(cls, competition, competition_class):
        """The columns to show for one class: its own override when saved and not
        inheriting, otherwise the General set."""
        row = cls.objects.filter(
            competition=competition, competition_class=competition_class
        ).first()
        if row is None or row.inherit_general:
            return cls.general_columns(competition)
        available = cls.available_keys(competition)
        return [key for key in available if key in (row.columns or [])]
