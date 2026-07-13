# Slalom Timing

Local timekeeping and admin software for slalom races. A Django 6 app (Python 3.14,
managed with [uv](https://docs.astral.sh/uv/)) that runs locally in the browser and:

- manages race **competitions**, their **types** (disciplines) and per-class age ranges
- manages race **participants** (CRUD + Django admin) and their bib/entry per competition
- ingests **live timing events** from a timing device through a pluggable connector
  interface, persists every event, and pushes it to a live dashboard over WebSockets
  (Django Channels)

The same app is the intended target for later network exposure to additional clients
(other people editing participants / entering penalties) — no rewrite planned, just
wider access plus authentication.

## Stack

- Python 3.14, Django 6.0, Django Channels 4 (ASGI, served by Daphne)
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- SQLite (`db.sqlite3`, gitignored) for local use
- No JS framework — vanilla JS + WebSocket for the live dashboard

## Getting started

```bash
uv sync                                   # install/sync dependencies
uv run python manage.py migrate           # create the local database
uv run python manage.py createsuperuser   # optional: for the Django admin
uv run python manage.py runserver         # dev server (auto-serves ASGI/WebSockets)
```

Open http://127.0.0.1:8000/ for the live dashboard. Admin is at `/admin/`.

To see live timing data on the dashboard, run the timing connector loop in a **second**
terminal:

```bash
uv run python manage.py run_timing_connector
```

Out of the box this uses `SimulatorConnector`, which emits random start/finish pulses so
the pipeline and dashboard work without any hardware. The app itself runs fine with just
`runserver` — it simply won't receive device events until the connector is running.

## Typical workflow

1. **Competition Setup → Manage competition types** — add a discipline (e.g. Motorcycle,
   Go-Cart).
2. **Competition Setup → New competition** — pick a type, name and date, then set which
   classes run and their age ranges. Birth-year ranges update live as you type.
3. **Set as current** on a competition — this drives which participants are "active",
   the class computed on the participant form, and bib matching for timing events.
4. **Participants → Add participant** — register a competitor and optionally assign a bib
   for the current competition right away.
5. **Dashboard** — watch live timing events resolve to participant names by bib.

## Common commands

```bash
uv run python manage.py runserver              # dev server
uv run python manage.py run_timing_connector   # timing connector loop (see above)
uv run python manage.py makemigrations
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run pytest                                  # test suite (pytest-django)
```

## Adding a real device connector

Implement `TimingDeviceConnector` (`apps/timing/connectors/base.py`) — `connect()`,
`disconnect()`, and an async `pulses()` generator yielding `TimingPulse` objects. Point
`TIMING_CONNECTOR` in `config/settings.py` at the new class's dotted path. Nothing else
changes: ingestion, persistence and the dashboard are written against the abstract
interface only.

## Project layout

```
config/                  Django project (settings, urls, asgi/wsgi)
apps/competitions/       Competition, CompetitionType, CompetitionClass + setup UI
apps/participants/       Participant + EventEntry models, CRUD views, admin
apps/timing/             TimingEvent, connectors, ingestion service, WebSocket consumer
templates/, static/      shared base template + per-app templates, plain CSS/JS
```

## Notes

- `CHANNEL_LAYERS` uses `InMemoryChannelLayer` — fine for a single local process. Switch
  to `channels_redis` only if this ever needs to run multi-process/multi-host.
- Every timing pulse is written to the database (`TimingEvent`) before it is broadcast,
  so a dropped WebSocket or crashed dashboard never loses timing data.
- `TimingEvent` bib numbers are matched against the current competition's `EventEntry`
  bibs at ingestion time to resolve a display name; the participant link is nullable
  since a pulse may arrive before a bib is registered.
- This is a development configuration (`DEBUG = True`, checked-in dev `SECRET_KEY`). Set
  a real secret key, `DEBUG = False` and `ALLOWED_HOSTS` before any network exposure.
```