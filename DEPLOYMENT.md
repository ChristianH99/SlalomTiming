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
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_COOKIES` | no | Escape hatches for running on plain HTTP. See the TLS section — the honest answer is to set up TLS instead. |
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

### The reverse proxy

`deploy/Caddyfile` puts Caddy in front of Daphne, terminating TLS with its own local
CA (no internet, no public domain needed) and passing WebSockets through untouched:

```bash
caddy run --config deploy/Caddyfile
```

Point `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` at the hostname you
serve, and install Caddy's root certificate on every device that opens the app
(`caddy trust` on the server; phones import it manually) — otherwise every browser
shows a warning.

Serving the app on plain HTTP means every password and session cookie on the venue
Wi-Fi is readable by anyone else on it, including devices you don't control.
`DJANGO_SECURE_COOKIES=False` exists to make that possible, not to make it safe;
[Tailscale](https://tailscale.com/) is the other reasonable answer if the operators'
devices can all join one tailnet.

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
| `DisallowedHost` in the log | The hostname isn't in `DJANGO_ALLOWED_HOSTS` | Add it (including the bare IP if people type that), restart |
| Browser refuses to submit a form, CSRF error | Origin missing from `DJANGO_CSRF_TRUSTED_ORIGINS` | Add `https://<host>`, restart |
| Live views stop updating for *some* browsers | Two server processes | See rule 1 — one process only |
| `database is locked` in the log | Heavy write contention on SQLite | Reduce open dashboards; WAL + a 30 s busy timeout are already configured |
