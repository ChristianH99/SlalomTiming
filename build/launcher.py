"""What the desktop shortcut of the packaged Windows build actually runs.

The installed layout is not a checkout, so this file is the difference between the
two:

    <install>\\python\\SlalomTiming.exe   the bundled interpreter + every dependency
    <install>\\app\\                      this repository's code, replaced wholesale
                                          by the next installer
    %LOCALAPPDATA%\\SlalomTiming\\data\\   db.sqlite3, media\\, .env, run\\, logs
                                          — never touched by install or uninstall

That split is the whole point: the database is the event, so it cannot live next to
code that an upgrade overwrites. ``SLALOM_DATA_DIR`` (config/settings.py) is what
moves it, and this file is what sets it.

Everything else here is the "one click" part — on first run there is no .env, no
database and no operator account, and a person who just double-clicked an icon is
not going to be handed a command line. So: generate the secret key, migrate, ask
once for the first login, open the browser, run one Daphne process in the
foreground. Closing the window stops the server.

Run modes:
    SlalomTiming.exe launcher.py              serve (what the shortcut does)
    SlalomTiming.exe launcher.py --selftest   used by build/build.ps1 to prove the
                                              payload works before it is packaged
"""

import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent          # <install>\app
INSTALL_DIR = APP_DIR.parent                       # <install>
DEFAULT_PORT = 8000
DEFAULT_BIND = '127.0.0.1'

# How the operator is told what is happening. This window *is* the app's status
# light during an event, so it says the URL, the data directory and how to stop.
BANNER = r"""
  ____  _       _                 _____ _           _
 / ___|| | __ _| | ___  _ __ ___ |_   _(_)_ __ ___ (_)_ __   __ _
 \___ \| |/ _` | |/ _ \| '_ ` _ \  | | | | '_ ` _ \| | '_ \ / _` |
  ___) | | (_| | | (_) | | | | | | | | | | | | | | | | | | | (_| |
 |____/|_|\__,_|_|\___/|_| |_| |_| |_| |_|_| |_| |_|_|_| |_|\__, |
                                                            |___/
"""


def data_dir():
    """Where everything this app writes lives (see the module docstring)."""
    override = os.environ.get('SLALOM_DATA_DIR')
    if override:
        return Path(override)
    base = os.environ.get('LOCALAPPDATA') or Path.home()
    return Path(base) / 'SlalomTiming' / 'data'


def load_env_file(path):
    """Put KEY=VALUE lines into the process environment.

    settings.py reads os.environ and Django loads no .env of its own, so this is
    the only thing that applies the operator's configuration. Existing variables
    win, which is what lets the build's --selftest override the whole file.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        os.environ.setdefault(name.strip(), value.strip())


def write_default_env(path):
    """First run: a working, *deliberate* configuration rather than a prompt.

    DEBUG is off (this is a real install, not a checkout), so a secret key has to
    exist or settings.py refuses to start — it is generated here, once, and stays
    in the data directory across upgrades.

    The default is 127.0.0.1 with DJANGO_ALLOW_PLAIN_HTTP: one laptop, nothing on
    the network, which is exactly the case DEPLOYMENT.md §3.4 option C allows. It
    is NOT a licence to move the bind address to 0.0.0.0 and leave it — putting
    this on venue Wi-Fi without TLS puts every password and session cookie on the
    air. The file says so where the person changing it will read it.
    """
    from django.core.management.utils import get_random_secret_key

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '# Slalom Timing configuration. Edit with any text editor, then restart\n'
        '# the app. Full reference: .env.example and DEPLOYMENT.md in the install\n'
        '# folder.\n'
        '\n'
        '# Signs sessions and password-reset tokens. Generated once for this\n'
        '# machine on first run — keep it, and keep it secret.\n'
        f'DJANGO_SECRET_KEY={get_random_secret_key()}\n'
        'DJANGO_DEBUG=False\n'
        '\n'
        '# --- Who may reach this server ---\n'
        '# Default: this laptop only. To let marshals\' phones and other devices in,\n'
        '# add the machine\'s name/IP here AND set SLALOM_BIND=0.0.0.0 below.\n'
        'DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost\n'
        f'SLALOM_BIND={DEFAULT_BIND}\n'
        f'SLALOM_PORT={DEFAULT_PORT}\n'
        '\n'
        '# --- TLS ---\n'
        '# Running without TLS is safe only while SLALOM_BIND stays on 127.0.0.1:\n'
        '# nothing else can connect. The moment you open this to a network, anything\n'
        '# else on that network can read passwords and steal a timekeeper\'s session.\n'
        '# Read the TLS section of DEPLOYMENT.md (Caddy with a local CA, or\n'
        '# Tailscale) before you change the bind address.\n'
        'DJANGO_ALLOW_PLAIN_HTTP=True\n',
        encoding='utf-8',
    )


def configure(argv):
    """Resolve the layout and configuration, before Django is imported."""
    sys.path.insert(0, str(APP_DIR))
    os.chdir(APP_DIR)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('SLALOM_DATA_DIR', str(data))
    # Collected at build time and shipped read-only beside the code: with DEBUG
    # off nothing serves /static/ until STATIC_ROOT exists, and the install
    # directory is the only place it can be.
    os.environ.setdefault('DJANGO_STATIC_ROOT', str(APP_DIR / 'staticfiles'))

    env_file = data / '.env'
    if not env_file.exists():
        # Needs Django on the path but not a configured one — get_random_secret_key
        # imports nothing from settings.
        write_default_env(env_file)
    load_env_file(env_file)

    if '--port' in argv:
        os.environ['SLALOM_PORT'] = argv[argv.index('--port') + 1]
    if '--bind' in argv:
        os.environ['SLALOM_BIND'] = argv[argv.index('--bind') + 1]
    return data, env_file


def free_port(bind, wanted):
    """The port to serve on, stepping past one something else already holds.

    A laptop that runs anything else on 8000 would otherwise give a double-clicker
    a Twisted traceback and no server.
    """
    for candidate in range(wanted, wanted + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('' if bind == '0.0.0.0' else bind, candidate))
            except OSError:
                continue
        return candidate
    return wanted


def ensure_database():
    """Apply migrations. On first run this is what creates db.sqlite3."""
    from django.core.management import call_command

    call_command('migrate', interactive=False, verbosity=0)


def ensure_operator_account():
    """Every page in this app requires a login, so a fresh database is unusable.

    Asked here rather than in the installer: the account belongs to the database,
    and the database outlives the installation.
    """
    from django.contrib.auth import get_user_model

    if get_user_model().objects.exists():
        return
    from django.core.management import call_command

    print('')
    print('  No user account exists yet, and every page needs a login.')

    if not sys.stdin or not sys.stdin.isatty():
        # Started without a console (a scheduled task, a service wrapper): asking
        # would loop forever on a closed stdin. Say what to do and serve anyway —
        # the login page still renders, and the account can be made from a shortcut.
        print('  This window cannot ask for one. Start Slalom Timing from its')
        print('  desktop shortcut once to create the first account.')
        print('')
        return

    print('  Create the first operator account now.')
    print('')
    for attempt in range(3):
        try:
            call_command('createsuperuser')
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - a mistyped password must not be fatal
            print(f'  Could not create the account: {exc}')
            if attempt < 2:
                print('  Try again.')
    print('  Carrying on without an account — the app will start, but nobody can')
    print('  log in yet. Restart it to try again.')


def open_browser_when_ready(url, bind, port):
    """Open the browser only once the socket answers, so it never lands on an error."""
    def wait():
        target = '127.0.0.1' if bind in ('0.0.0.0', '') else bind
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((target, port), timeout=0.5):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.3)

    threading.Thread(target=wait, daemon=True).start()


def hold_installer_off():
    """A named mutex the installer looks for (AppMutex in build/installer.iss).

    Overwriting a running server's files fails halfway through and leaves a broken
    install; with this, Setup asks the operator to close the app instead.
    """
    if sys.platform != 'win32':
        return
    import ctypes

    ctypes.windll.kernel32.CreateMutexW(None, False, 'SlalomTimingRunning')


def set_console_title(text):
    if sys.platform == 'win32':
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)


def selftest():
    """Prove the packaged payload actually works, from build.ps1.

    Checks the three things that are only wrong *after* packaging and are invisible
    until an operator double-clicks: a dependency that did not get vendored, a
    database that cannot be created, and a static manifest that collectstatic never
    wrote (which renders every page unstyled with dead timing views).
    """
    import django

    django.setup()
    from django.templatetags.static import static

    ensure_database()
    from django.contrib.auth import get_user_model

    get_user_model().objects.exists()

    url = static('css/main.css')
    if 'main.' not in url:
        raise SystemExit(f'static manifest looks wrong: {url}')

    from django.test import Client

    # SERVER_NAME because this runs against the *shipped* configuration: DEBUG is
    # off, so ALLOWED_HOSTS is enforced and Django's usual "testserver" host is not
    # in it. Asking as 127.0.0.1 is what the operator's browser will do.
    response = Client().get('/', follow=True, SERVER_NAME='127.0.0.1')
    if response.status_code != 200:
        raise SystemExit(f'GET / returned {response.status_code}, expected the login page')

    import daphne  # noqa: F401  - the server itself has to be importable

    print(f'selftest OK  (Django {django.get_version()}, static {url})')


def serve(data, env_file):
    import django

    django.setup()
    ensure_database()
    ensure_operator_account()

    bind = os.environ.get('SLALOM_BIND', DEFAULT_BIND)
    port = free_port(bind, int(os.environ.get('SLALOM_PORT', DEFAULT_PORT)))
    shown = '127.0.0.1' if bind in ('0.0.0.0', '') else bind
    url = f'http://{shown}:{port}/'

    print(BANNER)
    print(f'  Open the app at   {url}')
    if bind == '0.0.0.0':
        print('  Other devices     http://<this machine\'s IP>:%d/  (add it to' % port)
        print('                    DJANGO_ALLOWED_HOSTS in the file below, and read')
        print('                    the TLS section of DEPLOYMENT.md)')
    print(f'  Your data         {data}')
    print(f'  Settings          {env_file}')
    print('')
    print('  Leave this window open — it IS the server. Close it (or press Ctrl+C)')
    print('  to stop timing.')
    print('')

    hold_installer_off()
    open_browser_when_ready(url, bind, port)

    from daphne.cli import CommandLineInterface

    CommandLineInterface().run(['-b', bind, '-p', str(port), 'config.asgi:application'])


def main(argv):
    data, env_file = configure(argv)
    if '--selftest' in argv:
        selftest()
        return
    set_console_title('Slalom Timing — server')
    serve(data, env_file)


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        # A double-clicked window that closes instantly tells the operator nothing.
        import traceback

        traceback.print_exc()
        print('')
        print('Slalom Timing could not start. The error above says why.')
        try:
            input('Press Enter to close this window. ')
        except EOFError:
            pass
        sys.exit(1)
