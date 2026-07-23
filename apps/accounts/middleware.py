"""Access-control middleware: login required everywhere, plus page-level gating.

Runs as `process_view` so `request.resolver_match` is populated (the app/url name
we gate on). The timing device endpoint stays open (a device can't log in); the
admin gates itself on staff; everything else needs a login and, for non-superusers,
a role that grants the page the URL belongs to.
"""

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render

from . import pages


class AccessControlMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Static/media are served outside the URL resolver (or as unnamed
        # patterns); never gate them.
        path = request.path
        if path.startswith(settings.STATIC_URL) or path.startswith(settings.MEDIA_URL):
            return None

        match = request.resolver_match
        if match is None:
            return None

        app_name = match.app_name
        url_name = match.url_name

        # The Django admin authenticates itself (staff-only login).
        if match.namespace == "admin":
            return None

        # The login / logout pages must be reachable while anonymous.
        if app_name == "accounts" and url_name in ("login", "logout"):
            return None

        # The timing device posts here and can't authenticate.
        if (app_name, url_name) in pages.OPEN:
            return None

        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        # Superusers bypass all page gating.
        if request.user.is_superuser:
            return None

        # The User Access management pages are superuser-only.
        if app_name == "accounts":
            return self._forbidden(request)

        required = pages.pages_for_url(app_name, url_name)
        if not required:
            # Unmapped utility endpoint — a login is enough.
            return None

        if required & pages.user_pages(request.user):
            return None

        return self._forbidden(request)

    def _forbidden(self, request):
        return render(request, "accounts/forbidden.html", status=403)
