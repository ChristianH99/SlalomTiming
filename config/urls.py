"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import re

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.i18n import JavaScriptCatalog

from config.health import health
from config.media import serve_media

urlpatterns = [
    # Something a check can be pointed at without a session; see config/health.py
    # for why it says so little. Ungated (apps/accounts/pages.py OPEN).
    path('healthz', health, name='health'),
    path('admin/', admin.site.urls),
    # django.views.i18n.set_language: the topbar language selector POSTs here to
    # store the chosen language (session + cookie) and redirect back.
    path('i18n/', include('django.conf.urls.i18n')),
    # Serves the compiled "djangojs" catalog as a script that defines gettext()
    # etc. in the browser, so static/js/*.js can be translated. Loaded from
    # base.html before the app scripts; reflects the request's active language.
    path('jsi18n/', JavaScriptCatalog.as_view(), name='javascript-catalog'),
    path('accounts/', include('apps.accounts.urls')),
    path('competitions/', include('apps.competitions.urls')),
    path('participants/', include('apps.participants.urls')),
    path('results/', include('apps.results.urls')),
    path('transfer/', include('apps.transfer.urls')),
    path('', include('apps.timing.urls')),
]

# Serve uploaded logos (results-PDF headers/footers). Unlike static files these are
# written at runtime, so WhiteNoise — which indexes STATIC_ROOT once at startup —
# can't serve them; with DEBUG off and this route absent the logo previews 404.
# django.views.static.serve is slow for large media, but this is a handful of small
# logos on a single-event LAN app. Point a reverse proxy at MEDIA_ROOT and set
# DJANGO_SERVE_MEDIA=False to take it out of the Python process.
# (django.conf.urls.static.static() can't be used: it returns nothing unless DEBUG.)
# Served through config/media.py, not django.views.static.serve directly: every
# other page in this app needs a login and this one is no exception — see there.
if settings.SERVE_MEDIA:
    urlpatterns += [
        re_path(
            r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')),
            serve_media,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
