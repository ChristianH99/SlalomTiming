"""Content-Security-Policy (SEC-11).

A CSP is the difference between "an injected string reached the page" and "an
injected string ran". This app has real injection surface — a results-PDF
header an operator types and an *import* can also write, participant names that
reach every table, a marshal's free-text task spec — and each of those is guarded
at its own door. The CSP is what is left when one of those doors is wrong.

The policy is strict, and getting there is why every script in this app now lives
in ``static/js/``: ``script-src 'self'`` cannot tell our inline ``<script>`` from
an injected one, so allowing the first means allowing the second, and a policy
with ``'unsafe-inline'`` in it is a policy that does not stop the attack it is for.
The same goes for ``style-src`` and the one ``style="…"`` attribute that used to
be in the templates.

Deliberately no nonce. A nonce would let the inline blocks stay, at the cost of a
per-request random value threaded through every template — and it fails open the
moment somebody forgets one, which is exactly the kind of quiet regression this
audit found elsewhere. Files cannot be forgotten.

Notes on individual directives:

* ``connect-src 'self'`` covers the live WebSocket: CSP treats ``ws://`` and
  ``wss://`` to the page's own host as ``'self'``.
* ``img-src`` allows ``data:`` because that is how a canvas-drawn image would
  arrive; the uploaded logos are same-origin.
* ``frame-ancestors 'none'`` is the modern X-Frame-Options, which settings.py
  also sets for older browsers.
* ``form-action 'self'`` means a form on our page cannot be made to post
  somewhere else — the thing an injected ``<form>`` would be for.
"""

POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])

HEADER = "Content-Security-Policy"
REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"


class ContentSecurityPolicyMiddleware:
    """Attach the policy to every response.

    ``CSP_REPORT_ONLY`` (env ``DJANGO_CSP_REPORT_ONLY``) sends it as
    *Report-Only* instead — nothing is blocked, violations are reported to the
    browser console. That is the setting for finding out what a new page broke
    without breaking it in front of an operator mid-event; it is not a setting to
    deploy with.

    The Django admin is exempt: it ships its own inline scripts and styles, we do
    not control them, and it is a staff-only fallback surface rather than part of
    the app's own attack surface.
    """

    def __init__(self, get_response):
        from django.conf import settings

        self.get_response = get_response
        self.header = (
            REPORT_ONLY_HEADER if getattr(settings, "CSP_REPORT_ONLY", False) else HEADER
        )

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/admin/"):
            return response
        response.headers.setdefault(self.header, POLICY)
        return response
