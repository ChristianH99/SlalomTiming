"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Must be created before importing anything that touches models/apps, so
# Django's app registry is populated first.
django_asgi_app = get_asgi_application()

from config import singleinstance  # noqa: E402

# One event, one server process (channel layer, CP540 reader and event loop are all
# per-process). This is the only entry point a server goes through, so the rule is
# enforced where it's real — management commands and tests never reach it.
singleinstance.acquire()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from apps.timing import cp540  # noqa: E402
from apps.timing.routing import websocket_urlpatterns  # noqa: E402

# A restart must not quietly leave the rig deaf. The reader thread dies with the
# process, so if the operator left the device connected it is started again here
# — the same place the single-process rule is enforced, and the only entry point
# that is actually a running server.
cp540.autostart()


def _sweep_expired_sessions():
    """Delete session rows that have already expired.

    Django never prunes them for the database backend, so the table grew for the
    life of the install — rows holding a user id long after the session died, and
    a scan every time a page asks "who else is signed in?" (apps/common.py). A
    server start is the natural moment: it is once per event day, and it cannot
    collide with anything, because nothing is serving yet.
    """
    import logging

    try:
        from django.contrib.sessions.models import Session
        from django.utils import timezone

        removed, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
        if removed:
            logging.getLogger(__name__).info('Removed %d expired sessions', removed)
    except Exception:  # noqa: BLE001 - an unmigrated database must not stop the server
        logging.getLogger(__name__).warning(
            'Could not sweep expired sessions', exc_info=True
        )


_sweep_expired_sessions()

application = ProtocolTypeRouter(
    {
        'http': django_asgi_app,
        # Browsers do not apply the same-origin policy to WebSockets: any page an
        # operator visits can open a socket to this server with their cookies
        # attached. AllowedHostsOriginValidator refuses a handshake whose Origin
        # isn't one of ours, which is the WebSocket half of what CSRF does for
        # forms. (With DEBUG on, ALLOWED_HOSTS is localhost, so the Simulator tab
        # and the dev server still work.)
        'websocket': AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        ),
    }
)
