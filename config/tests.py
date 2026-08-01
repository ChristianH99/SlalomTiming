"""Deployment tests: the things that only break once DEBUG is off.

Every check here failed silently before — an unstyled production site, logos that
404, fonts fetched from a CDN at a venue with no uplink, the public development
secret key in a deployment, two server processes splitting the live updates in half.
None of it shows up in a normal test run, because a normal test run has DEBUG on.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import resolve, reverse

from config import singleinstance

TEMPLATE_DIR = Path(settings.BASE_DIR) / 'templates'
CSS = Path(settings.BASE_DIR) / 'static' / 'css' / 'main.css'
JS_DIR = Path(settings.BASE_DIR) / 'static' / 'js'


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
        # config.media wraps django.views.static.serve in a login check — media is
        # written from what operators upload and import, so it is not public.
        assert match.func.__module__ == 'config.media'
        assert match.kwargs['document_root'] == settings.MEDIA_ROOT

    def test_media_needs_a_login(self):
        """Media is written at runtime from what operators upload and *import*, and
        it is served from this app's own origin. It used to be the one route out of
        the login gate (the middleware skipped /media/ by prefix), which is what let
        a crafted import archive plant a file anybody could then fetch."""
        # A fresh client, not the shared fixture: that one is signed in as a
        # superuser, which is exactly the thing this test must not assume.
        from django.test import Client

        response = Client().get(f'{settings.MEDIA_URL}results_logos/1/logo.png')
        assert response.status_code == 302, 'uploaded media is reachable without a login'
        assert '/login/' in response.url


class TestFonts:
    """No third-party font requests. A venue has no uplink (every page load
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
    """No signing key may be committed, and a deployment must bring its own.

    There used to be a literal key in settings.py. This repository is public, so
    that key was public: anyone who had read it could forge a session cookie for
    any account. It is gone, each checkout mints its own into DATA_DIR, and these
    tests are what keep it that way."""

    def test_no_signing_key_is_committed(self):
        """A key in a public repository is a key everybody has, for ever — no
        history rewrite takes it back. So there must not be one to begin with."""
        source = (settings.BASE_DIR / 'config' / 'settings.py').read_text(encoding='utf-8')
        assert 'django-insecure-' not in source, (
            'a Django secret key literal is back in settings.py'
        )

    def test_a_development_checkout_generates_its_own_key(self):
        """A fresh checkout still runs with no setup — but not with everyone
        else's key."""
        from config.settings import _development_secret_key

        assert settings.SECRET_KEY
        assert len(settings.SECRET_KEY) >= 32
        # Stable across calls: sessions have to survive a restart.
        assert _development_secret_key() == _development_secret_key()

    def test_the_generated_key_is_not_committed(self):
        assert '.secret_key' in (settings.BASE_DIR / '.gitignore').read_text(encoding='utf-8')

    def test_deployment_without_a_key_refuses_to_start(self):
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
    """HTTPS is the default and turning it off is one explicit decision.

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
    """One event, one process — enforced, not assumed."""

    def _release(self):
        if singleinstance._lock_file is not None:
            singleinstance._lock_file.close()
            singleinstance._lock_file = None

    def test_second_server_is_refused(self, tmp_path, capsys):
        try:
            with override_settings(DATA_DIR=tmp_path, DEBUG=False):
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
            with override_settings(DATA_DIR=tmp_path, DEBUG=True):
                singleinstance.acquire()
                singleinstance.acquire()  # must not raise while developing
            assert 'starting anyway' in capsys.readouterr().err
        finally:
            self._release()

    def test_steps_aside_for_a_cross_process_channel_layer(self, tmp_path):
        layers = {'default': {'BACKEND': 'channels_redis.core.RedisChannelLayer'}}
        with override_settings(DATA_DIR=tmp_path, DEBUG=False, CHANNEL_LAYERS=layers):
            singleinstance.acquire()
            singleinstance.acquire()
        assert singleinstance._lock_file is None
        assert not (tmp_path / 'run').exists()

    def test_escape_hatch(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DJANGO_ALLOW_MULTIPLE_SERVERS', '1')
        with override_settings(DATA_DIR=tmp_path, DEBUG=False):
            singleinstance.acquire()
            singleinstance.acquire()
        assert singleinstance._lock_file is None


class TestWritablePaths:
    """Everything written at runtime lives under DATA_DIR, never under BASE_DIR.

    In a checkout the two are the same directory, so nothing here is visible while
    developing. In the packaged Windows build (build/) they are not: BASE_DIR is
    program files that the next installer replaces and an uninstall deletes, and
    DATA_DIR is the operator's event. A path that goes back to BASE_DIR would put
    the database in the first one, and nobody would find out until an upgrade
    silently took an event's times with it.
    """

    def _under(self, path, parent):
        return Path(parent) in Path(path).parents or Path(path) == Path(parent)

    def test_the_database_and_media_follow_the_data_directory(self, tmp_path):
        """In a subprocess, because the test runner swaps DATABASES['NAME'] for a
        test database — the configured value can only be read from outside."""
        result = _run_manage(
            'diffsettings', '--output', 'hash', '--all',
            SLALOM_DATA_DIR=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        printed = {}
        for line in result.stdout.splitlines():
            line = line.removeprefix('###').strip()
            if ' = ' in line:
                name, _, value = line.partition(' = ')
                printed[name.strip()] = value.strip()
        # as_posix(): a WindowsPath prints its repr with forward slashes.
        assert tmp_path.as_posix() in printed['DATABASES']
        assert tmp_path.as_posix() in printed['MEDIA_ROOT']

    def test_uploaded_media_is_in_the_data_directory(self):
        assert self._under(settings.MEDIA_ROOT, settings.DATA_DIR)

    def test_the_unrecorded_times_log_is_in_the_data_directory(self):
        """A time the database refused is the one record that it happened."""
        from apps.timing import ingest

        assert self._under(ingest.UNRECORDED_LOG, settings.DATA_DIR)

    def test_the_server_lock_is_in_the_data_directory(self, tmp_path):
        try:
            with override_settings(DATA_DIR=tmp_path, DEBUG=False):
                singleinstance.acquire()
            assert (tmp_path / 'run' / 'server.lock').exists()
        finally:
            if singleinstance._lock_file is not None:
                singleinstance._lock_file.close()
                singleinstance._lock_file = None

    def test_a_checkout_still_writes_beside_the_code(self):
        """The default has to stay BASE_DIR: dev, the tests and DEPLOYMENT.md all
        assume db.sqlite3 is in the project directory."""
        if not os.environ.get('SLALOM_DATA_DIR'):
            assert Path(settings.DATA_DIR) == Path(settings.BASE_DIR)


class TestRunBook:
    def test_deployment_documentation_exists(self):
        """A deployment needs a written procedure, not a README bullet."""
        root = Path(settings.BASE_DIR)
        assert (root / 'DEPLOYMENT.md').exists()
        assert (root / 'deploy' / 'slalomtiming.service').exists()
        assert (root / 'deploy' / 'start-server.ps1').exists()
        assert (root / 'deploy' / 'Caddyfile').exists()

    def test_the_windows_build_is_documented(self):
        """The one-click installer is a supported way to ship this app."""
        root = Path(settings.BASE_DIR)
        assert (root / 'build' / 'build.ps1').exists()
        assert (root / 'build' / 'installer.iss').exists()
        assert (root / 'build' / 'launcher.py').exists()
        assert (root / 'build' / 'README.md').exists()

    def test_login_page_is_reachable(self, client):
        """Smoke test that the URL conf still resolves after the media route."""
        assert client.get(reverse('accounts:login')).status_code == 200


class TestContentSecurityPolicy:
    """The policy is only worth having if it stays strict, and it only
    *can* be strict while no page carries inline script or style — so both halves
    are pinned here."""

    def test_every_page_carries_the_policy(self, client):
        response = client.get('/')
        assert 'Content-Security-Policy' in response.headers

    def test_the_policy_does_not_allow_inline_or_eval(self, client):
        policy = client.get('/').headers['Content-Security-Policy']
        assert "'unsafe-inline'" not in policy, (
            "a CSP with 'unsafe-inline' cannot tell our inline script from an "
            "injected one, which is the whole attack it exists to stop"
        )
        assert "'unsafe-eval'" not in policy
        for directive in ("default-src 'self'", "script-src 'self'",
                          "style-src 'self'", "frame-ancestors 'none'",
                          "base-uri 'none'", "form-action 'self'"):
            assert directive in policy, directive

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_no_template_carries_an_inline_script_or_style(self, template):
        """What makes the strict policy possible. A new inline block would not
        fail loudly — the page would simply stop working in a browser, which is
        the kind of thing that is discovered at an event."""
        source = template.read_text(encoding='utf-8')
        # Strip comments first: several of them talk *about* inline scripts.
        source = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '',
                        source, flags=re.S)
        source = re.sub(r'\{#.*?#\}', '', source)
        assert not re.search(r'<script(?![^>]*\ssrc=)[^>]*>', source), (
            f'{template.name}: inline <script>. Move it to static/js/ — see '
            f'config/csp.py.'
        )
        assert 'style="' not in source, (
            f'{template.name}: inline style attribute, which style-src blocks.'
        )

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_no_template_carries_an_inline_event_handler(self, template):
        source = template.read_text(encoding='utf-8')
        found = re.findall(r'\son(?:click|change|input|submit|load|error|'
                           r'keydown|keyup|focus|blur|mouseover)=', source)
        assert not found, f'{template.name}: inline event handler {found[:2]}'

    def test_no_script_file_contains_template_syntax(self):
        """A script that moved out of a template but kept a {% trans %} or a
        {{ var }} renders it verbatim into the browser."""
        for script in (settings.BASE_DIR / 'static' / 'js').glob('*.js'):
            source = script.read_text(encoding='utf-8')
            # Comments explain the move and legitimately name the tags they
            # replaced ("this used to be a {% trans %}"), so they are stripped
            # before the check — both forms.
            source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
            source = re.sub(r'^\s*//.*$', '', source, flags=re.M)
            assert '{%' not in source and '{{' not in source, (
                f'{script.name} still contains Django template syntax'
            )

    def test_no_script_file_starts_a_token_with_an_escaped_quote(self):
        r"""The signature of the edit that broke two files at once.

        Moving 1,778 lines out of templates was a mechanical edit, and the way it
        went wrong was ``querySelector(\"[data-run]\")`` — an escaped quote where
        a string should open, which is a SyntaxError that takes the whole file
        with it. Nothing on the server notices: the page renders, the script is
        dead, and you find out at an event.
        """
        for script in JS_DIR.glob('*.js'):
            source = script.read_text(encoding='utf-8')
            # Only where an *argument* should open — `href=\"…\"` inside a
            # string is legitimate and common.
            bad = re.findall(r'[(,]\s*\\"', source)
            assert not bad, (
                f'{script.name}: escaped quote opening a token ({len(bad)}x) — '
                f'the file will not parse'
            )



    @pytest.mark.parametrize('script', sorted(JS_DIR.glob('*.js')),
                             ids=lambda p: p.name)
    def test_every_script_is_structurally_whole(self, script):
        """The check above catches one signature; this catches the class.

        There is no JS engine in this test run (no node on the machine), so this
        is a lexer rather than a parser: it walks the file tracking strings,
        template literals, regex literals and both comment forms, then asserts
        that every bracket outside them closes and nothing is left open at EOF.

        That covers what actually goes wrong in a hand edit — a brace lost in a
        move, a string never closed, a comment never terminated — each of which
        is a SyntaxError that kills the whole file while the server serves it
        happily and the page renders without its behaviour. It is not a parser
        and does not pretend to be: it cannot see a misplaced `return` or a
        duplicate `const`. It sees the shape.
        """
        source = script.read_text(encoding='utf-8')
        pairs = {')': '(', ']': '[', '}': '{'}
        stack = []
        i, n, line = 0, len(source), 1
        # Whether a "/" here opens a regex or divides: after a value it divides,
        # after an operator or an opening bracket it opens a regex.
        after_value = False

        while i < n:
            ch = source[i]

            if ch == chr(10):
                line += 1
                i += 1
                continue
            if ch in ' \t\r':
                i += 1
                continue

            if source.startswith('//', i):
                nl = source.find(chr(10), i)
                if nl == -1:
                    break
                i = nl
                continue

            if source.startswith('/*', i):
                end = source.find('*/', i + 2)
                assert end != -1, f'{script.name}:{line} block comment never closed'
                line += source.count(chr(10), i, end)
                i = end + 2
                continue

            if ch == '"' or ch == "'":
                j = i + 1
                while j < n and source[j] != ch:
                    if source[j] == chr(92):
                        j += 2
                        continue
                    assert source[j] != chr(10), (
                        f'{script.name}:{line} string literal never closed'
                    )
                    j += 1
                assert j < n, f'{script.name}:{line} string literal never closed'
                i, after_value = j + 1, True
                continue

            if ch == '`':
                j = i + 1
                while j < n and source[j] != '`':
                    if source[j] == chr(92):
                        j += 2
                        continue
                    if source[j] == chr(10):
                        line += 1
                    j += 1
                assert j < n, f'{script.name}:{line} template literal never closed'
                i, after_value = j + 1, True
                continue

            if ch == '/' and not after_value:
                j, in_class, closed = i + 1, False, False
                while j < n:
                    c = source[j]
                    if c == chr(92):
                        j += 2
                        continue
                    if c == chr(10):
                        break
                    if c == '[':
                        in_class = True
                    elif c == ']':
                        in_class = False
                    elif c == '/' and not in_class:
                        closed = True
                        break
                    j += 1
                if closed:
                    i, after_value = j + 1, True
                    continue
                # A stray slash after all — treat it as an operator.

            if ch in '([{':
                stack.append((ch, line))
                after_value = False
            elif ch in ')]}':
                assert stack, f'{script.name}:{line} closing {ch} with nothing open'
                opener, opened_at = stack.pop()
                assert opener == pairs[ch], (
                    f'{script.name}:{line} {ch} closes {opener} opened at line {opened_at}'
                )
                after_value = True
            elif ch in ';,=+-*<>!&|?:':
                after_value = False
            else:
                after_value = True
            i += 1

        assert not stack, (
            f'{script.name}: {len(stack)} bracket(s) never closed — '
            + ', '.join(f'{ch} at line {ln}' for ch, ln in stack)
        )


class TestDataDirectoryPermissions:
    """DATA_DIR holds every competitor's personal data, the audit trail and (in a
    checkout) the signing key. Encryption at rest was considered and rejected in
    favour of file permissions, because a SQLCipher passphrase kept beside the
    database protects nothing. See config/datasecurity.py."""

    def test_it_restricts_the_directory_and_its_contents(self, tmp_path):
        from config import datasecurity

        (tmp_path / 'db.sqlite3').write_text('x')
        (tmp_path / 'logs').mkdir()
        (tmp_path / 'logs' / 'audit.log').write_text('x')
        if sys.platform != 'win32':
            (tmp_path / 'db.sqlite3').chmod(0o666)
            (tmp_path).chmod(0o777)

        outcome = datasecurity.harden(tmp_path)
        assert 'failed' not in outcome and 'skipped' not in outcome, outcome

        if sys.platform != 'win32':
            assert (tmp_path.stat().st_mode & 0o777) == 0o700
            assert ((tmp_path / 'db.sqlite3').stat().st_mode & 0o777) == 0o600
            assert ((tmp_path / 'logs' / 'audit.log').stat().st_mode & 0o777) == 0o600

    def test_it_can_be_turned_off(self, tmp_path):
        from config import datasecurity

        assert 'skipped' in datasecurity.harden(tmp_path, enabled=False)

    def test_it_never_raises_on_a_directory_it_cannot_touch(self, tmp_path):
        """A data directory on a network share or a FAT stick is a reason to log
        and carry on, not to refuse to time the event."""
        from config import datasecurity

        assert 'skipped' in datasecurity.harden(tmp_path / 'does-not-exist')

    def test_the_windows_grant_names_principals_by_sid(self):
        """SYSTEM and Administrators are localised names; this app is run on
        German-language Windows more often than not."""
        source = (settings.BASE_DIR / 'config' / 'datasecurity.py').read_text(encoding='utf-8')
        assert 'S-1-5-18' in source and 'S-1-5-32-544' in source
        assert '"SYSTEM:' not in source and '"Administrators:' not in source

    def test_the_server_entry_point_hardens_on_startup(self):
        """Here rather than in AppConfig.ready(), which also fires for migrate,
        collectstatic and the test suite — a developer's own file modes are not
        this app's business."""
        source = (settings.BASE_DIR / 'config' / 'asgi.py').read_text(encoding='utf-8')
        assert '_harden_data_directory()' in source


class TestAllowedHosts:
    """A deployment must say which hosts it answers on.

    Empty with DEBUG off, the app *starts* and then refuses every request with
    DisallowedHost — which on race morning reads as "the server is broken" from
    every phone at the venue at once.
    """

    @pytest.fixture(autouse=True)
    def _isolated_data_dir(self, tmp_path):
        """A server start also writes a lock file and sweeps sessions. It does
        that to whatever DATA_DIR says, which in a checkout is the checkout."""
        self.data_dir = tmp_path
        yield

    def _asgi(self, **env):
        """Import config.asgi in a subprocess, which is what a server does."""
        environ = {**os.environ, **{k: v for k, v in env.items() if v is not None}}
        for name, value in env.items():
            if value is None:
                environ.pop(name, None)
        # A lock file of its own, or the developer's own server would refuse it.
        environ['DJANGO_ALLOW_MULTIPLE_SERVERS'] = '1'
        # Importing config.asgi is importing a *server*, and a server hardens its
        # data directory (config/datasecurity.py) — which in a checkout is the
        # checkout. Left on, this test silently breaks ACL inheritance on the
        # developer's own working copy, once per run.
        environ['DJANGO_HARDEN_DATA_DIR'] = 'False'
        environ['SLALOM_DATA_DIR'] = str(self.data_dir)
        return subprocess.run(
            [sys.executable, '-c', 'import config.asgi'],
            cwd=settings.BASE_DIR, env=environ, capture_output=True, text=True,
        )

    def test_a_deployment_with_no_hosts_is_refused_at_startup(self):
        result = self._asgi(DJANGO_DEBUG='False', DJANGO_ALLOWED_HOSTS=None,
                            DJANGO_SECRET_KEY='x' * 50,
                            DJANGO_ALLOW_PLAIN_HTTP='True')
        assert result.returncode != 0
        assert 'DJANGO_ALLOWED_HOSTS' in result.stderr
        # …and the message says what to do about it, not merely what is wrong.
        assert 'DJANGO_ALLOWED_HOSTS=' in result.stderr

    def test_a_deployment_that_names_them_starts(self):
        result = self._asgi(DJANGO_DEBUG='False',
                            DJANGO_ALLOWED_HOSTS='timing.example,127.0.0.1',
                            DJANGO_SECRET_KEY='x' * 50,
                            DJANGO_ALLOW_PLAIN_HTTP='True')
        assert result.returncode == 0, result.stderr

    def test_collectstatic_does_not_need_them(self):
        """A required release step, and the packaged Windows build's own, run with
        DEBUG off and no hosts at all — quite legitimately, since nothing is being
        served. This is why the check is in config/asgi.py and not in settings."""
        result = _run_manage('collectstatic', '--noinput', '--dry-run',
                             DJANGO_DEBUG='False', DJANGO_ALLOWED_HOSTS=None,
                             DJANGO_SECRET_KEY='x' * 50,
                             DJANGO_ALLOW_PLAIN_HTTP='True')
        assert result.returncode == 0, result.stderr


@pytest.mark.django_db
class TestHealthEndpoint:
    """Something a check can be pointed at.

    "Is it up" used to mean opening a page, which means logging in, which means a
    person.
    """

    def test_it_answers_without_a_login(self):
        from django.test import Client

        response = Client().get('/healthz')
        assert response.status_code == 200
        assert response.json() == {'status': 'ok'}

    def test_it_is_registered_as_ungated(self):
        """Not by accident of URL shape: the access middleware gates on the
        (app_name, url_name) pair, so the pair has to be in pages.OPEN."""
        from apps.accounts import pages

        match = resolve('/healthz')
        assert (match.app_name, match.url_name) in pages.OPEN

    def test_it_gives_away_no_venue_state(self):
        """It is unauthenticated, so it must say nothing about which device is
        attached, whether this venue is timing, or how many people are
        registered. One word, the same to everybody."""
        from django.test import Client

        assert set(Client().get('/healthz').json()) == {'status'}

    def test_it_creates_no_session(self):
        """A monitor polling every few seconds must not fill the session table
        the server start has just swept."""
        from django.contrib.sessions.models import Session
        from django.test import Client

        before = Session.objects.count()
        Client().get('/healthz')
        assert Session.objects.count() == before

    def test_a_database_that_has_gone_reports_unhealthy(self):
        """The failure a check exists to catch: a process that is listening but
        whose database is unusable. "The port answers" would report it healthy."""
        from django.test import Client

        from config import health

        class Boom:
            def __enter__(self):
                raise OSError('the disk is full')

            def __exit__(self, *exc):
                return False

        class FakeConnection:
            def cursor(self):
                return Boom()

        original = health.connection
        health.connection = FakeConnection()
        try:
            response = Client().get('/healthz')
        finally:
            health.connection = original
        assert response.status_code == 503
        assert response.json() == {'status': 'error'}

    def test_it_is_never_cached(self):
        """A cached health check is a lie about the present."""
        from django.test import Client

        assert Client().get('/healthz')['Cache-Control'] == 'no-store'

    def test_it_answers_over_plain_http_behind_the_https_redirect(self):
        """The run-book, the unit file and any probe on the box itself ask
        `http://127.0.0.1:8000/healthz`. A 301 to a hostname they are not asking
        for makes every one of them report a healthy server as broken."""
        from django.test import Client

        with override_settings(DEBUG=False, SECURE_SSL_REDIRECT=True,
                               SECURE_REDIRECT_EXEMPT=[r'^healthz$'],
                               ALLOWED_HOSTS=['testserver']):
            response = Client().get('/healthz')
        assert response.status_code == 200

    def test_nothing_else_is_exempt_from_the_https_redirect(self):
        """The exemption is safe only because of what /healthz isn't. A page that
        carries a session cookie must never join this list."""
        import config.settings as project_settings

        source = Path(project_settings.__file__).read_text(encoding='utf-8')
        exempt = re.search(r'SECURE_REDIRECT_EXEMPT = \[(.*?)\]', source, re.S)
        assert exempt, 'SECURE_REDIRECT_EXEMPT is gone — was the health check moved?'
        assert exempt.group(1).count(',') == 0, (
            'something was added to SECURE_REDIRECT_EXEMPT: ' + exempt.group(1)
        )





class TestStatusRowsStayReadable:
    """A run closed with a state code used to fill its whole row with
    amber. Fifteen non-starters in a row — an ordinary morning — turned the Manual
    timing table into a wall of it, with the rows that still needed work invisible
    among them.
    """

    def test_the_row_is_marked_not_filled(self):
        css = CSS.read_text(encoding='utf-8')
        assert '.timing-row--status > td:first-child' in css, (
            'the state-code row lost its edge marker'
        )
        # A full-bleed `background` on the row itself is the thing that was wrong.
        rule = re.search(r'^\.timing-row--status \{([^}]*)\}', css, re.M)
        assert rule is None, f'the row is being filled again: {rule and rule.group(1)}'

    def test_the_code_is_stated_loudly_once(self):
        """The Status cell is a control and shows its own value; the Total cell —
        where a time would have been — is where the outcome is read. Both used to
        be bold amber, which said the same thing twice in the same voice."""
        css = CSS.read_text(encoding='utf-8')
        select = re.search(r'^\.status-select--set \{([^}]*)\}', css, re.M)
        total = re.search(r'^\.run-total--status \{([^}]*)\}', css, re.M)
        assert select and total
        # The control says "set" with its border and its cell tint, and leaves the
        # amber word itself to the Total cell.
        assert 'font-weight' not in select.group(1), select.group(1)
        # `border-color` is fine — a text `color` is the thing that made it shout.
        assert not re.search(r'(^|[;\s])color:', select.group(1)), select.group(1)
        assert 'font-weight: 700' in total.group(1), total.group(1)
        assert 'var(--amber)' in total.group(1), total.group(1)


class TestTheIgnoredRailIsBounded:
    """The real database reached 230 ignored times. Beside a live timing
    table that is not a list anybody reads."""

    def test_it_shows_the_ten_most_recent_per_column(self):
        source = (JS_DIR / 'ignored_panel.js').read_text(encoding='utf-8')
        assert re.search(r'VISIBLE = 10\b', source), 'the per-column cap moved'
        assert 'moreEl' in source, 'the "show all" fold is gone'

    def test_a_chip_does_not_repeat_its_arrival(self):
        """The list is already in arrival order and cut off after ten, so an age
        on every chip was the same fact told twice — and it rode along in a payload
        every open browser re-fetches on every incoming time."""
        source = (JS_DIR / 'ignored_panel.js').read_text(encoding='utf-8')
        assert 'ignored-chip-age' not in source
        assert 'received_at' not in source

    def test_the_payload_does_not_carry_what_nothing_renders(self):
        from apps.timing import autotiming

        source = Path(autotiming.__file__).read_text(encoding='utf-8')
        listing = re.search(r'def ignored_signals\(.*?\n(?=\n\ndef )', source, re.S)
        assert listing, 'ignored_signals moved'
        assert '"received_at"' not in listing.group(0)


class TestTheIgnoredRailStaysReachable:
    """Every operation this rail exists for is a drag *between* it and a run,
    so a rail that scrolls off the top with the first few rows is a rail the
    operator can't use for the rest of the table — and the Lock switch above it,
    which pauses the whole rig, goes with it. Forty runs is an ordinary morning."""

    def _rule(self, css, selector):
        match = re.search(r'^' + re.escape(selector) + r'\s*\{([^}]*)\}', css, re.M)
        assert match, f'{selector} is gone'
        return match.group(1)

    def test_the_rail_is_pinned_below_the_topbar(self):
        css = CSS.read_text(encoding='utf-8')
        rule = self._rule(css, '.ignored-side')
        assert 'position: sticky' in rule, rule
        # Below the topbar, which is itself sticky at top: 0 — a rail pinned at
        # 0 sits underneath it.
        assert 'var(--topbar-h)' in rule, rule
        assert 'max-height' in rule, rule

    def test_the_topbar_height_is_written_once(self):
        """The rail's offset and the topbar's own height are two numbers that
        have to agree, the same trap as --sidebar-w."""
        css = CSS.read_text(encoding='utf-8')
        assert re.search(r'--topbar-h:\s*\d+px;', css)
        assert 'min-height: var(--topbar-h)' in self._rule(css, '.shell-topbar')

    def test_only_the_chip_list_scrolls(self):
        """The count, "show all", the drag hint and above all the Lock switch
        stay on screen however deep the list has grown."""
        css = CSS.read_text(encoding='utf-8')
        assert 'overflow-y: auto' in self._rule(css, '.ignored-box')
        # Both flex items have to be allowed to shrink or the box overflows the
        # cap instead of scrolling inside it.
        assert 'min-height: 0' in self._rule(css, '.ignored-panel')

    def test_the_auto_column_gives_it_room_to_travel(self):
        """`.auto-layout` is `align-items: start`, so this aside is exactly its
        content's height — and a sticky child of a box its own size never
        moves."""
        css = CSS.read_text(encoding='utf-8')
        rule = self._rule(css, '.auto-ignored')
        assert 'align-self: stretch' in rule, rule
        assert 'align-items: flex-start' in rule, rule

    def test_it_lets_go_where_it_stacks_under_the_table(self):
        """Below 1100px both timing views fold to one column: there is nothing
        beside the rail to reach, and pinning it would cover the table."""
        css = CSS.read_text(encoding='utf-8')
        stacked = re.findall(
            r'@media \(max-width: 1100px\) \{(.*?)\n\}', css, re.S)
        release = [block for block in stacked if '.ignored-side' in block]
        assert release, 'the rail stays pinned on a stacked layout'
        assert 'position: static' in release[0], release[0]


class TestFlameOnlyEverMeansPenalty:
    """The Dashboard rendered the current competitor's *total time* in the
    flame colour, which everywhere else in this app — the results table's .rt-pen,
    the penalty chips, pdf._PEN — means "seconds added". A clean run's time read as
    penalised."""

    def test_the_headline_figure_is_not_flame(self):
        css = CSS.read_text(encoding='utf-8')
        rule = re.search(r'^\.dash-figure--primary \.dash-figure-value \{([^}]*)\}',
                         css, re.M)
        assert rule, 'the headline figure rule is gone'
        assert 'flame' not in rule.group(1), rule.group(1)

    def test_the_penalty_figure_is(self):
        css = CSS.read_text(encoding='utf-8')
        rule = re.search(r'^\.dash-figure--penalty \.dash-figure-value \{([^}]*)\}',
                         css, re.M)
        assert rule and 'var(--flame)' in rule.group(1)
        source = (JS_DIR / 'dashboard_overview.js').read_text(encoding='utf-8')
        assert '"penalty"' in source, 'the penalty figure is not toned as one'


class TestTheManualTimingBlankLine:
    """The Manual timing table always leads with an empty line — where whatever
    the operator records next goes. It is a rendered line, not a stored row, and
    the two things that would make it a liability are pinned here:

    * it must not create a row on a *render*. This table is re-rendered by every
      open browser on every incoming time, so a row per render is a row per
      screen per competitor.
    * a burst of edits (a bib, then a status a moment later) must land on **one**
      row, so the line only lets go of the row it made once that row has come
      back from the server and the line on screen is genuinely a new one."""

    @staticmethod
    def _source():
        return (JS_DIR / 'timing_live.js').read_text(encoding='utf-8')

    def test_the_table_leads_with_it(self):
        assert re.search(r'replaceChildren\(\s*addStrip\(\),\s*blankLineRow\(\)',
                         self._source())

    def test_a_render_never_creates_its_row(self):
        render = re.search(r'\n  function render\(\) \{.*?\n  \}', self._source(), re.S)
        assert render, 'render() is gone'
        assert 'addRun' not in render.group(0), render.group(0)
        assert 'blankLineRun' not in render.group(0), render.group(0)

    def test_it_holds_its_row_until_the_server_has_it(self):
        release = re.search(r'function releaseBlankLine\(\) \{.*?\n  \}',
                            self._source(), re.S)
        assert release, 'nothing releases the blank line'
        assert 'state.rows.some' in release.group(0), release.group(0)


class TestRightClickOnlyEverIgnoresATime:
    """Throwing a wrong measurement away is the one thing an operator does in a
    hurry and mid-run, so both timing views take a right-click for it. Putting a
    time *back* deliberately does not: a right-click that lands on the wrong chip
    would then re-pair a real time onto a run, which is the expensive mistake and
    the one nobody would go looking for. So the gesture is one-way on both pages,
    and the rail — the only place a restore lives — has no handler at all."""

    @pytest.mark.parametrize('name', ['timing_live.js', 'auto_timing.js'])
    def test_a_time_chip_ignores_on_right_click(self, name):
        source = (JS_DIR / name).read_text(encoding='utf-8')
        assert 'contextmenu' in source, f'{name} has no right-click handler'
        handler = re.search(
            r'function ignoreOnRightClick\(.*?\n  \}', source, re.S)
        assert handler, f'{name} does not route the right-click through one place'
        body = handler.group(0)
        assert 'preventDefault' in body, body      # else the browser menu opens
        assert re.search(r'\((?:signalId|.*?), true\)', body), body

    @pytest.mark.parametrize('name', ['timing_live.js', 'auto_timing.js'])
    def test_nothing_restores_a_time_on_one(self, name):
        """The only ``ignored`` call a right-click may make is the ignoring one."""
        source = (JS_DIR / name).read_text(encoding='utf-8')
        handler = re.search(r'function ignoreOnRightClick\(.*?\n  \}', source, re.S)
        assert 'false' not in handler.group(0), handler.group(0)

    def test_the_ignored_rail_has_no_right_click_at_all(self):
        source = (JS_DIR / 'ignored_panel.js').read_text(encoding='utf-8')
        assert 'contextmenu' not in source


class TestTheDashboardDoesNotPrintOneTimeTwice:
    """With penalties turned off a run's total time *is* its run time, so the
    current-competitor card printed the same number twice under two labels. The
    timing views keep both — a fixed layout the timekeeper reads at speed — but the
    Dashboard is a glance, so the run time only appears where it can differ."""

    @staticmethod
    def _guard_before(source, figure):
        """The source between the nearest preceding penalties test and ``figure``.
        A ``}`` in it means the guard closed before the figure was appended."""
        at = source.index(figure)
        return source[source.rindex('state.penalties_enabled', 0, at):at]

    def test_the_run_time_figure_is_inside_the_penalty_guard(self):
        source = (JS_DIR / 'dashboard_overview.js').read_text(encoding='utf-8')
        between = self._guard_before(source, 'gettext("Run time")')
        assert '}' not in between, between

    def test_the_total_time_figure_is_not(self):
        source = (JS_DIR / 'dashboard_overview.js').read_text(encoding='utf-8')
        between = self._guard_before(source, 'gettext("Total time")')
        assert '}' in between, 'the total time is gated away with the run time'


class TestNoMadeUpBibNumbers:
    """An unattributed run — a real time no slot owns — rendered "#?" in the
    field an operator reads first, on a tile that already says what it is in words
    and in flame."""

    def test_the_question_mark_bib_is_gone(self):
        for name in ('auto_timing.js', 'timing_live.js'):
            source = (JS_DIR / name).read_text(encoding='utf-8')
            assert '"?" :' not in source and "'?' :" not in source, name

    def test_a_missing_bib_renders_an_empty_chip(self):
        source = (JS_DIR / 'auto_timing.js').read_text(encoding='utf-8')
        assert 'function bibChip' in source
        css = CSS.read_text(encoding='utf-8')
        assert '.auto-tile-bib--none' in css and 'visibility: hidden' in css


class TestAutoTimingWithoutAStartPattern:
    """The page's most common failure mode used to be 100 % alarm —
    every tile a flame-bordered "Unattributed time" card repeating the same
    instruction paragraph. An alarm that fires on every row is not an alarm."""

    @pytest.mark.django_db
    def test_the_page_is_replaced_by_one_sentence(self, client):
        from apps.competitions.models import Competition, CompetitionType

        import datetime

        ctype = CompetitionType.objects.create(name='Slalom')
        Competition.objects.create(competition_type=ctype, name='Test',
                                   date=datetime.date(2026, 5, 1), is_active=True)
        response = client.get(reverse('timing:auto'))
        assert response.context['needs_pattern'] is True
        body = response.content.decode()
        # None of the timing apparatus, and none of its scripts.
        assert 'id="autotiming"' not in body
        assert 'auto_timing.js' not in body
        # …and it says where to go instead.
        assert reverse('competitions:runorder') in body
        assert reverse('timing:manual') in body

    def test_it_reads_as_an_answer_not_a_failed_page(self):
        """The same shape Marshal Posts uses — a heading and a centred card. Both
        timing views said it in a bare left-aligned paragraph at the top of an
        otherwise blank page, which reads as a page that didn't load."""
        for name in ('auto.html', 'live.html'):
            source = (Path(settings.BASE_DIR) / 'templates' / 'timing' / name
                      ).read_text(encoding='utf-8')
            assert 'class="empty-state"' in source, name
            assert 'class="notice"' not in source, (
                f'{name} still states "nothing to operate" as a notice'
            )


class TestEveryDialogIsTheAppsOwn:
    """§7.3 + the owner's rule: an operator only ever sees this app's dialogs.

    A browser dialog is chrome. It wears the OS's styling, cannot be translated
    by us, states its question as one unformatted string (which is why every
    caller was gluing "\\n\\n" into it) and puts the answer behind a control that
    looks nothing like the rest of the page. The single exception is the "leave
    this page?" on tab close, which a page is not allowed to draw itself."""

    def test_nothing_calls_the_browsers_confirm_alert_or_prompt(self):
        offenders = []
        for path in sorted(JS_DIR.rglob('*.js')):
            source = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(encoding='utf-8'),
                            flags=re.S)
            for call in re.findall(r'(?:window\.)?\b(confirm|alert|prompt)\s*\(', source):
                offenders.append(f'{path.name}: {call}()')
        assert offenders == [], offenders

    def test_the_one_allowed_browser_dialog_is_the_tab_closing(self):
        """beforeunload is the exception, and it only fires when there is
        something to lose — a guard that always warns gets clicked through."""
        source = (JS_DIR / 'shell.js').read_text(encoding='utf-8')
        block = re.search(r'window\.addEventListener\("beforeunload".*?\}\);', source, re.S)
        assert block, 'the tab-close guard is gone'
        assert 'if (!dirty || bypass) return;' in block.group(0)

    def test_the_app_dialog_is_on_every_page(self, client):
        body = client.get(reverse('accounts:users')).content.decode()
        assert 'data-app-dialog' in body
        for hook in ('data-dialog-title', 'data-dialog-body',
                     'data-dialog-accept', 'data-dialog-cancel'):
            assert hook in body, hook


class TestModalsManageFocus:
    """Every dialog was `role="dialog" aria-modal="true"` and none of them
    moved focus in, kept it there or gave it back — so Tab walked straight
    through to the page behind the overlay, and a keyboard user could type into a
    form they could not see."""

    def test_no_page_opens_a_modal_behind_the_shared_controller(self):
        """The controller is the only thing that knows about focus, so a page
        toggling `.hidden` itself is a dialog without any of it."""
        offenders = []
        for path in sorted(JS_DIR.rglob('*.js')):
            if path.name == 'shell.js':
                continue
            source = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(encoding='utf-8'),
                            flags=re.S)
            for line in source.splitlines():
                if re.search(r'\w*[Mm]odal\w*\.hidden\s*=', line):
                    offenders.append(f'{path.name}: {line.strip()}')
                if 'classList' in line and 'modal-open' in line:
                    offenders.append(f'{path.name}: {line.strip()}')
        assert offenders == [], offenders

    def test_the_controller_traps_restores_and_adopts(self):
        source = (JS_DIR / 'shell.js').read_text(encoding='utf-8')
        assert 'window.modalController' in source
        # Focus goes in…
        assert 'function focusIn()' in source
        # …Tab wraps rather than leaving…
        assert 'event.shiftKey && document.activeElement === first' in source
        # …and it is handed back to whatever opened the dialog.
        assert 'lastFocused.focus()' in source
        # A dialog the *server* rendered open never called open(), and was the
        # one kind with no focus in it at all.
        assert re.search(r'if \(!modal\.hidden\) \{\s*\n\s*document\.body\.classList\.add',
                         source)

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_every_dialog_is_labelled(self, template):
        source = template.read_text(encoding='utf-8')
        for tag in re.findall(r'<div[^>]*role="dialog"[^>]*>', source):
            assert 'aria-modal="true"' in tag, tag
            assert 'aria-labelledby=' in tag or 'aria-label=' in tag, tag


class TestTheUnsavedGuardCoversLeaving:
    """The guard caught a click on a link we render and nothing else —
    closing the tab and pressing Back both discarded silently — and its own
    "Save changes" then posted through form.submit(), which skips HTML5
    validation and every submit listener."""

    def test_back_is_guarded_too(self):
        source = (JS_DIR / 'shell.js').read_text(encoding='utf-8')
        assert 'popstate' in source, 'the Back button still discards silently'
        assert 'history.pushState' in source, (
            'popstate cannot fire without an entry of our own to pop'
        )

    def test_nothing_submits_a_form_past_its_own_validation(self):
        offenders = []
        for path in sorted(JS_DIR.rglob('*.js')):
            source = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(encoding='utf-8'),
                            flags=re.S)
            for line in source.splitlines():
                # requestSubmit is the one that validates and fires listeners;
                # the bare fallback beside it is for a browser without it.
                if re.search(r'\bform\.submit\(\)', line) and 'requestSubmit' not in source[
                        max(0, source.index(line) - 120):source.index(line)]:
                    offenders.append(f'{path.name}: {line.strip()}')
        assert offenders == [], offenders


class TestTheScalesAreClosed:
    """§7.2. The stylesheet's own rule is that a value appearing twice is a
    token; three kinds of value were exempt from it in practice, and each one
    had a bug living in the gap."""

    def test_nothing_sets_a_raw_z_index(self):
        """The ladder ran 3, 5, 10, 20, 30, 40, 199, 200, 200, 300, 1000
        — 200 shared by the sidebar and the event-changed bar, so which of two
        overlapping *fixed* elements won came down to document order."""
        css = CSS.read_text(encoding='utf-8')
        raw = re.findall(r'z-index:\s*(-?\d+)', css)
        assert raw == [], f'raw z-index values outside the token block: {raw}'

    def test_the_stacking_order_has_no_ties(self):
        from collections import Counter

        css = CSS.read_text(encoding='utf-8')
        steps = re.findall(r'--z-[a-z-]+:\s*(\d+);', css)
        repeated = [value for value, n in Counter(steps).items() if n > 1]
        assert not repeated, f'two layers share a step: {repeated}'

    def test_nothing_sets_a_raw_transition_duration(self):
        """Seven durations were in use, several a rounding apart, so the
        same interaction felt different depending on the component."""
        css = CSS.read_text(encoding='utf-8')
        raw = []
        for decl in re.findall(r'transition:[^;}]*', css):
            raw += re.findall(r'(?<!\()\b\d*\.?\d+s\b', decl)
        assert raw == [], f'raw durations: {raw}'

    def test_spacing_and_type_come_from_the_scales(self):
        """Seven lengths sat outside them — chevron nudges, a drag-gap
        height, a `font-size: 0.85em`."""
        css = CSS.read_text(encoding='utf-8')
        offenders = []
        for line in css.splitlines():
            if '--' in line.split(':')[0]:      # the token block's own declarations
                continue
            for decl in re.findall(
                    r'(?:padding|margin|gap|font-size)[a-z-]*:\s*[^;}]+', line):
                if 'var(--' in decl or 'clamp(' in decl:
                    continue
                if re.search(r'[\d.]+(px|rem|em)', decl):
                    offenders.append(line.strip())
        assert offenders == [], offenders

    def test_the_sidebar_width_is_written_once(self):
        """The width and the room the main column leaves for it are two
        numbers that must agree, and were written out separately."""
        css = CSS.read_text(encoding='utf-8')
        assert re.search(r'--sidebar-w:\s*\d+px;', css)
        # Both declarations read the token. (Not "240px appears once in the
        # file": an unrelated dropdown's scroll cap is 240px too, and coupling
        # those two numbers is the bug this fixes, not the fix.)
        for rule, prop in (('.shell-sidebar', 'width'), ('.shell-main', 'margin-left')):
            block = re.search(re.escape(rule) + r'\s*\{([^}]*)\}', css)
            assert block, rule
            assert f'{prop}: var(--sidebar-w)' in block.group(1), rule


class TestKeyboardFocusIsVisible:
    """There is a correct global `:focus-visible` outline — and then eight
    component rules turning the outline off, every one of which outranks it on
    specificity (`form input:focus` beats `input:focus-visible`). So on almost
    every input in the app a keyboard user got a border-colour change, and on
    the PDF header editor nothing at all: its `outline: none` was on the element
    rather than on `:focus`, so no state could bring it back."""

    def test_no_rule_suppresses_a_visible_focus_ring(self):
        # Comments out first — several of them quote the declaration they are
        # explaining, and a comment is not a rule.
        css = re.sub(r'/\*.*?\*/', '', CSS.read_text(encoding='utf-8'), flags=re.S)
        for line_no, line in enumerate(css.splitlines(), 1):
            if 'outline: none' not in line:
                continue
            # Walk back to the selector this declaration belongs to.
            selector = line if '{' in line else _selector_above(css, line_no)
            assert ':not(:focus-visible)' in selector, (
                f'main.css:{line_no} takes the focus ring away for good: {selector!r}'
            )

    def test_the_global_rule_still_covers_everything_focusable(self):
        css = CSS.read_text(encoding='utf-8')
        rule = re.search(r'((?:[^{}]*:focus-visible,\s*)+[^{}]*:focus-visible)\s*\{'
                         r'([^}]*outline:[^;]*;[^}]*)\}', css)
        assert rule, 'the global :focus-visible outline is gone'
        for tag in ('a', 'button', 'input', 'select', 'textarea'):
            assert f'{tag}:focus-visible' in rule.group(1), tag


def _selector_above(css, line_no):
    """The selector of the block a declaration on `line_no` sits in."""
    lines = css.splitlines()
    for candidate in reversed(lines[:line_no]):
        if '{' in candidate:
            return candidate.strip()
    return ''


def _url_names_in_the_project():
    """Every (app_name, url_name) pair the project's URLconf can resolve to."""
    from django.urls import get_resolver
    from django.urls.resolvers import URLPattern, URLResolver

    found = set()

    def walk(resolver, app_name):
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                walk(entry, entry.app_name or app_name)
            elif isinstance(entry, URLPattern) and entry.name:
                found.add((app_name, entry.name))

    walk(get_resolver(), '')
    return found


class TestThePagesCanBeUsedWithoutAMouse:
    """The suite had no accessibility assertions at all, which is how four
    defects shipped together: a focus ring removed on almost every input, no
    modal focus management, a hard-coded aria-expanded, and a table row
    claiming to be a button.

    These are file tests over the templates rather than a rendered-page audit —
    cheap, and they catch the whole class on the way in. The rendered halves live
    in TestModalsManageFocus and TestKeyboardFocusIsVisible."""

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_every_control_has_a_name(self, template):
        """A button with only an icon in it is an unlabelled button to a screen
        reader — "button", and nothing else. The × on a dialog, the chevron on a
        participant row and the hamburger are all this shape."""
        source = template.read_text(encoding='utf-8')
        source = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '',
                        source, flags=re.S)
        for match in re.finditer(r'<button([^>]*)>(.*?)</button>', source, re.S):
            attrs, inner = match.group(1), match.group(2)
            if 'aria-label' in attrs or 'aria-labelledby' in attrs:
                continue
            # Text the user can actually read: tags and template tags removed.
            text = re.sub(r'<[^>]*>|\{%.*?%\}', '', inner)
            text = re.sub(r'\{\{.*?\}\}', 'x', text).strip()
            assert text, (
                f'{template.name}: a <button> with no text and no aria-label — '
                f'{match.group(0)[:80]}'
            )

    @pytest.mark.parametrize('template', sorted(TEMPLATE_DIR.rglob('*.html')), ids=str)
    def test_no_element_claims_to_be_something_it_is_not(self, template):
        """`<tr role="button">` told a screen reader the row was not a row: no
        column headers, no position in the table. If a thing behaves like a
        button it should *be* one — see participant_list.html."""
        source = template.read_text(encoding='utf-8')
        source = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '',
                        source, flags=re.S)
        for tag in ('tr', 'td', 'th', 'table', 'ul', 'ol', 'li'):
            offenders = re.findall(rf'<{tag}[^>]*role="(?:button|link|checkbox)"',
                                   source)
            assert not offenders, f'{template.name}: <{tag} role=…> — {offenders}'

    def test_the_hamburger_does_not_render_a_state_the_server_cannot_know(self):
        """The sidebar is shown by default on desktop and off-canvas on
        mobile, so a rendered aria-expanded is simply wrong on a phone until the
        script runs. shell.js sets it from the real state on load."""
        source = (TEMPLATE_DIR / 'base.html').read_text(encoding='utf-8')
        hamburger = re.search(r'<button[^>]*id="shell-hamburger"[^>]*>', source)
        assert hamburger, 'the hamburger is gone'
        assert 'aria-expanded' not in hamburger.group(0)
        assert 'aria-controls="shell-sidebar"' in hamburger.group(0)
        assert 'syncAria' in (JS_DIR / 'shell.js').read_text(encoding='utf-8')

    def test_a_message_is_announced_and_can_be_dismissed(self):
        """Django's messages were a plain <ul>, so "Timing settings
        saved" never reached a screen reader — the page simply had one more list
        on it than before."""
        source = (TEMPLATE_DIR / 'base.html').read_text(encoding='utf-8')
        block = re.search(r'<ul class="messages"[^>]*>', source)
        assert block, 'the messages block is gone'
        assert 'role="status"' in block.group(0), (
            'messages are added to the page silently'
        )
        assert 'notice-dismiss' in (JS_DIR / 'shell.js').read_text(encoding='utf-8')

    @pytest.mark.django_db
    def test_every_page_offers_a_way_past_the_navigation(self, client):
        """A sidebar of twenty links in front of every page is the reason skip
        links exist."""
        body = client.get(reverse('accounts:users')).content.decode()
        assert 'class="skip-link"' in body
        assert 'href="#main-content"' in body
        assert 'id="main-content"' in body


class TestTheSidebarMarksOnePage:
    """Opening Competition Setup -> Results marked Timing -> Settings as well.

    Both sidebar entries decided they were current by comparing
    `request.resolver_match.url_name` against "settings", and a url_name is only
    unique within its app: `results:settings` and `timing:settings` are two
    different pages with the same name. Some entries did name their app and some
    didn't, so the collision was an accident of which line you read.

    The mapping now lives in apps/nav.py, keyed on the (app, url_name) pair. The
    two tests below are the reason that fixes the class of bug rather than this
    one instance: no pair may appear under two entries, and every pair must
    still exist in the URLconf.
    """

    def test_no_url_can_mark_two_entries(self):
        from apps import nav

        seen = {}
        for entry, urls in nav.ITEMS.items():
            for url in urls:
                assert url not in seen, (
                    f'{url[0]}:{url[1]} marks both {seen.get(url)} and {entry}'
                )
                seen[url] = entry

    def test_every_mapped_url_still_exists(self):
        """A renamed route would otherwise mark nothing, silently, on a page
        nobody thinks to re-check."""
        from apps import nav

        real = _url_names_in_the_project()
        for entry, urls in nav.ITEMS.items():
            for app_name, url_name in urls:
                assert (app_name, url_name) in real, (
                    f'{entry} points at {app_name}:{url_name}, which no longer exists'
                )

    def test_every_nested_entry_has_a_parent(self):
        from apps import nav

        nested = {name for name in nav.ITEMS if '.' in name}
        claimed = {child for children in nav.PARENTS.values() for child in children}
        assert nested == claimed
        assert not (set(nav.PARENTS) & set(nav.ITEMS)), (
            'a parent is a heading, never a page of its own'
        )

    @pytest.mark.parametrize('url_name, expected', [
        ('timing:dashboard', {'dashboard'}),
        ('competitions:list', {'setup', 'setup.manage'}),
        ('competitions:general', {'setup', 'setup.general'}),
        ('competitions:classes', {'setup', 'setup.classes'}),
        ('competitions:runorder', {'setup', 'setup.runorder'}),
        ('competitions:penalties', {'setup', 'setup.penalties'}),
        # The page this was all about: a Competition Setup page served by the
        # results app.
        ('results:settings', {'setup', 'setup.results'}),
        ('participants:list', {'participants'}),
        ('timing:manual', {'timing', 'timing.manual'}),
        ('timing:auto', {'timing', 'timing.auto'}),
        ('timing:settings', {'timing', 'timing.settings'}),
        ('competitions:marshal-posts', {'marshal_posts'}),
        ('results:index', {'results', 'results.index'}),
        ('transfer:backup', {'backup', 'backup.backup'}),
        ('transfer:export', {'backup', 'backup.export'}),
        ('transfer:import', {'backup', 'backup.import'}),
        ('transfer:review', {'backup', 'backup.import'}),
    ])
    def test_each_page_marks_exactly_its_own_entry(self, url_name, expected):
        from apps import nav

        assert nav.current(resolve(reverse(url_name))) == expected

    def test_a_page_with_no_sidebar_entry_marks_nothing(self):
        from apps import nav

        # The simulator opens in its own tab without the app shell, and the
        # audit log hangs off the sidebar footer rather than the nav.
        for name in ('timing:simulator', 'accounts:audit-log'):
            assert nav.current(resolve(reverse(name))) == frozenset()

    @pytest.mark.django_db
    def test_the_rendered_sidebar_marks_one_link(self, client):
        """End to end, through the template: the bug was visible on screen."""
        import datetime

        from apps.competitions.models import Competition, CompetitionType

        ctype = CompetitionType.objects.create(name='Slalom')
        Competition.objects.create(competition_type=ctype, name='Test',
                                   date=datetime.date(2026, 5, 1), is_active=True)

        body = client.get(reverse('results:settings')).content.decode()
        sidebar = body[body.index('<nav class="shell-nav">'):body.index('</nav>')]
        marked = re.findall(r'<a class="shell-nav-[^"]*\bactive\b[^"]*" href="([^"]+)"',
                            sidebar)
        assert sorted(marked) == sorted([
            reverse('competitions:list'),   # the Competition Setup parent
            reverse('results:settings'),    # its Results sub-page
        ]), marked
        assert reverse('timing:settings') not in marked


class TestTheSecurityLogGoesSomewhere:
    """The project configured no LOGGING at all, which hid two things.

    Python's last-resort handler emits WARNING and above, so `throttle.note_success`
    — "who signed in", written at INFO — was dropped on the floor: the login trail
    the throttle module exists to keep did not reach anywhere. And nothing was
    written to a *file*, so on the packaged Windows build (Daphne in a console
    window closed at the end of the day) failed logins and "this time could not be
    stored" went with it. Both are exactly the records somebody asks for a week
    later, which is why the absence was invisible until it mattered.
    """

    def test_the_project_configures_logging(self, settings):
        assert getattr(settings, 'LOGGING', None), 'no LOGGING configuration at all'

    def test_a_handler_writes_to_a_file_under_the_data_directory(self, settings):
        handlers = settings.LOGGING['handlers']
        files = [h for h in handlers.values()
                 if 'FileHandler' in h.get('class', '')]
        assert files, 'nothing is written to a file, only to a console that closes'
        for handler in files:
            assert str(handler['filename']).startswith(str(settings.DATA_DIR)), (
                'a log file outside DATA_DIR is overwritten by the next installer'
            )

    def test_the_login_trail_is_captured_at_info(self, settings):
        """The throttle logs a successful sign-in at INFO. A configuration that
        only keeps WARNING and above keeps none of it."""
        from apps.accounts import throttle

        logger = settings.LOGGING['loggers'].get(throttle.logger.name) \
            or settings.LOGGING['loggers'].get('apps') \
            or settings.LOGGING['root']
        assert logger['level'] in ('DEBUG', 'INFO'), logger


class TestEveryDateInputShowsItsDate:
    """`<input type="date">` reads exactly one value format — ISO — and silently
    ignores anything else, leaving the control **empty**. Django renders a date
    through the active locale, so on the shipped default language (German) a
    stored 15 June 2010 arrived as `15.06.2010`, the browser dropped it, and an
    operator opening an existing participant saw no birthday. Nothing errored;
    the value was still in the database.

    Both dates this app edits had it (issue #1), independently, because each
    declared its own widget — which is why the fix is one shared widget
    (`apps.common.DateInput`) and this test is about the *class* of bug: any date
    field anywhere in the project, including one added next month, must render a
    format the picker will actually accept.

    The suite is pinned to English (conftest), and English's first
    DATE_INPUT_FORMATS entry happens to be ISO, so every existing test rendering
    a form passed while the shipped language was broken. Hence the explicit
    languages below rather than the ambient one.
    """

    ISO = '%Y-%m-%d'

    def _date_widgets(self):
        """Every date widget declared by any form in the project, found rather
        than listed — a widget nobody remembered to add here is the whole
        failure mode."""
        import importlib

        from django import forms

        found = []
        for path in sorted(Path(settings.BASE_DIR).glob('apps/*/forms.py')):
            module = importlib.import_module(f'apps.{path.parent.name}.forms')
            for name in dir(module):
                candidate = getattr(module, name)
                if not (isinstance(candidate, type)
                        and issubclass(candidate, forms.BaseForm)):
                    continue
                for field_name, field in getattr(candidate, 'base_fields', {}).items():
                    widget = field.widget
                    if isinstance(widget, forms.DateInput) or \
                            getattr(widget, 'input_type', None) == 'date':
                        found.append((f'{name}.{field_name}', widget))
        return found

    def test_the_project_still_has_date_inputs_to_check(self):
        """If the discovery breaks, everything below passes vacuously."""
        assert len(self._date_widgets()) >= 2, self._date_widgets()

    def test_every_date_widget_renders_iso(self):
        for label, widget in self._date_widgets():
            assert widget.format == self.ISO, (
                f'{label} renders {widget.format!r}; a native date picker reads '
                f'{self.ISO!r} and drops anything else, showing an empty field'
            )

    def test_a_date_survives_rendering_in_every_shipped_language(self):
        """The guarantee stated the way the operator meets it, and asked of every
        language we ship rather than of German alone."""
        import datetime

        from django.utils import translation

        from apps.common import DateInput

        for code, _label in settings.LANGUAGES:
            with translation.override(code):
                rendered = DateInput().render('date', datetime.date(2010, 6, 15))
            assert 'value="2010-06-15"' in rendered, (
                f'a date rendered for {code} as {rendered!r}'
            )
