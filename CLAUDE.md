# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Local timekeeping/admin software for slalom races. Django 6 app (Python 3.14, managed with `uv`) that runs
locally in a browser and:

- manages race participants (CRUD + Django admin)
- ingests live events from a timing device through a pluggable connector interface, persists every event,
  and pushes it to a live dashboard over WebSockets (Django Channels)

The same Django app is the intended target for later network exposure to web clients (other people editing
participants / entering penalties) — no rewrite planned, just wider access + auth.

## Stack

- Python 3.14, Django 6.0, Django Channels 4 (ASGI, Daphne)
- uv for dependency + environment management (`pyproject.toml` / `uv.lock`)
- SQLite for now (`db.sqlite3`, gitignored)
- No JS framework — vanilla JS + WebSocket for the live dashboard (`static/js/dashboard.js`)

## Layout

```
config/                  Django project (settings, urls, asgi/wsgi)
apps/competitions/       Competition, CompetitionType, CompetitionClass; active-competition
                         selection. CompetitionClass is fully dynamic (editable name, not a
                         fixed enum): is_running, age range, practice_runs, counted_runs,
                         plus position (list order) and run_position (which run it starts in;
                         classes sharing a run_position start together). Competition.run_groups()
                         returns the ordered runs. Setup UI is a section: a tile list
                         ("Manage competitions") + General / Classes / Run order sub-pages that
                         all edit the *active* competition (no pk in the URL).
  assignment.py          Pluggable class-assignment strategies (Manual, Based-on-age) chosen per
                         competition via Competition.assignment_method; add a method in code only
                         (subclass AssignmentMethod + register). Competition.classes_for_participant()
                         resolves a participant's class(es). Toggles: Competition.allow_multiple_classes
                         (several distinct classes) + CompetitionClass.allow_multiple_entries (same
                         class more than once) — both Manual-only.
apps/participants/
  models.py              Participant (personal/contact data) + EventEntry (bib +
                         run status, unique per competition) + ClassAssignment (participant↔class
                         join for Manual assignment; explicit model, not a M2M, so duplicate
                         rows allow entering the same class multiple times)
  forms.py               add/edit forms (club autocomplete, email-domain completion)
  views.py               CRUD views + participant_check duplicate-detection endpoint
apps/timing/
  models.py              TimingEvent
  connectors/
    base.py              TimingDeviceConnector ABC + TimingPulse dataclass
    simulator.py          SimulatorConnector — fake device for dev, no hardware needed
    __init__.py            get_connector() factory reads settings.TIMING_CONNECTOR
  services.py             run_ingestion(): connect -> persist each pulse -> broadcast over Channels
  consumers.py            TimingConsumer (WebSocket, group "timing_updates")
  management/commands/run_timing_connector.py   entrypoint that runs the connector loop
templates/, static/      shared base template + per-app templates, plain CSS/JS
```

### Adding a real device connector

Implement `TimingDeviceConnector` (apps/timing/connectors/base.py) — `connect()`, `disconnect()`,
`pulses()` (async generator yielding `TimingPulse`). Point `TIMING_CONNECTOR` in `config/settings.py` at
the new class's dotted path. Nothing else changes — ingestion, persistence, and the dashboard are written
against the abstract interface only.

## Commands

```
uv sync                                  # install/sync dependencies
uv run python manage.py runserver        # dev server (Channels auto-serves ASGI/WebSockets)
uv run python manage.py run_timing_connector   # start the configured timing connector loop
uv run python manage.py makemigrations
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run pytest                            # tests (pytest-django)
```

Run `runserver` and `run_timing_connector` in separate terminals — the dashboard at `/` needs both to see
live data (the app itself works with just `runserver`, it just won't receive device events).

## Notes

- `CHANNEL_LAYERS` uses `InMemoryChannelLayer` — fine for a single local process. Switch to
  `channels_redis` only if this ever needs to run multi-process/multi-host.
- Every timing pulse is written to the DB (`TimingEvent`) before/as it's broadcast, so a dropped
  WebSocket or crashed dashboard never loses data.
- `TimingEvent.bib_number` is matched against the current competition's `EventEntry.bib_number` at
  ingestion time to resolve a display name (bibs live on `EventEntry`, scoped per competition, not on
  `Participant`); the participant FK is nullable since a pulse may arrive before a bib is registered.
