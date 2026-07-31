"""Serving uploaded media (the results-PDF logos) — behind the login.

Media is the one thing this app writes at runtime and hands back over HTTP, and it
used to be the one route out of the login gate: ``AccessControlMiddleware`` skipped
``/media/`` by prefix, before it ever looked at ``request.user``. That mattered more
than "a club emblem is not a secret", because the *content* of MEDIA_ROOT is decided
by whatever an operator uploads or imports — and anything served from this origin
that a browser is willing to execute runs with the app's cookies.

So the exemption is gone (see apps/accounts/middleware.py) and this view stands in
front of ``django.views.static.serve``. Two guards, deliberately both:

* a login, checked here rather than left to the middleware, so it survives anyone
  reworking the middleware's prefix rules;
* ``nosniff`` plus a filename-derived type, so a file whose bytes disagree with its
  extension is not sniffed into something executable.

Note the deployment shape this cannot reach: ``DJANGO_SERVE_MEDIA=False`` hands
``/media/`` to a reverse proxy, which serves it with no login at all. That is a
choice to make deliberately — DEPLOYMENT.md says so.
"""

from django.contrib.auth.decorators import login_required
from django.views.static import serve as static_serve


@login_required
def serve_media(request, path, document_root=None):
    response = static_serve(request, path, document_root=document_root)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
