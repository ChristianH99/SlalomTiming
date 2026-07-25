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

from apps.timing.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        'http': django_asgi_app,
        'websocket': AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
