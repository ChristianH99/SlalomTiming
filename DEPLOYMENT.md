# Deploying Slalom Timing

How to run this app for a real event, instead of `manage.py runserver`. Everything
here assumes the whole event lives on **one machine**: the timekeeping laptop, or a
small box at the venue.

Two rules the rest of this document is built around:

1. **One server process.** The live-update channel layer (`InMemoryChannelLayer`),
   the CP540 reader thread and the event loop they post onto all live *inside* the
   process. A second worker gets its own copies, and browsers attached to the wrong
   one silently stop receiving updates. `config/singleinstance.py` takes a lock file
   at startup so a second process fails loudly instead of half-working.
2. **The database is the event.** `db.sqlite3` holds every recorded time. Treat it
   the way you'd treat the paper sheets.

---

## 1. Install (once per machine)

From a checkout, below. If the venue machine should not have a checkout at all, build
the Windows installer instead and skip to section 3 — see *Windows without a checkout*.

```bash
git clone <this repo> /srv/slalomtiming      # or C:\SlalomTiming on Windows
cd /srv/slalomtiming
uv sync                                      # Python 3.14 + dependencies
cp .env.example .env                         # then edit it — see section 2
uv run python manage.py migrate
uv run python manage.py createsuperuser      # the first operator account
uv run python manage.py collectstatic --noinput
```

`collectstatic` is **not optional**: with `DJANGO_DEBUG=False` nothing serves
`/static/` until it has run, and every page renders unstyled with dead timing views.
It is repeated on every release below for the same reason.

## 2. Configure

All configuration comes from the process environment. `.env.example` is the
annotated list; copy it to `.env` and fill it in. `.env` is gitignored — it holds
the secret key.

| Variable | Required | What it does |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | **yes** | Signs sessions. The app *refuses to start* with `DEBUG=False` and no key, because the fallback key is public in this repository. Generate: `uv run python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"` |
| `DJANGO_DEBUG` | **yes** | `False` for any deployment. Turns on the HTTPS/cookie hardening block. |
| `DJANGO_ALLOWED_HOSTS` | **yes** | Comma-separated hostnames/IPs the site answers on, e.g. `timing.local,192.168.1.10`. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | with HTTPS | e.g. `https://timing.local` — needed for form posts from the real host. |
| `TIMING_DEVICE_TOKEN` | if a networked HTTP device posts times | Shared secret for `POST /timing/signal/`. The CP540 doesn't need it (it feeds the reader thread directly). |
| `DJANGO_STATIC_ROOT` | no | Where `collectstatic` writes. Default `staticfiles/` in the project. |
| `DJANGO_MEDIA_ROOT` | no | Uploaded results-PDF logos. Default `media/`. |
| `DJANGO_SERVE_MEDIA` | no | `False` when the reverse proxy serves `MEDIA_ROOT` itself. |
| `DJANGO_ALLOW_PLAIN_HTTP` | no | The only way to run without TLS, and a decision — see section 3.4. The old `DJANGO_SECURE_SSL_REDIRECT` / `DJANGO_SECURE_COOKIES` overrides now refuse to start. |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_PRELOAD` | no | Defaults 300 s / off. Only raise them with a stable public domain (section 3.4). |
| `DJANGO_SESSION_HOURS` | no | How long a login lasts. Default 12 — one event day. |
| `DJANGO_LOGIN_MAX_ATTEMPTS`, `DJANGO_LOGIN_LOCKOUT_SECONDS` | no | Failed logins per username+IP before a lockout, and its length. Defaults 10 / 300 s. |
| `DJANGO_ALLOW_MULTIPLE_SERVERS` | no | Disables the single-process lock. Only correct if you have swapped `CHANNEL_LAYERS` for `channels_redis`. |

## 3. Serve

### Windows (the timekeeping laptop)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\start-server.ps1
```

The script loads `.env`, runs `check --deploy`, `migrate` and `collectstatic`, then
runs one Daphne process in the foreground. Ctrl+C or closing the window stops it.
`-Port 8000` and `-Bind 0.0.0.0` are options; `-SkipChecks` restarts mid-event
without touching the database or static files.

### Windows without a checkout — the packaged installer

If the timekeeping laptop should not have a git checkout, a Python or a terminal on
it, build the installer instead (**[build/README.md](build/README.md)**):

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1     # on YOUR machine
```

That produces `dist\SlalomTiming-Setup-<version>.exe` — one ~28 MB file carrying its
own interpreter and every dependency. Copy it to the laptop, run it, and the operator
gets a desktop icon. No administrator account needed. Sections 1 and 2 above do not
apply: the first start generates the secret key, creates the database and asks for the
operator login by itself.

Two differences from a checkout are worth knowing before an event:

- **The event data is somewhere else.** `%LOCALAPPDATA%\SlalomTiming\data` holds
  `db.sqlite3`, `media\`, `.env` and `run\` — deliberately outside the program folder,
  so upgrading or uninstalling cannot take an event with it. That is the folder to back
  up (section 5), and the Start Menu has a shortcut to it.
- **It serves 127.0.0.1 only, until you say otherwise.** That is the plain-HTTP case
  section 3.4 allows. Letting marshals' phones in means editing `SLALOM_BIND` and
  `DJANGO_ALLOWED_HOSTS` in that `.env` — and reading 3.4 first, because at that point
  the app is on the venue network without TLS.

### Linux (a venue box)

```bash
sudo cp deploy/slalomtiming.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slalomtiming
journalctl -u slalomtiming -f
```

Both run the same command — one Daphne process:

```bash
uv run daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

Do **not** add `--workers`, do not run it under a process manager that spawns
several copies, and do not use `runserver`: it is a development server, and Django
says plainly that it is not for production use.

### 3.4 TLS — pick one of these before the first event

The app defaults to HTTPS and **there is one switch that turns that off**
(`DJANGO_ALLOW_PLAIN_HTTP`). It exists so the decision is visible, not so it is easy:
on plain HTTP every password and every session cookie on the venue Wi-Fi is readable
by anything else on that network — a marshal's phone, a spectator's laptop, whatever
joined the guest SSID. Stealing a timekeeper's session is enough to change results.

**Option A — Caddy with its own local CA (the default answer).**
`deploy/Caddyfile` puts Caddy in front of Daphne, terminating TLS and passing
WebSockets through untouched. No internet and no public domain needed:

```bash
caddy run --config deploy/Caddyfile
```

Point `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` at the hostname you
serve, and install Caddy's root certificate on every device that opens the app
(`caddy trust` on the server prints where it lives; phones import it manually) —
otherwise every browser shows a warning. Do this once, in the workshop, not in the
paddock: it is the one step that needs every device in your hands.

**Option B — [Tailscale](https://tailscale.com/).** If every operator device can join
one tailnet, `tailscale cert` + serving on the tailnet address gives you real
certificates and takes the venue Wi-Fi out of the picture entirely. Needs internet
once, to enrol the devices.

**Option C — plain HTTP.** Only for a single laptop with nothing else on the network
(and then you may as well bind to `127.0.0.1`). Set `DJANGO_ALLOW_PLAIN_HTTP=True`;
every management command and the server log will say what you have done.

On HSTS: the defaults are deliberately short (300 s, no preload). HSTS pins a hostname
to HTTPS in every browser that saw the header and **cannot be revoked before it
expires** — with a reused venue hostname and a local CA, a one-year pin means a laptop
that once opened `timing.local` here refuses plain HTTP at the next venue for the rest
of the year. Raise `DJANGO_HSTS_SECONDS` (and add `DJANGO_HSTS_PRELOAD=True`) once you
have a stable public domain.

## 4. Release a new version

```bash
git pull
uv sync
uv run python manage.py migrate --noinput
uv run python manage.py collectstatic --noinput
sudo systemctl restart slalomtiming      # or restart start-server.ps1
```

Never mid-event. Static file names are content-hashed, so browsers pick up new CSS
and JS on the next load without a forced refresh.

## 5. Race day

**Before the event**

- [ ] Server started; open `/` and confirm the Dashboard renders **styled** (an
      unstyled page means `collectstatic` didn't run).
- [ ] Log in from one of the marshals' phones over the venue network — this catches
      a wrong `ALLOWED_HOSTS`, a missing certificate and a firewall in one go.
- [ ] The event is selected as the current competition, classes and run order are
      set, participants have bibs.
- [ ] Timing → Settings: device selected, channels correct, CP540 **Connect** shows
      the live line log ticking.
- [ ] Fire one test start/finish through the Simulator or the real rig and see it
      land on the Manual timing view.
- [ ] Copy `db.sqlite3` somewhere else *now*, so there is a known-good starting point.

**Starting the server**

Windows: run `deploy\start-server.ps1`. Linux: `sudo systemctl start slalomtiming`.
Then start Caddy if it isn't already running as a service.

**During the event**

- The log is the first place to look: `journalctl -u slalomtiming -f`, or the
  Daphne window on Windows.
- `timing_unrecorded.log` in the project root should stay empty. Anything in it is a
  timing signal the database refused — the time is in that file, not lost, and needs
  entering by hand.
- Take a snapshot between runs:
  `uv run python -c "import sqlite3; sqlite3.connect('db.sqlite3').execute('VACUUM INTO ?', ('backup-YYYYMMDD-HHMM.sqlite3',))"`
  and copy it to a USB stick or a second machine. There is no automatic backup yet.

**After the event**

- Export the event from **Import / Export → Export** (a self-contained `.zip`) and
  keep it with the results PDFs.
- Stop the server; keep `db.sqlite3` until the results are final and published.

## 6. When something is wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every page is unstyled, no live updates | `collectstatic` hasn't run, or `STATIC_ROOT` is empty | `uv run python manage.py collectstatic --noinput`, restart |
| `Slalom Timing is already running against this directory` | A server process is still up (section 1, rule 1) | Stop it — check Task Manager / `systemctl status slalomtiming`. The lock is `run/server.lock`. |
| Results-PDF logo previews 404 | `DJANGO_SERVE_MEDIA=False` without the proxy serving `/media/` | Unset it, or add the `handle_path /media/*` block in the Caddyfile |
| `ImproperlyConfigured: DJANGO_SECRET_KEY is not set` | `.env` missing or not loaded into the process | Section 2; systemd needs `EnvironmentFile=`, PowerShell uses `start-server.ps1` |
| `ImproperlyConfigured: DJANGO_SECURE_COOKIES=False is no longer honoured` | An old `.env` from before the TLS decision | Section 3.4 — set up TLS, or set `DJANGO_ALLOW_PLAIN_HTTP=True` deliberately |
| Login says "Too many failed attempts" | The failed-attempt lockout (username + IP) | Wait it out, restart the server, or raise `DJANGO_LOGIN_MAX_ATTEMPTS` |
| A browser insists on HTTPS after you moved to plain HTTP | An HSTS pin from an earlier HTTPS run | Nothing server-side can revoke it; clear the site's HSTS entry in the browser and see section 3.4 |
| `DisallowedHost` in the log | The hostname isn't in `DJANGO_ALLOWED_HOSTS` | Add it (including the bare IP if people type that), restart |
| Browser refuses to submit a form, CSRF error | Origin missing from `DJANGO_CSRF_TRUSTED_ORIGINS` | Add `https://<host>`, restart |
| Live views stop updating for *some* browsers | Two server processes | See rule 1 — one process only |
| `database is locked` in the log | Heavy write contention on SQLite | Reduce open dashboards; WAL + a 30 s busy timeout are already configured |
| Setup says Slalom Timing is running | The launcher holds a mutex while the server is up | Close the black server window, then run Setup again |
| The installed app opens an empty event | Its database is `%LOCALAPPDATA%\SlalomTiming\data`, not the checkout's | Import the event's `.zip` (Import / Export), or copy `db.sqlite3` in with the app closed |
| Setup warns "unknown publisher" | The installer isn't code-signed | "More info" → "Run anyway"; see build/README.md |
