# Slalom Timing

Local timekeeping and admin software for slalom races. A Django 6 app (Python 3.14,
managed with [uv](https://docs.astral.sh/uv/)) that runs locally in the browser and:

- manages race **competitions**, their **types** (disciplines), their **classes**
  (age ranges, per-class practice/counted run counts, scoring method) and the **run order**
  (which classes start together and in what order, plus the **start pattern** — the order
  participants take their runs within a run)
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

Competition Setup is a section with a landing page plus four sub-pages (General, Classes,
Run order, Penalties) in the sidebar. The sub-pages always act on the **current** competition:

1. **Competition Setup → Manage competition types** — add a discipline (e.g. Motorcycle,
   Go-Cart). Types can be expanded to list their competitions, and deleted while unused.
   **Settings** opens the rules every competition of that type is run under:
   - **Penalties** — whether penalties are entered during the race, and the whole-second
     amounts for a pylon, a task, the stop line, and the most a single task can add. The
     amounts are required while penalties are on, and cleared if you switch them off. The
     penalties screen that will read them is not built yet.
   - **Evaluation** — the **tie break** rule (*Fastest run time* or *Manual*) and the
     **timing precision** the device resolves to (1/10, 1/100 or 1/1000 s). Recorded here;
     the results calculation that reads them isn't built yet.
   - **Required participant info** — which details the participant form asks for in this
     discipline: co-driver, vehicle, address, club, e-mail, phone. A `*` marks the ones that
     are mandatory once collected. Name, date of birth and licence number are always asked for.
2. **Competition Setup → New competition** — pick a type, name and date. The new
   competition becomes the current one so you can configure it right away.
3. **Manage competitions** — the tile list of all competitions by date. **Set as current**
   picks the one the sub-pages edit; this also drives which participants are "active", the
   class computed on the participant form, and bib matching for timing events.
4. **General** — the current competition's name, type and date.
5. **Classes** — pick the **assignment method** (how participants get their class) at the top,
   then edit each class as a tile: rename, Running, practice / counted runs, scoring, delete.
   **Scoring** picks how the class's counted runs become a result — *Aggregate times*,
   *Best run only*, or *Regularity test*. It is recorded per class; the results feature that
   reads it isn't built yet.
   - **Manual**: you assign participants to classes by hand on the participant form. A top switch
     allows a participant in several *different* classes; a per-class switch allows entering the
     same class more than once.
   - **Based on age**: each class gets an age range (birth years update live) and the class is
     computed from the participant's date of birth.
6. **Run order** — drag classes into runs to start them together, drop a class in the gap
   between runs to split it out, and drag a run's handle to reorder. Only classes marked
   *Running* on the Classes page appear here.
   Below that, the **start pattern** sets the order participants take their runs *inside* a
   run. It is a list of blocks; a block takes participants N at a time (or *all* at once) and
   plays its runs — dragged in from a Practice / Counted palette — so every participant in
   that group takes the first run, then the second, and so on, before the block slides on to
   the next group and repeats until everyone has been through it. For example "2 at a time,
   Practice + Counted" then "all, Counted" gives
   `1P 2P 1C 2C | 3P 4P 3C 4C | …` followed by everyone's second counted run.
   Chips name a run *type*, not a number: each participant uses up their own runs in the
   order the blocks come, so one pattern serves classes with different run counts, and a
   participant with none of that run left simply sits it out. One pattern is stored per
   competition and replayed for every run. A live preview expands it per run and flags any
   runs a class grants that the pattern never plays; toggle **Dummy participants** (on by
   default) to check a pattern against a made-up field before anyone is registered.
7. **Penalties** — how penalties are entered for the current competition. **Penalties set by
   marshal posts** off leaves the timekeeper entering every penalty on the Times view; on, each
   marshal post enters the penalties for its own area. When on, set the **number of posts**, then
   for each post type the **tasks it watches** as a free-text list (`1, 5, 9, 11-15, 20`) and tick
   which single post is **responsible for the stop line** (ticking one locks it out on the others).
   A confirmation under General combines every post's tasks into e.g. *Tasks 1-35 assigned*.
8. **Marshal Posts** (top-level) — the operator surface a marshal drives on their phone. Pick your
   post from the dropdown and press **Confirm** (it locks in as a red **Change post** button so it
   isn't nudged by accident, and claims the post so no other device can pick it — taken posts show
   *— in use*). Below, each task the post watches is a big touch button: tap to add a
   pylon hit, long-press for a task penalty (the per-task ceiling from the competition type caps the
   pylons — going over wiggles the button). The stop line, if this post owns it, is its own button.
   **Submit** finalises the current bib and **locks** the board (a 🔒 shows); there's no way back until a
   timekeeper unlocks it from Auto timing. The current competitor and each post's penalty are exchanged
   live with **Auto timing** (below).
9. **Timing → Auto timing** — the order-driven live view. The start order (run order × start pattern)
   runs down the left as draggable tiles, grouped by run with a header (e.g. *Run · Klasse 5, Klasse 6*
   for combined classes) that sticks to the top and is replaced by the next run's as you scroll; each tile
   carries its total time (run + penalties). The list follows the current starter automatically until you
   scroll away, and a *▲/▼ current* cue brings it back. Incoming start/finish times attach to the order
   automatically — no bib typing — and the right shows the previous / current / next competitor with their
   start, finish, run time and **total time**. The current competitor stays centred until the next one
   starts, then the tiles shift up. Beside the times, one box per marshal post shows its pylon/task/stop-line
   counts (*2 P · 1 T · SL*), turning green with a 🔒 once submitted — so the timekeeper sees all-green when
   a competitor is fully judged. A 🔒 button locks every post at once; clicking a box opens a pop-up under
   it — a locked post gets **+/-** steppers to correct each task's pylons (and toggle the stop line) plus
   **Unlock**, an unlocked post shows the read-only breakdown and a **Lock** button. The timekeeper can
   also **+/-** the total pylon and task counts. Double-click a time to ignore it (listed on the right),
   drag it back onto a slot to re-pair.
10. **Participants → Add participant** — register a competitor and optionally assign a bib
   for the current competition right away. The form asks only for the details the selected
   **competition type** collects (see step 1) — pick a different type and the fields follow
   immediately. The **Class** line follows the competition's assignment method: Manual shows a
   class picker (one or several, with repeats where allowed), Age based shows the class derived
   live from the date of birth. The form autocompletes known clubs and common email domains and
   warns about likely duplicates (name or licence) before saving.
11. **Dashboard** — watch live timing events resolve to participant names by bib.

Leaving General, Classes, Run order, Penalties or the participant form with unsaved edits pops a styled
confirmation (Save / Discard / Cancel) rather than losing the changes. **Save changes** carries
on to wherever you were heading; view-only controls (such as the start pattern's preview
switches) don't count as changes.

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
apps/competitions/       Competition, CompetitionType (discipline + its rules, edited on a
                         per-type Settings page), CompetitionClass, MarshalPost + setup UI
                         (tile list + General / Classes / Run order / Penalties sub-pages) and
                         the top-level Marshal Posts operator page
apps/participants/       Participant + EventEntry models, CRUD views, admin. The form is
                         built from the selected type's "required participant info".
apps/timing/             TimingSignal -> arrangement -> TimedRun timing path, the live Times
                         and Auto timing views, MarshalPenalty, WebSocket consumers (plus the
                         legacy TimingEvent connector/dashboard path)
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