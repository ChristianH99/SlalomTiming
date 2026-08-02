# Slalom Timing

Local timekeeping and admin software for slalom races. A Django 6 app (Python 3.14,
managed with [uv](https://docs.astral.sh/uv/)) that runs locally in the browser and:

- manages race **competitions**, their **types** (disciplines), their **classes**
  (age ranges, per-class practice/counted run counts, scoring method) and the **run order**
  (which classes start together and in what order, plus the **start pattern** — the order
  participants take their runs within a run)
- manages race **participants** (CRUD + Django admin) and their bib/entry per competition
- ingests **live timing events** from a timing device through a pluggable connector
  interface, persists every event, and pushes it to live timing views over WebSockets
  (Django Channels) — a manual table and an order-driven view that share the same runs
- scores each class into ranked **results** by its scoring method (aggregate / best run /
  regularity), with penalties folded in and ties flagged for inspection

The same app is the intended target for later network exposure to additional clients
(other people editing participants / entering penalties) — no rewrite planned, just
wider access plus authentication.

## Stack

- Python 3.14, Django 6.0, Django Channels 4 (ASGI, served by Daphne)
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- SQLite (`db.sqlite3`, gitignored) for local use
- No JS framework — vanilla JS + WebSocket for the live views (Manual timing, Auto timing,
  Marshal Posts and the Dashboard)

## Getting started

On Windows, `start.bat` (or `start.ps1`) does all of the below in one double-click,
installing uv and Python 3.14 first if the machine has neither. By hand:

```bash
uv sync                                   # install/sync dependencies
uv run python manage.py migrate           # create the local database
uv run python manage.py createsuperuser   # the first login (every page needs one)
uv run python manage.py collectstatic     # required for the test suite too — see below
uv run python manage.py runserver         # dev server (auto-serves ASGI/WebSockets)
```

Open http://127.0.0.1:8000/ for the **Dashboard**, the organiser's live overview of the
event. Admin is at `/admin/`.

`runserver` alone drives the whole app, timing included — there is no second process to
start. To see times arrive without any hardware, set the device to **Simulator** on
*Timing → Settings* and press **Start simulator**: it opens a standalone device emulator
in a new tab whose pad posts start/finish signals into the app, which land on *Manual
timing* and *Auto timing*. A real Tag Heuer CP540 is the same picture with the device set
to CP540 and its IP filled in; its reader thread is started from that page.

(`manage.py run_timing_connector` belongs to the **legacy** `TimingEvent` connector-loop
path, which is kept working but is not the timing path described above. Nothing on the
current screens needs it.)

## Typical workflow

Competition Setup is a section with a landing page plus five sub-pages (General, Classes,
Run order, Penalties, Results) in the sidebar. The sub-pages always act on the **current** competition:

1. **Competition Setup → Manage competition types** — add a discipline (e.g. Motorcycle,
   Go-Cart). Types can be expanded to list their competitions, and deleted while unused.
   **Settings** opens the rules every competition of that type is run under:
   - **Penalties** — whether penalties are entered during the race, and the whole-second
     amounts for a pylon, a task, the stop line, and the most a single task can add. The
     amounts are required while penalties are on, and cleared if you switch them off. The
     timing views and the results ranking read these amounts.
   - **Evaluation** — the **tie break** rule (*Fastest run time* or *Manual*) and the
     **timing precision** the device resolves to (1/10, 1/100 or 1/1000 s). The tie break
     separates equal results in the class tables; the precision is how every time is shown
     and truncated.
   - **Required participant info** — which details the participant form asks for in this
     discipline: licence number, co-driver, vehicle, address, club, e-mail, phone. A `*`
     marks the ones that are mandatory once collected. Only **name and date of birth** are
     always asked for; everything else, licence included, is this per-type choice.
2. **Competition Setup → New competition** — pick a type, name and date. The new
   competition becomes the current one so you can configure it right away.
3. **Manage competitions** — the tile list of all competitions by date. **Set as current**
   picks the one the sub-pages edit; this also drives which participants are "active", the
   class computed on the participant form, and bib matching for timing events.
   - **Archive** signs a finished event off. A competition normally reads its rules from its
     *type*, and its competitors from the participant records — both of which are shared and
     get edited for years afterwards, so a penalty amount changed next winter, or a club
     corrected, used to rewrite a result that had already been printed. Archiving copies both
     onto the event: from then on it is evaluated by the settings it was run under and shows
     the competitors it ran with, whatever happens to those records later. It also becomes
     **read-only** — every control on its screens is disabled, it records no further times,
     and its results and PDFs still open exactly as before. **This cannot be undone.**
   - **Archived settings** on an archived tile shows what was frozen, read-only.
   - **Duplicate** on an archived tile asks what the copy should carry: the *setup only*
     (classes, run order, marshal posts — for building next year's event from this one), or
     *everything*, including the competitors, their bibs and every recorded time. The second
     is how a signed-off event gets corrected: the copy is live and editable, the original
     stays as it was. If the type's settings have moved on since, the dialog lists what
     disagrees and you choose which value the copy runs under — and says how many other
     competitions that choice moves, since it is saved to the shared type.
     A full copy then resolves its competitors the way an **import** does: anyone still on
     file exactly as they raced is reused, anyone no longer on file (deleted, or never here)
     is re-created, and anyone whose record has changed since opens a merge window where you
     pick, field by field, whether the copy uses what is on file now or what they raced
     under. Copying an old event is not somebody racing again, so it does not count as a use
     of their record.
   - While an archived event is the current one, **Participants** lists the competitors it
     was archived with — the people who had a bib on the day, as their records read then —
     rather than everyone registered under the discipline. Editing or deleting somebody
     afterwards cannot change it, and there is nothing on the page to edit with.
4. **General** — the current competition's name, type and date.
5. **Classes** — pick the **assignment method** (how participants get their class) at the top,
   then edit each class as a tile: rename, Running, practice / counted runs, scoring, delete.
   **Scoring** picks how the class's counted runs become a result — *Aggregate times* (sum of
   every counted run), *Best run only* (the single fastest), or *Regularity test* (the smallest
   spread between runs). Each class's Results page ranks its competitors by this method (step 12).
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
   marshal posts** off leaves the timekeeper entering every penalty on the Manual timing view; on, each
   marshal post enters the penalties for its own area. When on, set the **number of posts**, then
   for each post type the **tasks it watches** as a free-text list (`1, 5, 9, 11-15, 20`) and tick
   which single post is **responsible for the stop line** (ticking one locks it out on the others).
   A confirmation under General combines every post's tasks into e.g. *Tasks 1-35 assigned*.
8. **Results** — which participant details (club, address, licence, …) the class result tables show:
   a **General** default that every class shows, plus optional **per-class additions**. A class can
   only *add* columns General doesn't already show — there is no inheriting-and-replacing and no
   override. Only the details this competition's type collects are offered; rank, bib and name are
   always shown. The same page holds the results-PDF layout (header/footer, logos, orientation).
9. **Marshal Posts** (top-level) — the operator surface a marshal drives on their phone. Pick your
   post from the dropdown and press **Confirm** (it locks in as a red **Change post** button so it
   isn't nudged by accident, and claims the post so no other device can pick it — taken posts show
   *— in use*). Below, each task the post watches is a big touch button: tap to add a
   pylon hit, long-press for a task penalty (the per-task ceiling from the competition type caps the
   pylons — going over wiggles the button). The stop line, if this post owns it, is its own button.
   **Submit** finalises the current bib and **locks** the board (a 🔒 shows); there's no way back until a
   timekeeper unlocks it from Auto timing. The current competitor and each post's penalty are exchanged
   live with **Auto timing** (below).
10. **Timing → Manual timing** — the operator's live table for the current competition: start/finish times
   paired into runs (newest first), with bib, class, run and penalty entry, live over a WebSocket. Entering
   a bib fills the name, sets the class and picks the next run; a multi-class participant gets a class
   dropdown. Hover the gap under the header for a **+** to pre-enter an upcoming starter. When the device
   misses a time, **double-click** a Start, Finish or Run time (or an empty slot) to type it in by hand —
   keyed-in times are highlighted green, and the run's total honours them. Drag a wrong time to a rail on
   the right to ignore it (drag it back, or double-click the rail chip, to reuse it).
11. **Timing → Auto timing** — the order-driven live view. The start order (run order × start pattern)
   runs down the left as draggable tiles, grouped by run with a header (e.g. *Run · Klasse 5, Klasse 6*
   for combined classes) that sticks to the top and is replaced by the next run's as you scroll; each tile
   carries its total time (run + penalties). The list follows the current starter automatically until you
   scroll away, and a *▲/▼ current* cue brings it back. Incoming times attach to the order automatically —
   no bib typing — while a run you pre-entered on Manual timing shows pre-filled in its place and new times
   step over it. The right shows the previous / current / next competitor with their start, finish, run time
   and **total time**; the **current** tile follows the latest timing activity (a fresh finish for an earlier
   starter surfaces it, not just the last to start). You can key a time in here by hand too — double-click a
   Start, Finish or Run time; do it on an upcoming competitor (one who hasn't started) and keying their
   start makes them the current one. **Scroll** the tiles with the mouse wheel (one competitor per notch)
   or **click** a start-order tile on the left to browse the field without changing who is current — the
   centred tile frames blue and reads *Next up* / *Previous* while you browse. You can also **drag** a
   time chip from the current competitor onto another tile's Start/Finish slot — including an upcoming
   competitor who has no run yet — to move a mis-attributed time onto the right starter. Beside the times, one box per marshal post shows its pylon/task/stop-line
   counts (*2 P · 1 T · SL*), turning green with a 🔒 once submitted. A 🔒 button locks every post at once;
   clicking a box opens a pop-up — a locked post gets **+/-** steppers to correct each task's pylons (and
   toggle the stop line) plus **Unlock**, an unlocked post shows the read-only breakdown and a **Lock**
   button. The timekeeper can also **+/-** the run's Pylons, Task and Stop line counts. Drag a wrong time to
   the Ignored list to ignore it, and back onto a slot to re-pair. Manual timing and Auto timing share the
   same runs: a bib, time or penalty entered on either — including a marshal-post penalty on a run you
   selected manually — shows up and adds into the total on both.
12. **Results** (top-level) — one sub-page per running class, each a table ranked by the class's scoring
   method (aggregate / best run / regularity; lower is better, penalties folded into every run's time). A
   participant entered into a class more than once keeps only their best result ranked; equal scores the
   type's tie break can't separate share a rank and are flagged **⚑ inspect** (click the flag to order
   them by hand). Competitors whose event is settled without a placing — DNS, DNC, DSQ — sit at the foot
   of the ranked table with that code instead of a score; ones still waiting on a run (best run needs
   just one) are in a separate block below, where a **DNS** button closes a run nobody started. Above the
   per-class pages the sidebar lists an **Overall** page per scoring method × counted-run count, ranked
   across the classes that share them. Every table exports to A4 PDF, one class, one Overall group or
   everything in one file. The columns follow the Competition-Setup → Results settings (step 8).
13. **Participants → Add participant** — register a competitor and optionally assign a bib
   for the current competition right away. The form asks only for the details the selected
   **competition type** collects (see step 1) — pick a different type and the fields follow
   immediately. The **Class** line follows the competition's assignment method: Manual shows a
   class picker (one or several, with repeats where allowed), Age based shows the class derived
   live from the date of birth. The form autocompletes known clubs and common email domains and
   warns about likely duplicates (name or licence) before saving.
14. **Dashboard** (at `/`) — the organiser's read-only overview of the event as it runs: overall
   progress against the start order, a per-class board (done / running / not started with a
   completion percentage), the competitor on course right now with their times and penalties, and
   headline counts (participants, classes finished, runs remaining, non-starters, marshal posts).
   It reads the same runs the timing views do, so its numbers cannot disagree with theirs.

Leaving General, Classes, Run order, Penalties, Results settings or the participant form with unsaved edits pops a styled
confirmation (Save / Discard / Cancel) rather than losing the changes. **Save changes** carries
on to wherever you were heading; view-only controls (such as the start pattern's preview
switches) don't count as changes.

## Common commands

```bash
uv run python manage.py runserver              # dev server — the whole app, timing included
uv run python manage.py run_timing_connector   # legacy connector loop only (see Getting started)
uv run python manage.py makemigrations
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py collectstatic          # required before any DEBUG=False run *and* before pytest
uv run pytest                                  # test suite (pytest-django)
```

`collectstatic` is a prerequisite of the **test suite**, not only of a deployment: static
files are served through WhiteNoise's manifest storage in every mode, so `{% static %}`
resolves through the gitignored `staticfiles/staticfiles.json`. A checkout that has never
run it fails most of the suite with "Missing staticfiles manifest entry", because every
page render 500s. Run it once after cloning, and again after adding a static file.

## Running a real event

`runserver` is a development server. For an actual event — one machine at the venue, marshals'
phones and a dashboard on the network — follow **[DEPLOYMENT.md](DEPLOYMENT.md)**: it covers the
configuration, the release steps, the race-morning checklist and what to do when something is
wrong. The ready-made pieces are in `deploy/`:

```bash
powershell -ExecutionPolicy Bypass -File deploy\start-server.ps1   # Windows laptop
sudo systemctl enable --now slalomtiming                           # Linux (deploy/slalomtiming.service)
caddy run --config deploy/Caddyfile                                # TLS in front of it
```

All three run exactly **one** Daphne process. That is a hard requirement, not a preference: the
live-update channel layer, the CP540 reader thread and its event loop live in the process, so a
second worker silently splits the event in half. A second start is refused (`run/server.lock`).

### A Windows machine with nothing installed on it

Two ways, depending on whether that machine should carry a checkout:

```powershell
start.bat                                                  # a checkout: installs uv, Python 3.14
                                                           # and the dependencies, then serves
powershell -ExecutionPolicy Bypass -File build\build.ps1   # no checkout: build an installer
```

`build.ps1` produces `dist\SlalomTiming-Setup-<version>.exe` — one ~28 MB file carrying its own
Python and every dependency. It installs without an administrator account and leaves a desktop
icon; the operator never sees a terminal, because the first start generates the secret key, creates
the database and asks for the first login itself. Its event data lives in
`%LOCALAPPDATA%\SlalomTiming\data`, outside the program folder, so an upgrade or an uninstall
cannot take an event with it. See **[build/README.md](build/README.md)**.

That build also runs on GitHub (`.github/workflows/windows-installer.yml`): every push is
built and kept on its run page, and **publishing a release** builds it at that release's
version and attaches it as an asset:
`https://github.com/<owner>/<repo>/releases/latest/download/SlalomTiming-Setup-<version>.exe`
— anonymous only while the repository is public; on a private one that download needs to
be authenticated (`gh release download`).

## Adding a real device

The timing path the current screens are built on is `TimingSignal` → arrangement →
`TimedRun`, and it has one real driver: the Tag Heuer CP540 (`apps/timing/cp540.py`), a
reader thread that parses the device's lines and hands each signal to
`ingest.record_signal(running_number, port, is_manual, device_time)`. A new device is that
same shape — read its stream, parse it, call `record_signal()` — and nothing downstream
changes: persistence, the run arrangement, both timing views and the results all read what
that one door writes.

The `TimingDeviceConnector` ABC (`apps/timing/connectors/base.py`, selected by
`TIMING_CONNECTOR` in `config/settings.py`) belongs to the **legacy** `TimingEvent`
connector-loop path and is not what a new device should implement.

## Project layout

```
config/                  Django project (settings, urls, asgi/wsgi)
apps/competitions/       Competition, CompetitionType (discipline + its rules, edited on a
                         per-type Settings page), CompetitionClass, MarshalPost + setup UI
                         (tile list + General / Classes / Run order / Penalties sub-pages) and
                         the top-level Marshal Posts operator page
apps/participants/       Participant + EventEntry models, CRUD views, admin. The form is
                         built from the selected type's "required participant info".
apps/timing/             TimingSignal -> arrangement -> TimedRun timing path, the live Manual
                         timing and Auto timing views (which share runs and sync bidirectionally),
                         MarshalPenalty, WebSocket consumers (plus the legacy TimingEvent
                         connector/dashboard path)
apps/results/            Ranked per-class and Overall results (top-level "Results" section) computed
                         from the runs by each class's scoring method, their A4 PDF export, plus a
                         Competition-Setup sub-page choosing the columns and the PDF layout
apps/accounts/           Login, and page-level roles (a role is a Django group listing the pages it
                         may open); the sidebar shows only what the caller holds
apps/transfer/           The "Backup" section: the automatic database backup, .zip export/import of
                         an event or a competition type, and the participant-CSV import
templates/, static/      shared base template + per-app templates, plain CSS/JS, self-hosted fonts
deploy/, build/          systemd unit, Windows start script and Caddyfile for a real deployment;
                         build/ packages the same app as a one-click Windows installer
```

## Notes

- `CHANNEL_LAYERS` uses `InMemoryChannelLayer` — fine for a single local process, which is
  why only one server process may run (`config/singleinstance.py` enforces it). Switch
  to `channels_redis` only if this ever needs to run multi-process/multi-host.
- Every incoming time is written to the database before anything is broadcast, and a
  briefly locked database is waited out and retried; a time that still cannot be stored is
  appended to `timing_unrecorded.log` rather than dropped (`apps/timing/ingest.py`). A
  dropped WebSocket or a closed tab therefore never loses timing data — a reopened view
  reads it all back.
- `TimingEvent` and its connector loop are the **legacy** feed, kept working and slated to
  be redone. Its bib-to-name matching at ingestion time is not how the current path works:
  bibs live on `EventEntry` (per competition) and a run gets its competitor from the
  operator typing a bib on Manual timing or from the start-order binding on Auto timing.
- Out of the box this is a development configuration (`DEBUG = True`, checked-in dev
  `SECRET_KEY`). Every deployment setting comes from the environment — see `.env.example`
  and [DEPLOYMENT.md](DEPLOYMENT.md). With `DEBUG=False` the app refuses to start on the
  checked-in key, serves static files through WhiteNoise (so `collectstatic` must have run)
  and self-hosts its fonts, so it needs no internet at a venue.