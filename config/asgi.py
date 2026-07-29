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


def _require_allowed_hosts():
    """A deployment must say which hosts it answers on.

    With DEBUG off and ALLOWED_HOSTS empty, Django starts and then refuses every
    single request with DisallowedHost. On race morning that reads as "the server
    is broken" from every phone at the venue at once — the worst moment to go
    looking for a setting. So it is refused here, where the message can say what
    to do, and *here* rather than in settings.py because the setting only means
    anything to something that serves requests: `collectstatic` is a required
    release step (and the packaged build's own) and runs with DEBUG off and no
    hosts at all, quite legitimately.
    """
    from django.conf import settings

    if settings.DEBUG or settings.ALLOWED_HOSTS:
        return
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        'DJANGO_ALLOWED_HOSTS is empty and DEBUG is False, so every request '
        'would be refused with DisallowedHost. Set it to the names and '
        'addresses this server answers on, comma-separated:\n'
        '  DJANGO_ALLOWED_HOSTS=timing.club.example,192.168.1.10,localhost'
    )


_require_allowed_hosts()

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

# The event is the database, so it is copied somewhere else on a timer. Here for
# the same reason as the reader thread: this is the one entry point that is a
# running server (see apps/transfer/backup.py).
from apps.transfer import backup  # noqa: E402

backup.autostart()


def _harden_data_directory():
    """Restrict DATA_DIR to the account running the server (config/datasecurity.py).

    Here for the same reason as the single-instance lock and the CP540 autostart:
    this is the one entry point that is actually a server, so management commands
    and the test suite never touch the developer's own file modes.
    """
    import logging

    from django.conf import settings

    from config import datasecurity

    outcome = datasecurity.harden(settings.DATA_DIR, settings.HARDEN_DATA_DIR)
    logging.getLogger(__name__).info('Data directory permissions: %s', outcome)


_harden_data_directory()


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
