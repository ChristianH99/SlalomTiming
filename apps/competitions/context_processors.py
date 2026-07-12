from .models import Competition


def active_competition(request):
    return {"active_competition": Competition.get_current()}
