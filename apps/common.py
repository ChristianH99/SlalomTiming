"""Helpers shared across apps."""

from django.utils.http import url_has_allowed_host_and_scheme

# How many session rows to look at when answering "is anyone else using this?".
# A venue has a handful; the cap is only there so a stale session table can never
# turn a page load into a scan.
_SESSION_SCAN_LIMIT = 200


def other_signed_in_users(request):
    """Who else is signed in right now, newest session first.

    Some settings are global to the whole installation — the active competition
    above all — so changing one changes what everybody else's screen is showing.
    That is fine on a single laptop and a trap as a network app, so the pages
    that own such a setting ask first, and this is how they know whether there
    is anybody to ask about.

    Sessions that have expired don't count, nor does the caller's own — but a
    second session of the caller's own account does: it is another screen, quite
    possibly in somebody else's hands.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session
    from django.utils import timezone

    own_key = request.session.session_key
    rows = (
        Session.objects.filter(expire_date__gt=timezone.now())
        .exclude(session_key=own_key)
        .order_by("-expire_date")[:_SESSION_SCAN_LIMIT]
    )
    user_ids = []
    for row in rows:
        try:
            uid = row.get_decoded().get("_auth_user_id")
        except Exception:       # a corrupted or unreadable session tells us nothing
            continue
        if uid and uid not in user_ids:
            user_ids.append(uid)
    if not user_ids:
        return []
    users = get_user_model().objects.filter(pk__in=user_ids)
    by_id = {str(user.pk): user for user in users}
    return [by_id[uid] for uid in user_ids if uid in by_id]


def safe_next(request, fallback):
    """Return the POSTed ?next URL if it's a safe in-app path, else fallback.
    Lets the unsaved-changes modal's "Save changes" land on the page the user
    was navigating to."""
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return fallback
