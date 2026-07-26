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


class TestTemplateComments:
    """Django's ``{# #}`` is single-line only: its lexer matches ``{#.*?#}``
    without DOTALL, so a comment that wraps is never recognised as one and its
    text is rendered onto the page for the operator to read.

    This has now escaped review twice — once before commit 5eef180, and again
    while doing the design-system work, where a note above a ``<th>`` printed
    itself above the timing table. Nothing was checking, so here is the check.
    Multi-line commentary belongs in ``{% comment %}…{% endcomment %}``.
    """

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_no_multiline_hash_comments(self, template):
        source = template.read_text(encoding='utf-8')
        for index, opener in enumerate(source.split('{#')):
            if index == 0:
                continue
            head = opener.split('#}')[0]
            assert '\n' not in head, (
                f'{template.name}: a {{# #}} comment spans lines, so Django will '
                f'render it as text. Use {{% comment %}}: {head.strip()[:60]}…'
            )


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


class TestTls:
    """REL-7: HTTPS is the default and turning it off is one explicit decision.

    These run `manage.py diffsettings` in a subprocess because the hardening block
    only exists with DEBUG off — it cannot be reached with override_settings.
    """

    KEY = 'a-real-key-for-this-test-0123456789abcdefghijklmnop'

    def _settings(self, **env):
        # --all, because a setting left at Django's own default (SECURE_SSL_REDIRECT
        # is False out of the box) is otherwise not listed at all — and "absent" is
        # exactly what these tests must not confuse with "off".
        result = _run_manage(
            'diffsettings', '--output', 'hash', '--all',
            DJANGO_DEBUG='False', DJANGO_ALLOWED_HOSTS='localhost',
            DJANGO_SECRET_KEY=self.KEY, **env,
        )
        assert result.returncode == 0, result.stderr
        values = {}
        for line in result.stdout.splitlines():
            line = line.removeprefix('###').strip()
            if ' = ' in line:
                name, _, value = line.partition(' = ')
                values[name.strip()] = value.strip()
        return values, result.stderr

    def test_a_deployment_is_https_by_default(self):
        values, _stderr = self._settings()
        assert values['SECURE_SSL_REDIRECT'] == 'True'
        assert values['SESSION_COOKIE_SECURE'] == 'True'
        assert values['CSRF_COOKIE_SECURE'] == 'True'

    def test_plain_http_needs_the_one_explicit_flag_and_says_so(self):
        values, stderr = self._settings(DJANGO_ALLOW_PLAIN_HTTP='True')
        assert values['SECURE_SSL_REDIRECT'] == 'False'
        assert values['SESSION_COOKIE_SECURE'] == 'False'
        assert values['SECURE_HSTS_SECONDS'] == '0'  # HSTS is meaningless over HTTP
        # Loud, because this is the setting that undoes the rest of the block.
        assert 'without TLS' in stderr

    def test_the_retired_escape_hatches_refuse_to_start(self):
        """An old .env must not quietly keep serving plain HTTP under a name that no
        longer describes what it does."""
        for retired in ('DJANGO_SECURE_COOKIES', 'DJANGO_SECURE_SSL_REDIRECT'):
            result = _run_manage(
                'check', DJANGO_DEBUG='False', DJANGO_ALLOWED_HOSTS='localhost',
                DJANGO_SECRET_KEY=self.KEY, **{retired: 'False'},
            )
            assert result.returncode != 0, retired
            assert 'DJANGO_ALLOW_PLAIN_HTTP' in result.stderr

    def test_hsts_is_short_by_default_and_raisable(self):
        """An HSTS pin cannot be revoked before it expires, and a venue hostname
        served from a local CA gets reused — so a year is a trap, not a default."""
        values, _stderr = self._settings()
        assert 0 < int(values['SECURE_HSTS_SECONDS']) <= 3600
        assert values['SECURE_HSTS_PRELOAD'] == 'False'
        raised, _stderr = self._settings(
            DJANGO_HSTS_SECONDS='31536000', DJANGO_HSTS_PRELOAD='True')
        assert raised['SECURE_HSTS_SECONDS'] == '31536000'
        assert raised['SECURE_HSTS_PRELOAD'] == 'True'

    def test_check_deploy_is_clean(self):
        """`start-server.ps1` runs this on every start, so a real warning has to
        stand out — which it can't if there are warnings we have decided to accept."""
        result = _run_manage(
            'check', '--deploy',
            DJANGO_DEBUG='False', DJANGO_ALLOWED_HOSTS='localhost',
            DJANGO_CSRF_TRUSTED_ORIGINS='https://localhost',
            DJANGO_SECRET_KEY=self.KEY,
        )
        assert result.returncode == 0, result.stderr
        assert 'System check identified no issues' in result.stdout + result.stderr


class TestSessions:
    def test_a_session_does_not_last_a_fortnight(self):
        """Django's two-week default, on a shared timekeeping laptop and on
        marshals' personal phones."""
        assert settings.SESSION_COOKIE_AGE <= 24 * 3600


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
