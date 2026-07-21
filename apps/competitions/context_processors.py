from .models import Competition


def active_competition(request):
    """The active competition plus its running classes in run order and the Overall
    result groups, so the sidebar can list a Results sub-page per class and per
    Overall (scoring method × counted-run count)."""
    competition = Competition.get_current()
    running_classes = competition._running_classes_ordered() if competition else []
    overall_groups = []
    if competition:
        # Imported here to avoid a results<->competitions import cycle at load time.
        from apps.results import resultscalc
        from apps.results.models import ResultColumnSettings
        if ResultColumnSettings.overall_enabled(competition):
            overall_groups = resultscalc.overall_groups(competition)
    return {
        "active_competition": competition,
        "results_classes": running_classes,
        "results_overall": overall_groups,
    }
