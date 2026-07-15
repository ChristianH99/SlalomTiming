"""Helpers shared across apps."""

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, fallback):
    """Return the POSTed ?next URL if it's a safe in-app path, else fallback.
    Lets the unsaved-changes modal's "Save changes" land on the page the user
    was navigating to."""
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return fallback
