"""Helpers shared across apps."""

import json

from django import forms
from django.utils.http import url_has_allowed_host_and_scheme

# The one format a browser's native date picker will read or write, whatever
# language the page is in. It is not a display format and never reaches a reader:
# the browser renders the value in the *user's* locale and posts it back as this.
ISO_DATE = "%Y-%m-%d"


class DateInput(forms.DateInput):
    """A native date picker that shows the date it was given.

    ``<input type="date">`` accepts exactly one value format — ISO ``2010-06-15``
    — and **silently ignores anything else**, leaving the control empty. Django,
    meanwhile, renders a date through the active locale: with the shipped default
    language (German) a stored 15 June 2010 comes out as ``15.06.2010``, the
    browser discards it, and the operator opens an existing participant to find
    the birthday gone. Nothing errors; the value is still in the database.

    So the format is pinned here rather than at each use. The two dates this app
    edits (a participant's date of birth, a competition's date) each declared
    their own ``forms.DateInput(attrs={"type": "date"})`` and each had the bug;
    a third would have inherited it. ``config/tests.py`` fails on any date widget
    in the project that is not this one.

    Note this is a *rendering* fix only — the browser posts ISO back and Django's
    ``DateField`` parses that in any locale, so saving was never broken. The
    report was of a value that vanished from the screen, and that is what it was.
    """

    input_type = "date"

    def __init__(self, attrs=None, format=None):        # noqa: A002 - Django's own name
        super().__init__(attrs, format or ISO_DATE)


def json_body(request):
    """The request's JSON body as a dict — ``{}`` for anything else.

    Every endpoint the browser posts JSON to goes through here, because there
    are three ways a body that is not our JSON reaches a view, and each of them
    is a 500 rather than an answer:

    * ``json.loads`` on **bytes** sniffs the encoding from the leading octets, so
      a body starting with a null byte is read as UTF-16 and raises
      ``UnicodeDecodeError``. That is a ``ValueError`` but *not* a
      ``JSONDecodeError``, so an ``except json.JSONDecodeError`` — which is what
      three of these endpoints had — does not catch it.
    * Valid JSON is not necessarily an object. ``"a string"``, ``42`` and
      ``null`` all parse happily, and then every ``payload.get(...)`` in the view
      is an ``AttributeError``.
    * A body that is not valid UTF-8 at all.

    A client sending any of these is broken or unfriendly; either way the answer
    is "no", not a traceback. Found by ``config/hostility_tests.py``, which asks
    every JSON endpoint in the app the same questions — the two that fell over
    were the two that had written their own parsing.
    """
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        # Both JSONDecodeError and UnicodeDecodeError are ValueErrors — which is
        # exactly the trap: catching the narrower `json.JSONDecodeError` reads
        # like it covers a malformed body and does not.
        return {}
    return payload if isinstance(payload, dict) else {}


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
    was navigating to.

    ``require_https`` follows the request: on a TLS deployment an absolute
    ``http://`` URL — even to this very host — is a downgrade, and saving a form
    is not the moment to put a session cookie on the air.
    """
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return fallback
