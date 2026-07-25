"""Deployment tests: the things that only break once DEBUG is off.

Every check here failed silently before — an unstyled production site, logos that
404, fonts fetched from a CDN at a venue with no uplink, the public development
secret key in a deployment, two server processes splitting the live updates in half.
None of it shows up in a normal test run, because a normal test run has DEBUG on.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import resolve, reverse

from config import singleinstance

TEMPLATE_DIR = Path(settings.BASE_DIR) / 'templates'


def _run_manage(*args, **env):
    """Run a manage.py command in a subprocess with a patched environment.

    A value of None removes the variable — settings.py distinguishes "unset" (use
    the development fallback) from "set to something".
    """
    environ = {**os.environ, **{k: v for k, v in env.items() if v is not None}}
    for name, value in env.items():
        if value is None:
            environ.pop(name, None)
    return subprocess.run(
        [sys.executable, 'manage.py', *args],
        cwd=settings.BASE_DIR,
        env=environ,
        capture_output=True,
        text=True,
    )


class TestStaticFiles:
    def test_static_root_is_set(self):
        """collectstatic refuses to run without it, so nothing serves /static/."""
        assert settings.STATIC_ROOT
        assert Path(settings.STATIC_ROOT).is_absolute()

    def test_whitenoise_serves_static(self):
        """`runserver` stops serving /static/ once DEBUG is off; WhiteNoise doesn't."""
        assert settings.STORAGES['staticfiles']['BACKEND'].startswith('whitenoise.')
        middleware = settings.MIDDLEWARE
        assert 'whitenoise.middleware.WhiteNoiseMiddleware' in middleware
        # It has to sit directly below SecurityMiddleware to see every request.
        assert middleware.index('whitenoise.middleware.WhiteNoiseMiddleware') == \
            middleware.index('django.middleware.security.SecurityMiddleware') + 1


class TestMedia:
    def test_media_is_served_with_debug_off(self):
        """The results-PDF logo previews are uploaded at runtime, so WhiteNoise
        (which indexes STATIC_ROOT at startup) can't serve them — Django must."""
        with override_settings(DEBUG=False):
            match = resolve(f'{settings.MEDIA_URL}results_logos/1/logo.png')
        assert match.func.__module__ == 'django.views.static'
        assert match.kwargs['document_root'] == settings.MEDIA_ROOT

    def test_media_is_not_gated_by_access_control(self, rf):
        """The access-control middleware skips /media/ by prefix, which only works
        because Django normalises MEDIA_URL to a leading slash."""
        from apps.accounts.middleware import AccessControlMiddleware

        assert str(settings.MEDIA_URL).startswith('/')
        request = rf.get(f'{settings.MEDIA_URL}results_logos/1/logo.png')
        middleware = AccessControlMiddleware(lambda req: None)
        assert middleware.process_view(request, lambda req: None, (), {}) is None


class TestFonts:
    """REL-4: no third-party font requests. A venue has no uplink (every page load
    would block on a timing-out request), and sending visitors' IPs to Google is the
    pattern German courts have ruled against."""

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_no_external_font_requests(self, template):
        source = template.read_text(encoding='utf-8')
        assert 'fonts.googleapis.com' not in source
        assert 'fonts.gstatic.com' not in source

    def test_font_files_are_bundled(self):
        fonts = Path(settings.BASE_DIR) / 'static' / 'fonts'
        names = {path.name for path in fonts.glob('*.woff2')}
        assert {'inter-latin.woff2', 'manrope-latin.woff2'} <= names
        assert (fonts / 'OFL.txt').exists(), 'the bundled fonts need their licence'

    def test_font_face_sheet_points_at_the_bundled_files(self):
        sheet = (Path(settings.BASE_DIR) / 'static' / 'css' / 'fonts.css').read_text(encoding='utf-8')
        assert '@font-face' in sheet
        assert 'https://' not in sheet


class TestSecretKey:
    """SEC-15: the development key is public — every checkout has it."""

    def test_deployment_with_the_dev_key_refuses_to_start(self):
        result = _run_manage(
            'check',
            DJANGO_DEBUG='False',
            DJANGO_ALLOWED_HOSTS='localhost',
            DJANGO_SECRET_KEY=None,
        )
        assert result.returncode != 0
        assert 'DJANGO_SECRET_KEY is not set' in result.stderr

    def test_deployment_with_a_real_key_starts(self):
        result = _run_manage(
            'check',
            DJANGO_DEBUG='False',
            DJANGO_ALLOWED_HOSTS='localhost',
            DJANGO_SECRET_KEY='a-real-key-for-this-test-0123456789abcdefghijklmnop',
        )
        assert result.returncode == 0, result.stderr


class TestSingleInstance:
    """REL-5: one event, one process — enforced, not assumed."""

    def _release(self):
        if singleinstance._lock_file is not None:
            singleinstance._lock_file.close()
            singleinstance._lock_file = None

    def test_second_server_is_refused(self, tmp_path, capsys):
        try:
            with override_settings(BASE_DIR=tmp_path, DEBUG=False):
                singleinstance.acquire()
                assert singleinstance._lock_file is not None
                held = singleinstance._lock_file
                with pytest.raises(SystemExit):
                    singleinstance.acquire()
                # The first process keeps the lock; only the newcomer is turned away.
                assert singleinstance._lock_file is held
            assert 'already running' in capsys.readouterr().err
        finally:
            self._release()

    def test_development_only_warns(self, tmp_path, capsys):
        try:
            with override_settings(BASE_DIR=tmp_path, DEBUG=True):
                singleinstance.acquire()
                singleinstance.acquire()  # must not raise while developing
            assert 'starting anyway' in capsys.readouterr().err
        finally:
            self._release()

    def test_steps_aside_for_a_cross_process_channel_layer(self, tmp_path):
        layers = {'default': {'BACKEND': 'channels_redis.core.RedisChannelLayer'}}
        with override_settings(BASE_DIR=tmp_path, DEBUG=False, CHANNEL_LAYERS=layers):
            singleinstance.acquire()
            singleinstance.acquire()
        assert singleinstance._lock_file is None
        assert not (tmp_path / 'run').exists()

    def test_escape_hatch(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DJANGO_ALLOW_MULTIPLE_SERVERS', '1')
        with override_settings(BASE_DIR=tmp_path, DEBUG=False):
            singleinstance.acquire()
            singleinstance.acquire()
        assert singleinstance._lock_file is None


class TestRunBook:
    def test_deployment_documentation_exists(self):
        """REL-3: a deployment needs a written procedure, not a README bullet."""
        root = Path(settings.BASE_DIR)
        assert (root / 'DEPLOYMENT.md').exists()
        assert (root / 'deploy' / 'slalomtiming.service').exists()
        assert (root / 'deploy' / 'start-server.ps1').exists()
        assert (root / 'deploy' / 'Caddyfile').exists()

    def test_login_page_is_reachable(self, client):
        """Smoke test that the URL conf still resolves after the media route."""
        assert client.get(reverse('accounts:login')).status_code == 200
