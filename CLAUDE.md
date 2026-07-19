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
apps/common.py           Helpers shared across apps: safe_next() resolves the POSTed ?next to
                         an in-app URL (rejecting off-site ones), so the unsaved-changes
                         modal's "Save changes" lands where the user was navigating.
apps/competitions/       Competition, CompetitionType, CompetitionClass; active-competition
                         selection. CompetitionType is the discipline *and* the rules its
                         competitions run under, edited on a per-type Settings page
                         (types/<pk>/settings/): penalties on/off plus four whole-second
                         amounts (mandatory only while penalties are on — the form clears
                         them when it's off), tie-break rule, timing-device precision
                         (format_time() renders a time at it), and which participant details
                         the discipline collects. Only the settings are stored so far; the
                         penalties screen and the tie-break/scoring calculations belong to
                         the timing and results features and aren't written yet.
                         CompetitionType.PARTICIPANT_INFO is the single source of truth for
                         the last of those: setting -> (label, mandatory, Participant fields),
                         which both the settings page and the participant form build from.
                         CompetitionClass is fully dynamic (editable name, not a
                         fixed enum): is_running, age range, practice_runs, counted_runs,
                         scoring_method (CompetitionClass.Scoring: aggregate times, best run
                         only, or regularity test — recorded per class, the calculation belongs
                         to the results feature and isn't written yet),
                         plus position (list order) and run_position (which run it starts in;
                         classes sharing a run_position start together). Competition.run_groups()
                         returns the ordered runs. Setup UI is a section: a tile list
                         ("Manage competitions") + General / Classes / Run order / Penalties
                         sub-pages that all edit the *active* competition (no pk in the URL).
                         MarshalPost (competition FK, 1-based number, tasks spec, one
                         handles_stop_line per competition, plus claim_token/claim_seen — a
                         heartbeated soft lock so only one device edits a post at a time)
                         records the marshal-post penalty setup: Competition.penalties_by_marshal_posts
                         turns it on, and each post watches a set of numbered tasks. taskspec.py parses/renders the
                         free-text task lists ("1, 5, 11-15") the Penalties page collects.
                         The Marshal Posts page (competitions:marshal-posts, a top-level sidebar
                         item below Timing) is the operator surface a marshal drives on a phone;
                         it reads the current competitor from the Auto timing view and pushes
                         penalties back over the timing WebSocket (see apps/timing/autotiming.py
                         and timing.MarshalPenalty).
  taskspec.py            parse()/format_ranges()/summary() for the marshal-post task-number
                         specs. Shared by the Penalties page and the Marshal Posts page.
  Competition            also carries auto_timing_order (a saved manual override of the Auto
                         timing start order; see apps/timing/autotiming.py).
  assignment.py          Pluggable class-assignment strategies (Manual, Based-on-age) chosen per
                         competition via Competition.assignment_method; add a method in code only
                         (subclass AssignmentMethod + register). Competition.classes_for_participant()
                         resolves a participant's class(es). Toggles: Competition.allow_multiple_classes
                         (several distinct classes) + CompetitionClass.allow_multiple_entries (same
                         class more than once) — both Manual-only.
  startpattern.py        The order participants take their runs *within* a run. One pattern per
                         competition (Competition.start_pattern, JSON), replayed for every run.
                         A pattern is a list of blocks; a block takes participants `window` at a
                         time (None = all) and plays its chips (practice/counted) in order, every
                         participant in the window taking a chip's run before the next chip —
                         then slides the window on until everyone's through. Chips name a run
                         *type*, not a number: each participant consumes their own runs in chip
                         order, so one pattern serves classes with different run counts (a
                         participant out of that type sits the chip out). Competition.start_lists()
                         is the resulting per-run start order; shortfalls() flags runs a class
                         grants that the pattern never plays. Edited on the Run order page below
                         the run grouping; the live preview re-implements the expansion in JS.
                         The preview can run on real starters or on made-up ones (one run of N,
                         bibs 1..N, taking the first run's first class's run counts) so a pattern
                         can be checked before anyone is registered. Its controls are unnamed and
                         data-no-dirty, so they neither post nor trip the unsaved-changes guard.
apps/participants/
  models.py              Participant (personal/contact data) + EventEntry (bib +
                         run status, unique per competition) + ClassAssignment (participant↔class
                         join for Manual assignment; explicit model, not a M2M, so duplicate
                         rows allow entering the same class multiple times).
                         Only name and date-of-birth are required at the DB level — licence,
                         co-driver, vehicle, address, club, e-mail and phone are all blank=True
                         because whether they're collected (and mandatory) is a per-discipline
                         decision (CompetitionType.PARTICIPANT_INFO, which now includes licence),
                         enforced by the form, not the model.
  forms.py               add/edit forms (club autocomplete, email-domain completion).
                         A participant is always registered under the *active competition's*
                         type — there is no type picker. The form resolves the type once (an
                         existing participant keeps its own; a new one takes the active
                         competition's) and renders only the groups that type collects; the
                         server blanks anything it doesn't. The required marker comes from
                         PARTICIPANT_INFO's static mandatory flag rather than field.required.
  views.py               CRUD views + participant_check duplicate-detection endpoint. The list
                         is scoped to the active competition's type and only shows the columns
                         that type collects (club, licence); with no competition selected it
                         prompts to pick one and shows nothing, and adding is blocked.
apps/timing/            The current timing path is TimingSignal -> arrangement -> TimedRun,
                        surfaced on the live Times view. The old TimingEvent + connector-loop
                        dashboard is legacy and slated to be redone.
  models.py              TimingSettings (singleton: device [Tag Heuer TP540 / Simulator],
                         single-digit start/finish channel, IP), TimingSignal (the raw device
                         inbox — running number, port, is_manual, device_time; stamped with the
                         active competition; `ignored`), TimedRun (one run: a start_signal
                         paired with a finish_signal, each OneToOne so a time is used once, plus
                         the operator's bib/class/run/penalty entry, plus pylon_adjust/task_adjust
                         — the Auto-timing timekeeper's signed +/- to the totals), MarshalPenalty
                         (one per run×marshal-post: aggregate counts, a per-task `detail` JSON, and
                         a `submitted` flag == locked, written from the Marshal Posts page and shown
                         on Auto timing), and legacy TimingEvent.
  autotiming.py          The Auto timing view's logic. The start order (Competition.start_lists()
                         = run order × start pattern) is a flat list of slots; a saved override
                         (Competition.auto_timing_order, a list of slot keys) reorders it. Times
                         still arrive as TimingSignal -> arrangement -> TimedRun, but bind to
                         slots *positionally* (n-th started run = n-th slot) so no bib is typed —
                         identity is the order. Each slot carries its run-group index/label (the
                         classes starting together) for the list dividers. current_run() is the last
                         to have started (the one marshals judge). serialize() builds the page: per
                         run the grand pylon/task/stop-line totals (marshal posts + timekeeper
                         adjust, clamped ≥0), the total time (run + penalty seconds), and per post a
                         box with counts, submitted/locked state and per-task detail for the pop-up.
                         marshal_state() returns the current competitor + the post's detail so the
                         marshal board resumes after an unlock.
  arrangement.py         Causal pairing of signals into runs: a finish joins the oldest open
                         start that began before it; a start never adopts an earlier orphan
                         finish. A start first fills the oldest empty *placeholder* row (one
                         pre-entered by the operator) before opening a new run. ignore keeps a
                         row that still carries a bib/run (placeholder) rather than deleting it.
                         ingest()/detach()/assign()/rows(); rows() is newest-first with
                         placeholders on top. effective_role() handles a single light barrier
                         (start_channel == finish_channel): the one channel alternates
                         start/finish/start/…
  calc.py                Run time (integer-microsecond truncation to the type's precision, never
                         rounded), total penalty, fixed-decimal formatting.
  forms.py               TimingSettingsForm (IP required only for the TP540).
  views.py               Settings page; standalone Simulator; live Times view + a JSON
                         arrangement endpoint and mutate endpoints (run-update by run id,
                         ignore, pair). timing_signal ingests device posts (csrf-exempt, since a
                         real device can't send a token) and nudges live views to refresh.
                         AutoTimingView + auto-state/reorder/reset-order endpoints; auto-adjust
                         (timekeeper +/- to a run's totals); marshal-state (the current competitor
                         for a post) + marshal-submit (a post's penalty + per-task detail, refused
                         once submitted/locked) + marshal-unlock / marshal-lock / marshal-lock-all
                         (timekeeper reopens or force-locks one or all posts) + marshal-task-edit
                         (timekeeper edits a locked post's per-task pylons/stop-line) tie the
                         Marshal Posts page to Auto timing. marshal-claim/release/claims enforce
                         one device per post. All share broadcast_live()/timing_live
                         so a device post, a marshal tap or an unlock nudges every open Auto timing
                         and Marshal Posts page to re-fetch.
  connectors/            (legacy) TimingDeviceConnector ABC + SimulatorConnector for the old
                         connector-loop dashboard; get_connector() reads settings.TIMING_CONNECTOR.
  services.py            run_ingestion() [legacy] + Channels group names (timing_updates,
                         timing_live).
  consumers.py           TimingConsumer (legacy dashboard) + TimingLiveConsumer (pushes refresh
                         nudges to open Times views, group "timing_live").
  management/commands/run_timing_connector.py   [legacy] runs the connector loop
templates/timing/        settings.html, simulator.html (standalone, no app shell), live.html,
                         auto.html (Auto timing)
static/js/               dashboard.js (legacy) + timing_live.js (Times view: render, edits,
                         double-click-to-ignore, drag-to-pair, WebSocket refresh) + auto_timing.js
                         (Auto timing: draggable start order, prev/current/next tiles, marshal
                         boxes, ignore/re-pair, WS refresh). marshal_posts.js pushes taps/submit
                         to timing:marshal-submit and pulls the current competitor via
                         timing:marshal-state, single-flight so rapid taps can't land out of order.
```

### Timing UI (under the sidebar "Timing" menu)

- **Settings** (`timing/settings/`) — pick the device and channels. "Start" opens the Simulator
  in a new tab (Simulator) or connects to the device (TP540; the driver is a stub for now).
- **Simulator** (`timing/simulator/`) — a standalone new-tab device emulator: a running clock, an
  auto-incrementing running number (with reset), a 2×4 pad (ports 1–4 light barrier, M1–M4 manual
  → same port, is_manual), and an on-page log. Each press POSTs a signal to `timing:signal`.
- **Times** (`timing/times/`) — the operator's live view for the active competition: start/finish
  times (shown at the type's precision, truncated) paired into runs (newest first), with bib, class,
  run, and penalty entry (−/+ steppers, out of the tab order), live over a WebSocket. Entering a bib
  looks up the name, sets the class (updating it if the bib changes, clearing it if the bib is cleared),
  and auto-selects the next not-yet-done run (P then C); an unknown bib is flagged but kept. Tab out of
  the bib field lands on the Class dropdown for a multi-class participant. The dropdown has one slot per
  class the participant is entered into — a class entered more than once shows as "Klasse 2 (1)/(2)"
  (TimedRun.class_occurrence tracks which), each with its own runs; a run already recorded, or a class
  whose runs are all done, is disabled, and the default class advances to the first slot not yet
  completed. Manual times are tinted vs light-barrier ones.
  Hover between the header and the top row for a **+** to pre-enter an upcoming starter (an empty
  placeholder row); incoming starts fill placeholders oldest-first, so times populate bottom-to-top.
  Double-click a time to ignore it — ignored starts and finishes each get a rail column on the right,
  floated beside where they fall; drag one back onto a run's slot to use it (rejected with a wiggle if
  it would put a start after its finish). Column widths are fixed so entering a bib never shifts them.
- **Auto timing** (`timing/auto/`) — the order-driven live view. The start order (run order × start
  pattern) runs down the left as draggable tiles ("#3 C1"), grouped by run with a "Run · <classes>"
  divider that sticks to the top of the list and is replaced by the next run's as it scrolls up (so the
  header names the run shown at the top, not the running one). Each tile shows the total time (run +
  penalties), and dragging saves a persisted override (Reset order re-derives it). The list follows the
  current starter automatically until the operator scrolls away; a "▲/▼ current" cue re-engages it.
  Incoming times bind to the order positionally — no bib typing — and the right shows the previous /
  current / next competitor with start, finish, run time and **total time**. The current (last to start)
  stays centred until the next one starts, then the tiles shift up. Beside the times, one box per marshal
  post shows its pylon/task/stop-line counts ("2 P · 1 T · SL"), turning green with a 🔒 once submitted;
  a 🔒 button to their left locks every post at once, and the timekeeper can +/- the run's total
  pylon/task counts. Clicking a box opens a speech-bubble pop-up under it: a locked post shows +/-
  steppers to edit each task's pylons (and toggle the stop line) plus an **Unlock** button; an unlocked
  post shows the read-only breakdown and a **Lock** button. Double-click a time to ignore it (listed on
  the right), drag it back onto a slot to re-pair.
- **Marshal Posts** is a top-level sidebar item (below Timing) — the marshal's phone surface (see the
  competitions app). It reads the current competitor from Auto timing over the timing WebSocket and
  pushes every tap and the final submit back (with the per-task detail) so the boxes above fill and go
  green. Once submitted the board shows a 🔒 and locks; a timekeeper Unlock reopens it, and the board
  resumes its exact per-task state for editing. Confirming a post claims it for that device
  (heartbeated); the post shows "— in use" and can't be claimed on another device until released or the
  claim goes stale.

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
- The three `TimingEvent` bullets below describe the **legacy** dashboard/connector-loop path,
  kept working but slated to be redone; the current operator surface is the live Times view built
  on `TimingSignal` -> `TimedRun`.
- Every timing pulse is written to the DB (`TimingEvent`) before/as it's broadcast, so a dropped
  WebSocket or crashed dashboard never loses data.
- `TimingEvent.bib_number` is matched against the current competition's `EventEntry.bib_number` at
  ingestion time to resolve a display name (bibs live on `EventEntry`, scoped per competition, not on
  `Participant`); the participant FK is nullable since a pulse may arrive before a bib is registered.
