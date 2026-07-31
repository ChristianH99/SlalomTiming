"""Who changed what.

A timekeeper, a marshal and an organiser all write to the same rows, and until
now nothing recorded which of them did. That is fine on one laptop and useless
the moment a competitor protests a result: "the penalty on run 412 says 2 pylons"
is a fact nobody can attribute, and there is no way to tell an honest correction
from a mistake from a stranger on the venue Wi-Fi.

So every request that *changes* something writes a line to ``audit.log``.

Deliberately a log file rather than a table:

* a table means a database write on every edit, and this app's scarce resource is
  the one SQLite write lock the CP540 reader thread needs to record a time. An
  append to a file takes nothing the timing rig wants;
* it needs no model, no migration and no screen of its own — a superuser
  downloads it from the User Access page (apps/accounts/views.py);
* it survives whatever the database does, which is the case it exists for.

Deliberately a *middleware* rather than a call in each view. There are around
forty endpoints that change data; a hand-written line in each is forty chances to
forget one, and the one that gets forgotten is the one somebody asks about. This
sees every request instead, and cannot be bypassed by adding a view.

**Only mutations.** GET is the live views re-fetching, several times a second per
open browser; logging that would bury the event in noise and record nothing.

What a line holds: when, who, from where, what they called, what they sent, and
what came back. The payload is what makes it useful — "user=timekeeper
timing:run-status run_id=412 status=dsq" answers the protest — so it is recorded,
with the obvious exceptions redacted.
"""

import json
import logging

logger = logging.getLogger("apps.audit")

# Never write these to disk, whatever they are called on the way in. Matched as
# substrings and case-insensitively, so `password1`, `new_password` and
# `X-Device-Token` are all covered without listing them.
REDACTED_KEYS = ("password", "token", "csrf", "secret", "key")
REDACTED = "***"

# A payload is context, not a copy of the request. Long values are the free-text
# ones (a PDF header, a task spec), and the interesting part is at the front.
MAX_VALUE = 200
MAX_PAYLOAD = 2000


def _redact(key, value):
    lowered = str(key).lower()
    if any(marker in lowered for marker in REDACTED_KEYS):
        return REDACTED
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:MAX_VALUE] + "…" if len(text) > MAX_VALUE else text


def payload(request):
    """The request's parameters, redacted and flattened to ``k=v`` pairs.

    Read here rather than after the view: a JSON body can only be consumed once,
    and by then the view may have taken it.
    """
    items = {}
    try:
        if request.content_type == "application/json":
            try:
                body = json.loads(request.body or b"{}")
            except (ValueError, UnicodeDecodeError):
                body = {}
            if isinstance(body, dict):
                items = {k: _redact(k, v) for k, v in body.items()}
            else:
                items = {"body": _redact("body", body)}
        else:
            # Files are named but never read: an import archive is 60 MB of other
            # people's personal data and has no business in a log.
            items = {k: _redact(k, v) for k, v in request.POST.items()}
            items.update({k: f"<file {f.name}>" for k, f in request.FILES.items()})
    except Exception:  # noqa: BLE001
        # Reading the request is itself fallible — an over-large body raises
        # RequestDataTooBig, a truncated multipart raises, a consumed stream
        # raises RawPostDataException. None of that may take down the request
        # this is only *observing*: log that something arrived and could not be
        # read, which is itself worth knowing, and let the view answer as it
        # would have.
        return "<unreadable body>"
    text = " ".join(f"{k}={v}" for k, v in items.items())
    return text[:MAX_PAYLOAD] + "…" if len(text) > MAX_PAYLOAD else text


def _actor(request):
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user.get_username()
    # An unauthenticated mutation is the timing-device door (pages.OPEN) — worth
    # saying so rather than leaving the line looking anonymous by accident.
    return "-"


def _client_ip(request):
    from apps.accounts.throttle import client_ip

    return client_ip(request)


class AuditMiddleware:
    """Write one line per data-changing request. See the module docstring."""

    METHODS = ("POST", "PUT", "PATCH", "DELETE")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Captured before the view runs, because a JSON body is consumable and a
        # form's fields can be mutated by the form machinery.
        snapshot = payload(request) if request.method in self.METHODS else None
        response = self.get_response(request)
        if snapshot is None:
            return response
        match = getattr(request, "resolver_match", None)
        view = "?"
        if match is not None:
            view = f"{match.app_name}:{match.url_name}" if match.app_name else (match.url_name or "?")
        # The response code goes in brackets rather than as `status=`: several
        # payloads carry a `status` of their own (a run's DNF/DSQ above all), and
        # two different `status=` on one line is a line nobody can read.
        logger.info(
            "user=%s ip=%s %s %s [%s] %s",
            _actor(request), _client_ip(request), request.method, view,
            response.status_code, snapshot,
        )
        return response
