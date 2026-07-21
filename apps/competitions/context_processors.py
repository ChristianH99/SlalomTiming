from .models import Competition


def active_competition(request):
    """The active competition plus its running classes in run order, so the sidebar
    can list a Results sub-page per class."""
    competition = Competition.get_current()
    running_classes = competition._running_classes_ordered() if competition else []
    return {
        "active_competition": competition,
        "results_classes": running_classes,
    }
