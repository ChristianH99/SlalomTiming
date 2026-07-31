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
config/                  Django project (settings, urls, asgi/wsgi). health.py is /healthz —
                         the one ungated URL outside the timing device's, answering "ok" or
                         a 503 and deliberately nothing else: it is unauthenticated, so
                         which device is attached or whether this venue is timing would be
                         venue state handed to anyone who asks. It runs one SELECT 1,
                         because a process listening while its database has gone is the
                         failure a check exists to catch. It is also the only path exempt
                         from the HTTPS redirect (settings SECURE_REDIRECT_EXEMPT) — every
                         local probe asks for it over plain http — and a test refuses any
                         second entry on that list. csp.py is the
                         Content-Security-Policy middleware (see Security); media.py puts
                         /media/ behind the login; datasecurity.py restricts DATA_DIR to the
                         account running the server at startup (a 0o077 umask + chmod on POSIX,
                         an icacls grant naming SYSTEM and Administrators *by SID* on Windows,
                         because those names are localised). singleinstance.py takes an
                         exclusive lock on run/server.lock from asgi.py, so only one server process
                         ever serves an event (the channel layer, the CP540 reader thread and its
                         event loop are all per-process — a second worker splits the live updates
                         silently). Refused in a deployment, warned about while DEBUG is on.
                         tests.py holds the deployment tests (the DEBUG=False-only failures)
                         plus the cross-cutting file checks (CSP, the design-system
                         scales, the sidebar registry, the JS structural check).
                         hostility_tests.py is its sibling for what happens when a client
                         is *unkind*: it **discovers** every JSON endpoint from the
                         URLconf and asks each the same hostile questions, so the
                         endpoint added next month is covered the day it is added. The
                         malformed-id 500 was reachable on nine endpoints at once precisely because the
                         tests that existed named their targets one at a time. It also
                         holds the concurrency tests, which need
                         `django_db(transaction=True)`: the ordinary fixture wraps a test
                         in a transaction other threads cannot see, so a threaded test
                         written on it is a test of nothing.
                         settings.py: HTTPS is the default once DEBUG is off and there is exactly
                         ONE way off it — DJANGO_ALLOW_PLAIN_HTTP (the older per-setting hatches
                         DJANGO_SECURE_SSL_REDIRECT/DJANGO_SECURE_COOKIES now *refuse to start*,
                         so an old .env can't quietly serve a venue on plain HTTP), and setting it
                         prints a warning on every command. HSTS defaults short (300 s, no
                         preload): a pin can't be revoked before it expires and a venue hostname
                         served from a local CA gets reused, so a year is a trap — raise it with a
                         real public domain. Also SESSION_COOKIE_AGE (12 h, an event day, not
                         Django's fortnight) and the login-throttle limits. DATA_DIR (env
                         SLALOM_DATA_DIR, default BASE_DIR) is where everything the app *writes*
                         goes — db.sqlite3, media/, run/server.lock, timing_unrecorded.log. A
                         asgi.py is where a *server's* startup rules live, as opposed to
                         settings': the single-instance lock, the CP540 and backup
                         autostarts, the data-dir hardening, the expired-session sweep (so
                         there is no clearsessions schedule to add) and the refusal to
                         start with DEBUG off and an empty ALLOWED_HOSTS. That last one is
                         not in settings.py on purpose — collectstatic is a required
                         release step and the packaged build's own, and runs with DEBUG off
                         and no hosts quite legitimately.
                         checkout keeps them beside the code; the packaged Windows build points it
                         at %LOCALAPPDATA% because the next installer overwrites the code and the
                         database is the event. Anything written at runtime belongs under DATA_DIR,
                         never BASE_DIR — config/tests.py::TestWritablePaths holds the line.
deploy/                  Serving the app for real: systemd unit, Windows start-server.ps1 and a
                         Caddyfile — all pinned to exactly one Daphne process. Run-book: DEPLOYMENT.md
                         (§3.4 is the TLS decision: Caddy's local CA, Tailscale, or plain HTTP said
                         out loud).
start.ps1 / start.bat    The dev-machine one-click start (root level, double-clickable): installs uv
                         if missing, `uv sync` (which fetches Python 3.14 itself), migrates, offers
                         `createsuperuser` when the database has no accounts, then runserver. Refuses
                         to run when .env says DJANGO_DEBUG=False and points at deploy/start-server.ps1
                         — a development server must not end up serving a venue.
build/                   Packaging that same app as a one-click Windows installer for a machine with
                         no Python, no uv and no terminal: build.ps1 vendors a standalone CPython 3.14
                         plus every dependency from uv.lock, copies the code, runs collectstatic,
                         draws the icon (make_icon.py, from the CSS tokens), self-tests the payload
                         and compiles installer.iss into dist/SlalomTiming-Setup-<version>.exe.
                         launcher.py is what the desktop shortcut runs: it points SLALOM_DATA_DIR at
                         %LOCALAPPDATA%\SlalomTiming\data, writes a .env with a generated secret key
                         on first run, migrates, asks for the first operator account, opens the
                         browser and runs one Daphne process. Two rules the pipeline exists to keep:
                         the installed code is disposable and the data is not (hence DATA_DIR), and
                         no .pyc may ship — `__pycache__\<long migration name>.cpython-314.pyc` pushes
                         an install path past Windows' 260-char limit and rolls Setup back. See
                         build/README.md.
.github/workflows/       windows-installer.yml: the same build on a windows-latest runner (test
                         suite first, then build.ps1). Every *push* keeps its installer as a run
                         artifact and publishes nothing — that run is the check that packaging
                         still works. A *published release* builds at the release's version (the
                         tag, `v1.2.3` → 1.2.3, which becomes both the filename and Setup's
                         AppVersion; a non-version tag fails the build, a tag disagreeing with
                         pyproject.toml only warns, since the release already exists by then) and
                         uploads the .exe as a **release asset** — never to a branch: git keeps
                         every version of a file forever and a 28 MB Setup .exe recompresses
                         differently each build, so committed installers would add ~28 MB of
                         permanent repo weight each, while assets live outside the object store
                         and deleting one reclaims the space.
apps/accounts/           Access control. Login required everywhere and page-level roles:
                         pages.py is the registry (page key -> the (app, url_name) patterns it
                         covers; PAGES is also the sidebar order), a role is a Django Group with a
                         RoleAccess row listing its keys, and a user's access is the union of their
                         roles (superusers get everything). middleware.py gates on
                         resolver_match in process_view; pages.OPEN is the one ungated URL
                         (timing:signal — a device can't log in, so that view authorises itself:
                         see _signal_authorized, which requires the *timing* page for a
                         session-authenticated post). A URL may belong to two pages (the marshal
                         endpoints serve both Timing and Marshal Posts; timing:run-status serves
                         both Timing and Results, since a DNS is assigned from the results
                         table), which is why those views
                         re-check who is calling rather than trusting the gate.
                         throttle.py: failed logins counted per (username, IP) in the cache and
                         locked out past a limit, every attempt logged; views.LoginView is the
                         three hooks that use it. Counters clear on restart — acceptable because
                         the app is one process by design.
                         context_processors.access exposes the caller's page keys, so the sidebar
                         only lists what they may open (the gate itself is the middleware).
Cross-cutting bits       Each app also carries the ordinary Django plumbing: admin.py (models
                         registered for the Django admin — the fallback surface for anything the
                         app's own screens don't edit), apps.py, urls.py and migrations/.
                         apps/timing/routing.py is the WebSocket URL map (ws/timing/ → the legacy
                         TimingConsumer, ws/timing/live/ → TimingLiveConsumer), reached from
                         config/asgi.py. Three **context processors** run on every render (settings
                         TEMPLATES): apps/competitions/context_processors.active_competition (the
                         active competition plus its running classes and Overall groups, which is
                         how the sidebar lists a Results sub-page each), the accounts one above,
                         and apps/nav.context (which sidebar entry is the current page).
                         **templatetags/**: apps/competitions/templatetags/competitions_tags.py
                         (participant_classes — a participant's classes under the competition's
                         assignment method) and apps/results/templatetags/pdf_markup.py.
apps/audit.py            Who changed what. AuditMiddleware writes one line per
                         *mutating* request to `<DATA_DIR>/logs/audit.log` — user, IP,
                         view name, response code and the redacted payload. A middleware
                         rather than a call per view (forty endpoints is forty chances to
                         forget one, and the forgotten one is the one somebody asks
                         about), and a file rather than a table (a table means a DB write
                         on every edit, competing for the one SQLite write lock the CP540
                         reader needs). GETs are never recorded — the live views re-fetch
                         several times a second per open browser. Downloaded whole
                         (rotated files first, so it reads chronologically) from
                         accounts:audit-log, superuser-only.
apps/common.py           Helpers shared across apps: safe_next() resolves the POSTed ?next to
                         an in-app URL (rejecting off-site ones), so the unsaved-changes
                         modal's "Save changes" lands where the user was navigating.
                         other_signed_in_users() reads the live session table — how a page
                         owning an installation-wide setting knows whether changing it
                         would move somebody else's screen (see select_competition).
                         json_body() is the one door a posted JSON body comes through.
                         Three endpoints parsed their own and all three were 500s:
                         `json.loads` on *bytes* sniffs the encoding from the leading
                         octets, so a body starting with a null byte is read as UTF-16
                         and raises UnicodeDecodeError — a ValueError but **not** a
                         JSONDecodeError, which is what they caught; and valid JSON need
                         not be an object, so `"a string"` parses and every
                         `payload.get(...)` after it is an AttributeError. Anything that
                         is not an object comes back as {}.
apps/nav.py              Which sidebar entry base.html marks as current: entry id ->
                         the (app_name, url_name) pairs that are that page, plus
                         PARENTS (a parent is marked when any child is). A context
                         processor exposes it as `nav_current`. It is a registry rather
                         than a comparison in the template because a url_name is only
                         unique *within* an app: both entries asking whether url_name ==
                         "settings" is what made Competition Setup -> Results
                         (`results:settings`) light up Timing -> Settings as well. The
                         sets being pairwise disjoint, and every pair in them still
                         existing in the URLconf, are what config/tests.py
                         ::TestTheSidebarMarksOnePage checks — so the *class* of bug
                         fails a test rather than being noticed on a screen.
apps/competitions/       Competition, CompetitionType, CompetitionClass; active-competition
                         selection — which is one global flag for the whole installation, so
                         select_competition() names the other people signed in and refuses
                         without `confirm_switch` (competition_confirm_switch.html), then
                         announces the new event by name over the timing WebSocket
                         (services.notify_competition_changed) rather than letting every open
                         view quietly re-render as somebody else's event. CompetitionType is the discipline *and* the rules its
                         competitions run under, edited on a per-type Settings page
                         (types/<pk>/settings/): penalties on/off plus four whole-second
                         amounts (mandatory only while penalties are on — the form clears
                         them when it's off), tie-break rule, timing-device precision
                         (format_time() renders a time at it), and which participant details
                         the discipline collects. The tie-break rule now drives results ranking
                         (apps/results/resultscalc.py); the timing penalties screen consumes the
                         penalty amounts.
                         CompetitionType.PARTICIPANT_INFO is the single source of truth for
                         the last of those: setting -> (label, mandatory, Participant fields),
                         which both the settings page and the participant form build from.
                         CompetitionClass is fully dynamic (editable name, not a
                         fixed enum): is_running, age range, practice_runs, counted_runs,
                         scoring_method (CompetitionClass.Scoring: aggregate times, best run
                         only, or regularity test — recorded per class and computed by
                         apps/results/resultscalc.py),
                         plus position (list order) and run_position (which run it starts in;
                         classes sharing a run_position start together). Competition.run_groups()
                         returns the ordered runs. Three class methods are about how a class *reads*
                         and whether it works at all: display_name() writes it the one way every
                         results heading, the sidebar's Results sub-list and every PDF does —
                         "Class 7", the word **always** prefixed, never conditionally. It used to
                         be skipped for a name already opening with the word in any shipped
                         language, which made the heading depend on how somebody had typed a name;
                         the division is now that the organiser owns the name and the app owns the
                         word. name_hint() is the other half: a name that repeats the word ("Klasse
                         3" → "Klasse Klasse 3") is pointed out on the Classes page, where it can
                         be changed, rather than papered over at every render. And
                         scoring_warning()
                         names a setup that can never rank anybody (no counted runs; a regularity
                         test over a single run, which scores the whole field 0 and is then
                         silently ranked by fastest run). All of these are legal to save, so they
                         are shown on the Classes tile (and, for scoring, above the empty results
                         table) rather than refused. Setup UI is a section: a tile list
                         ("Manage competitions") + General / Classes / Run order / Penalties /
                         Results sub-pages that all edit the *active* competition (no pk in the
                         URL; the Results sub-page lives in apps/results).
                         MarshalPost (competition FK, 1-based number, tasks spec, one
                         handles_stop_line per competition, plus claim_token/claim_seen — a
                         heartbeated soft lock so only one device edits a post at a time)
                         records the marshal-post penalty setup: Competition.penalties_by_marshal_posts
                         turns it on, and each post watches a set of numbered tasks. taskspec.py parses/renders the
                         free-text task lists ("1, 5, 11-15") the Penalties page collects.
                         Deleting a post CASCADE-deletes every MarshalPenalty recorded against it,
                         so turning the toggle off (or reducing the post count) is confirmed first:
                         PenaltiesView counts what would go (_penalties_lost) and refuses without
                         `confirm_penalty_loss`, and the page shows the same number in a dialog
                         before submitting — the server check is what catches a stale page.
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
                         grants that the pattern never plays. A new competition has **no
                         pattern**: a pattern is what *Auto* timing needs, and Auto timing is a
                         choice — plenty of events run on the Manual view with competitors turning
                         up at the line in any order. Nothing else reads it (the dashboard and the
                         results derive from the entries and their classes), and Auto timing says
                         it needs one and links to where to build it rather than rendering its
                         apparatus around an empty order. Edited on the Run order page below
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
                         Participant.last_used_at is when the record was last *used* —
                         edited (every save, so an import or a merge counts) or entered
                         into a competition (a bib assigned or changed, via the post_save
                         on EventEntry, which is a different row and would not otherwise
                         touch this one). It exists because updated_at cannot answer the
                         retention question: somebody who has raced every year since 2019
                         and never changed their address has an updated_at of 2019 and is
                         not stale. Indexed — the sweep that will use it asks the whole
                         table. Not carried by apps/transfer: an import *is* a use, so the
                         importer's own save stamps it fresh.
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
  bibs.py                What a bib carries. TimedRun.bib_number is a loose integer, so a run
                         belongs to whoever wears the number: changing it hands every time
                         recorded under it to the next holder and takes on any time recorded
                         under the new one. bib_change_effect() counts both sides (a
                         placeholder row with no time on it doesn't count), and both doors
                         that can change a bib — the edit form and the list's inline field —
                         refuse until it has been confirmed.
  views.py               CRUD views + participant_check duplicate-detection endpoint. The list
                         is scoped to the active competition's type and only shows the columns
                         that type collects (club, licence); with no competition selected it
                         prompts to pick one and shows nothing, and adding is blocked.
                         The list row's expandable detail also carries the **whole-event
                         disqualification** (participant_set_dsq -> EventEntry.status = DSQ,
                         scoped to the *active* competition, needing a bib): the wider of the
                         two DSQs, deliberately away from the per-run one on the timing views,
                         and shown as a pill on the row so a disqualified starter reads as one.
                         It rides in the panel's action row beside Edit/Delete with what it
                         costs in its `title` — a rare action, not a field of the record.
                         ParticipantUpdateView re-renders its form with `confirm_bib_change`
                         (the modal) and participant_set_bib answers {"confirm": …} until the
                         caller sends `confirm` — see bibs.py.
apps/timing/            The current timing path is TimingSignal -> arrangement -> TimedRun,
                        surfaced on the live Manual timing view (`timing/manual/`, name `manual`) and the
                        Auto timing view — which share the same runs (see the sync below). The old
                        TimingEvent + connector-loop dashboard is legacy and slated to be redone.
  models.py              TimingSettings (singleton: device [Tag Heuer CP540 / Simulator],
                         start/finish channel — inputs 1–4 only, MIN_CHANNEL/MAX_CHANNEL, because
                         every supported device numbers its inputs that way and a channel outside
                         it could never match a signal (a finish channel of 7 silently meant
                         nothing was ever a finish); the same number for both is one light barrier,
                         IP + TCP port for the CP540, plus
                         ignore_incoming — the red operator "Lock" switch on the timing pages: while
                         on, every incoming signal is stored ignored [straight to the ignore list]
                         instead of placed into a run, applying to every device/simulator, and
                         reader_enabled — whether the operator left the device connected, the one
                         bit of the reader that outlives the process; see cp540.autostart),
                         TimingSignal (the raw device
                         inbox — running number, port, is_manual, device_time; stamped with the
                         active competition; `ignored`; `entered` == operator typed the time by
                         hand, device failed — distinct from is_manual), TimedRun (one run: a
                         start_signal paired with a finish_signal, each OneToOne so a time is used
                         once, plus the operator's bib/class/run/penalty entry, pylon_adjust/
                         task_adjust/stopline_adjust — the Auto-timing timekeeper's signed +/- to
                         the totals in marshal mode (a Pylons/Task/Stop line stepper each),
                         `manual_run_time` [operator-typed run time, overrides the computed elapsed
                         when the device gave no usable pair], `manual_entry` [the operator owns
                         this run's identity: it claims its slot in the Auto order and the auto
                         binding won't reassign it] and `status` [TimedRun.Status — the run ended
                         DNF/DNC/DNS/DSQ instead of in a time; never scored whatever times sit on
                         it, and never filled by an incoming signal, see runstatus.py]),
                         MarshalPenalty (one per run×marshal-post:
                         aggregate counts, a per-task `detail` JSON, and a `submitted` flag ==
                         locked, written from the Marshal Posts page and shown on Auto timing), and
                         legacy TimingEvent.
  autotiming.py          The Auto timing view's logic. The start order (Competition.start_lists()
                         = run order × start pattern) is a flat list of slots; a saved override
                         (Competition.auto_timing_order, a list of slot keys) reorders it. bind_runs()
                         aligns runs to that order: a *manual* run (manual_entry) claims the slot its
                         identity matches — so a run pre-entered on Manual timing shows pre-filled and
                         incoming device times step over it — and the remaining *auto* runs bind to
                         the still-empty slots positionally (n-th started auto run = n-th free slot).
                         apply_bindings() then folds each auto run's slot identity (bib/class/run)
                         onto the TimedRun objects *in memory*, so the Manual view reads the same
                         competitor — the Auto→Manual half of the sync — and sync_bindings()
                         persists it. Which of the two you want is the rule: **a reader binds, a
                         writer persists.** Rendering is a GET and must not take the write lock the
                         timing rig's own thread needs to record a time (several open browsers
                         refreshing on one nudge raced each other on those rows), so every read
                         path — both timing views, the Dashboard, results, the PDFs — calls
                         apply_bindings on the runs it is about to render, and only ingest, a
                         reorder and the operator's edits (views._rebind_and_broadcast) call
                         sync_bindings. Both take an optional `runs` list (autotiming.all_runs) so
                         one read serves the whole request. Penalties aren't cached: they're computed live
                         from the posts + the run's own counts (penalty_seconds), so both views agree.
                         current_run()/current_index() pick the run with the latest timing
                         activity (last start, or a finish that just came in for an earlier starter),
                         not merely the last to start. penalty_seconds()/_penalty_lines() are the one
                         penalty source shared with the Manual view and results, additive from real
                         sources: in marshal mode a run's total = the marshal-post counts + the counts
                         the operator keyed directly onto a run they own + the signed timekeeper adjust
                         (an auto-bound run's own counts are a stale cache, ignored); otherwise it is
                         the run's own counts. So a marshal penalty on an operator-selected run, or a
                         penalty typed on Manual timing, shows in both views. own_counts_apply() is
                         the same rule asked the other way round — whether a run's own counts still
                         reach its total — which is how the Manual view knows to disable steppers
                         that would take a number and change nothing. serialize() builds the
                         page: per run the grand penalty totals, the
                         total time (run + penalty seconds), and per post a box with counts,
                         submitted/locked state and per-task detail. marshal_state() returns the
                         current competitor + the post's detail. Two small pieces of live state ride
                         along in both payloads: barrier_phase() (single light barrier only — what
                         the *next* pulse will count as, computed from the runs already read, since
                         the phase is otherwise invisible and one stray pulse inverts it for the
                         rest of the event) and _empty_reason() (with no start order, which piece of
                         setup is missing — no running class, nobody registered, or classes
                         granting no runs — instead of one sentence blaming the run order. "No
                         pattern" is not one of these: it is answered before any of this, by
                         AutoTimingView.needs_pattern, which replaces the whole page).
  arrangement.py         Causal pairing of signals into runs: a finish joins the oldest open
                         start that began before it; a start never adopts an earlier orphan
                         finish. Ordering (which run is newest, which open start is oldest) is by
                         *arrival* — received_at/id — NOT the device's own clock: a real device
                         runs on its internal clock (often not wall-clock, e.g. 00:40:xx), so
                         device_time would sort fresh times below older data. device_time is used
                         only to compute a run's elapsed (finish − start, a correct delta whatever
                         the clock reads) and to reject a backwards pairing. Same rule in
                         autotiming (started_runs/_signal_activity/bind_runs) and results.
                         A start first fills the oldest empty *placeholder* row (one
                         pre-entered by the operator, no times *and no typed run time* yet) before
                         opening a new run. ignore keeps a row that still carries a bib/run/typed
                         time (placeholder) rather than deleting it. assign() (drag a time onto a
                         slot) drops the dragged signal in first so re-homing the previous occupant
                         can't break the OneToOne; a measured occupant it displaces keeps its own
                         row, a keyed-in override (entered) is discarded. ingest()/detach()/assign()/
                         rows(); rows() is newest-first with placeholders on top. effective_role()
                         handles a single light barrier (start_channel == finish_channel): the one
                         channel alternates start/finish/start/…
  runstatus.py           Closing a run with a **state code** instead of a time: DNF / DNC / DNS /
                         DSQ. Three surfaces write one — Manual timing (a Status column), Auto
                         timing (Status buttons on the tiles) and the results table's
                         not-yet-ranked block (a DNS button where the time would be) — and all
                         three come through here, because two of them can name a run that does
                         not exist yet: a competitor who never started has no signal, so
                         `run_for_slot()` (moved here from views._run_from_slot, which still
                         aliases it) builds the row from their **start-order slot key**. Setting
                         a code takes the run over (`manual_entry`), so the positional binding
                         can't hand it to the next starter. What a code means for a *result* is
                         apps/results/resultscalc.py's business, not this module's.
  calc.py                Run time (integer-microsecond truncation to the type's precision, never
                         rounded); resolved_run_time() prefers a run's manual_run_time override;
                         total penalty; fixed-decimal formatting (format_precision) plus
                         format_clock() which renders a time as mm:ss.xxx (used by the results tables).
  dashboard.py           The organiser Dashboard's read-only overview (at "/"). serialize()
                         reuses autotiming.serialize() so its numbers always match the timing
                         views: overall run progress (finished vs the start order's expected
                         slots), per-class done/running/not-started state with a completion
                         percent, the competitor on course now (bib/name/class/run + times and
                         penalties once finished), and headline counts (participants, classes
                         done, runs remaining, non-starters, marshal posts).
                         Both sides of the progress bar answer to the state codes: a run is
                         **done** once it is *settled* — a resolved time **or** a state code (a
                         DNF is as final as a time) — and a competitor whose entry is DSQ/DNS/DNF
                         has their **unsettled** runs taken out of the denominator, since runs
                         they will now never take aren't outstanding work. Runs of theirs that
                         did settle stay counted on both sides.
  forms.py               TimingSettingsForm (IP + TCP port required only for the CP540, but
                         kept — not cleared — when another device is selected; defaults
                         192.168.1.50:7000).
  ingest.py              record_signal(): the one door every raw signal comes through — the
                         HTTP endpoint and the CP540 reader thread both call it. Persists the
                         TimingSignal *first* (independently of any browser — a closed tab loses
                         nothing; a reopened view reads it back), then folds it into the
                         arrangement, syncs bindings, broadcasts. A time is never lost: a locked DB
                         is waited out (WAL + busy timeout, apps.py) and the insert retried; if it
                         still fails the signal is appended to a durable recovery file
                         (timing_unrecorded.log) and record_signal returns None. The two *reads*
                         it needs first — the active competition and the operator lock — are
                         inside that same guard, which they were not: they sat above it,
                         unretried, so a lock while reading either raised straight out of
                         record_signal and the time reached neither the table nor the recovery
                         file. Placement is a second retried step — if it loses a lock race the
                         signal is already saved and gets re-placed by arrangement.reconcile() on
                         the next signal; it catches DatabaseError, not just OperationalError,
                         because two signals placed at once can both try to pair with the same
                         start and TimedRun.start_signal is a OneToOne. When the
                         operator Lock (TimingSettings.ignore_incoming) is on, the signal is still
                         captured but stored ignored and *not* placed — it lands on the ignore list.
  cp540.py               Tag Heuer CP540 driver: a daemon reader thread (module-level `reader`)
                         holding a plain-TCP line socket, started/stopped from the Settings page.
                         Parses only `TN` lines (running number, input 1–4 or M1–M4 → port +
                         is_manual, and the mm:ss.fffff time; the net-time form's extra leading
                         column is handled by locating the input/number relative to the time
                         token) into record_signal(); every wire line is kept in a ring buffer the
                         Settings page polls (timing:cp540-status) to show a live debug log.
                         A dropped link never ends the session: _worker loops over _session(),
                         reconnecting with backoff (_RETRY_DELAYS, napped in short ticks so
                         Disconnect stays instant) until stop() — a knocked cable would otherwise
                         lose every later time silently. Each state change nudges the live views
                         (_set_status → notify_live), and link_state() is what they render: for a
                         device that must hold a connection, whether it has one. The simulator
                         holds none, so it never alarms. The thread itself dies with the process,
                         so autostart() re-establishes it from TimingSettings.reader_enabled —
                         called from config/asgi.py (the one entry point that is a running
                         server), never from AppConfig.ready(), and it never raises: an
                         unmigrated database means no autostart, not a server that won't boot.
  views.py               DashboardView (organiser overview) + dashboard-state JSON endpoint;
                         Settings page; standalone Simulator; live Manual timing view + a JSON
                         arrangement endpoint and mutate endpoints (run-update by run id — marks
                         the run manual_entry, but only when the payload carries an *identity*
                         field (IDENTITY_FIELDS: bib/class/run); a penalty stepper used to take the
                         run over too, which pinned an auto-bound run to the slot it happened to be
                         showing —, ignore, pair [also takes a slot_key, so a time can be
                         dragged onto an upcoming Auto competitor with no run yet], run-status
                         [close a run with DNF/DNC/DNS/DSQ or clear it — by run id, or by slot_key
                         for a competitor with no run at all; see runstatus.py], set-time [type a
                         start/finish by hand, displaced device time kept on the rail], set-runtime
                         [type a run time]; the slot_key path creates the run via _run_from_slot).
                         Every operator-entered number is bounded here, because SQLite stores an
                         out-of-range value rather than refusing it: MAX_RUN_SECONDS + digits-only
                         parsing in _parse_duration (an over-long Decimal makes *every later read*
                         of that row raise, which used to 500 both timing views and the dashboard
                         for the rest of the event), and MAX_PENALTY_COUNT in _as_count/_as_signed.
                         serialize_arrangement() applies the Auto binding to the rows it renders so
                         the two views stay in step, and the endpoints that *move* that binding
                         persist it through _rebind_and_broadcast (see autotiming.sync_bindings).
                         _RowContext is why the table is affordable: the rows used to resolve their
                         own bib, their participant's classes and "is this run already recorded?"
                         once per row *and per dropdown option* — 293 queries at 40 starters, 1413
                         at 200, re-fetched by every open browser on every incoming time. The
                         context reads the entries and the runs once and answers from memory, so
                         the cost is flat in the size of the field (16 queries either way); the
                         mutate endpoints build one with _RowContext.load() *after* their save,
                         because over-max counts the row being saved. A context is a snapshot —
                         never build one before the writes of a request. serialize_arrangement (and
                         autotiming.serialize) also
                         carry `device_link` (cp540.link_state) so a lost device raises its alarm
                         on the timing pages, not only on the settings page. timing_signal ingests device posts (csrf-exempt, since a
                         real device can't send a token) and nudges live views to refresh — but it is
                         the *simulator's* door and is refused (409) unless the simulator is the
                         selected device, so only one source ever writes at a time (the CP540 reader
                         is likewise stopped when any other device is selected).
                         AutoTimingView + auto-state/reorder/reset-order endpoints; auto-adjust
                         (timekeeper +/- to a run's totals); marshal-state (the current competitor
                         for a post) + marshal-submit (a post's penalty + per-task detail, refused
                         once submitted/locked) + marshal-unlock / marshal-lock / marshal-lock-all
                         (timekeeper reopens or force-locks one or all posts) + marshal-task-edit
                         (timekeeper edits a locked post's per-task pylons/stop-line) tie the
                         Marshal Posts page to Auto timing. marshal-claim/release/claims enforce
                         one device per post — and that claim is *authorisation*, not decoration:
                         the access gate can't tell a timekeeper from a marshal (both pages grant
                         the same URLs), so the views draw the line. marshal-submit from a
                         non-timekeeper must present the post's claim_token (constant-time
                         compared) and name a run inside MARSHAL_RUN_WINDOW — the last few started
                         runs, so a tap the network swallowed still lands from the page's outbox
                         while a run from an hour ago can't be rewritten. unlock / lock /
                         lock-all / task-edit are timekeeper-only (403 otherwise). All share
                         broadcast_live()/timing_live
                         so a device post, a marshal tap or an unlock nudges every open Auto timing
                         and Marshal Posts page to re-fetch.
  connectors/            (legacy) TimingDeviceConnector ABC + SimulatorConnector for the old
                         connector-loop dashboard; get_connector() reads settings.TIMING_CONNECTOR.
  services.py            run_ingestion() [legacy] + Channels group names (timing_updates,
                         timing_live) + the two nudges every live view listens for:
                         notify_live() ("re-fetch") and notify_competition_changed(name) ("the
                         active competition moved under you"), both safe from a request handler
                         or a background thread.
  consumers.py           TimingConsumer (legacy dashboard) + TimingLiveConsumer (pushes refresh
                         and competition-changed nudges to open live views, group
                         "timing_live"). It also answers the client's heartbeat with a pong: a
                         socket can die without a close frame, and a page that can't tell is a
                         frozen screen with a green light on it.
  management/commands/run_timing_connector.py   [legacy] runs the connector loop
templates/timing/        dashboard.html (organiser overview), settings.html, simulator.html
                         (standalone, no app shell), live.html (Manual timing), auto.html (Auto timing),
                         _device_alarm.html (the device-link banner both timing pages include),
                         _live_connection.html (the WebSocket-connection indicator every live
                         view includes — Marshal Posts too, which is why it lives here),
                         _time_legend.html (what the time-chip colours mean — measured /
                         manual trigger / typed by hand. Persistent, beside the table on both
                         timing pages: they are load-bearing distinctions during timing and
                         used to be explained only inside the "?" modal nobody opens mid-run),
                         _barrier_phase.html (single-light-barrier rigs only: what the next pulse
                         will be read as, and that ignoring a stray time puts the sequence back)
static/js/               dashboard_overview.js (organiser Dashboard: renders the stat tiles,
                         progress ring, per-class board and current-competitor card from the
                         dashboard-state JSON, re-fetching on each timing_live WebSocket nudge) +
                         dashboard.js (legacy) + timing_live.js (Manual timing view: render, edits,
                         drag-to-pair, drag-to-rail-to-ignore, double-click a Start/Finish/Run-time
                         to type it by hand [entered times highlighted], WebSocket refresh) +
                         auto_timing.js (Auto timing: draggable start order, prev/current/next tiles,
                         marshal boxes, re-pair, same double-click manual time entry, WS refresh).
                         marshal_posts.js pushes taps/submit to timing:marshal-submit and pulls the
                         current competitor via timing:marshal-state. Nothing is fire-and-forget:
                         a push goes into an outbox held in localStorage (so it survives a reload
                         or a phone walking out of Wi-Fi range) and is retried with backoff until
                         the server takes it, still last-writer-wins per run+post so rapid taps
                         can't land out of order. The board says "Sending…" / "not sent yet", and
                         the "submitted" toast waits for the server rather than claiming it early;
                         a 409 means the run is already locked and is dropped quietly.
                         device_alarm.js renders the shared device-link banner from `device_link`;
                         barrier_phase.js the single-barrier phase pill from `barrier`, both called
                         from each view's own render pass.
                         marshal_posts.js also *holds* a competitor change while the board has
                         unsubmitted taps: the timekeeper's "current" follows the latest timing
                         activity, so with two runners on course it flips back and forth, and
                         swapping under the marshal's fingers would throw away what they had
                         already judged. An amber bar offers the switch; a clean or submitted board
                         still follows immediately.
                         ignored_panel.js owns the Ignored-times rail for *both* timing views
                         (they used to carry a copy each, differing only in which drag handler
                         they wired up). It splits Start/Finish only when the rig has two
                         channels — `autotiming.ignored_split`; with one light barrier an
                         ignored signal has no role, and asking `signal.role()` anyway put
                         every chip under Start and left Finish permanently empty. Each
                         column shows the **ten most recent** and folds the rest behind a
                         "show all": a morning of practice runs reaches a couple of hundred
                         chips, and the ones that matter are always the ones that just
                         arrived — which is why `ignored_signals()` ordering newest-first is
                         load-bearing rather than cosmetic. A chip shows the device's time
                         and nothing else. It used to carry how long ago it arrived as well,
                         which on a list already in arrival order and cut off after ten is
                         the same fact twice — and the timestamp that needed rode along in a
                         payload every open browser re-fetches on every incoming time. There
                         is deliberately no clear-all: an ignored signal is still the only
                         record the device fired, and this app does not delete recorded times.
                         live_socket.js owns the WebSocket for all four live views (no other
                         file may call `new WebSocket` — a test enforces it): reconnect with
                         backoff, a heartbeat so a link that died without a close frame is
                         noticed, the connection indicator, the "current event was changed"
                         bar, and — the point — a re-fetch on every reconnect, since a signal
                         that arrived during an outage is otherwise invisible until the next
                         one happens to arrive.
apps/results/           A "Results" landing page (index) listing every running class + the
                        Overall pages, per-class ranked tables, cross-class Overall tables, and a
                        Competition-Setup "Results" sub-page that configures the columns.
  models.py              RESULT_COLUMNS: the results column vocabulary — key -> (label,
                         availability, group). availability gates a column on a CompetitionType
                         flag (co_driver/club/email/phone/street+city[requires_address]/vehicle/
                         license), or is always-on (driver_name, birthday, birth_year), or the
                         special "any class has practice runs" for the training column; group is
                         which fixed layout column it renders into (name / address / vehicle /
                         licence blocks, or the runs region). ResultColumnSettings: one General
                         row (competition_class null) holds the default columns + the show_overall
                         toggle; a per-class row holds *additions only* (a class can only add
                         columns General doesn't already show — no inherit/override). columns_for()
                         = general ∪ class additions; available_keys() filters the vocabulary to
                         what the type collects; overall_enabled() reads show_overall.
                         ManualTieResolution: a timekeeper's saved ordering of a tie group, keyed by
                         scope ("class:<pk>" / "overall:<method>:<runs>") + the member set + the
                         `score` they were tied on — an ordered [entry_pk, occurrence, rank] list
                         that applies only while the same competitors are tied at the same score
                         (else ignored; a decision about 60.00 s is not a decision about 58.00 s,
                         and a row with no score predates the field and never applies). Nothing
                         deleted a resolution whose tie had dissolved, so saving one now sweeps
                         this scope's rows that no longer match a live tie (results:tie-resolve —
                         a *write* path; recompute must never delete during a render).
                         ResultsPdfLayout: the per-competition results-PDF header/footer — the
                         editor's limited rich text (`header_html`/`footer_html`, wildcards
                         unresolved), `orientation` (portrait / landscape / auto-fit), and the two
                         optional logos with their rendered heights in mm. `increment_start_year`
                         is what the `#increment` wildcard counts from. One row per competition,
                         `for_competition()` returning a transient blank one when there is none.
  resultscalc.py         The scoring/ranking engine. RunIndex reads the event's runs *once* and
                         groups them by (bib, class, occurrence, run type), binding Auto timing's
                         positional identity onto them in memory (autotiming.apply_bindings) so
                         results read one representation — rendering a table, or a PDF, is a pure
                         read. Every competitor's runs used to be fetched twice over, counted then
                         practice, per competitor per class: 149 queries for a 40-strong class, 629
                         for 200, and "export everything" multiplied that by the class count.
                         sync_identities() is the *persisting* half (autotiming.sync_bindings) and
                         is not needed to display anything. run_penalty_seconds() is the single penalty
                         source (marshal-post totals + timekeeper adjust when penalties_by_marshal_posts,
                         else the run's own counts). _competitor() gathers a competitor's counted and
                         practice (training) runs and their total_time (run + penalties). scores by
                         CompetitionClass.Scoring (aggregate sum / best-run min / regularity
                         spread — lower wins), then _rank() (dedup_key-parameterised) assigns ranks:
                         a competitor's repeat entries after their first ranked one are skipped, and
                         equal scores the type's tie_break can't separate share a rank + are flagged
                         for inspection. Competitors sharing a score form a tie group (_rank_group):
                         a rule that fully separates them ranks them distinctly, tie_state "auto"
                         (green flag); a rule that can't (or Manual tie-break) leaves them sharing a
                         rank, tie_state "pending" (red, inspect) until a stored ManualTieResolution
                         orders them (tie_state "manual", green). tie_start/tie_size expose the group's
                         first rank + size so an edit knows the legal ranks. validate_resolution()
                         checks a posted order is a legal ranking (start fixed; each rank ties the
                         previous or takes its own position — 1,2 or 1,1 but never 2,1);
                         live_tie_keys() is the set of tie groups a fresh table actually has, which
                         is how the save path knows which stored resolutions are dead.
                         compute_class_results() ranks one class; overall_groups()
                         lists the distinct (scoring_method, counted_runs) groups (one Overall page
                         each) and compute_overall_results() ranks across a group's classes (dedup by
                         participant *and* class, so one competitor shows once per class). Rankable =
                         every counted run recorded and no final state code — except best-run,
                         which needs one run. A skipped repeat entry still shows its gap to the
                         winner. **State codes** (`TimedRun.status`) become a competitor's own
                         outcome in `_final_status()`: a code on a *practice* run means nothing
                         here; a competitor is only settled once every counted run carries a time
                         or a code (so a DNF on run 1 doesn't retire somebody with run 2 still to
                         drive) — except a whole-event DSQ (`EventEntry.status`), which settles
                         them at once. All counted runs DSQ → DSQ, all DNS → DNS, best-run
                         scoring with a run still timed → they rank on it, anything else → DNC.
                         `_split()` therefore puts every competitor in exactly one of three
                         groups: `ranked`, `status_rows` (settled on a code — DNS, then DNC, then
                         DSQ, by bib within each) and `unranked` (still waiting on a run).
  views.py               build_table() assembles the shared layout + per-row lines both the class
                         and Overall tables render (a column group renders only when a field in it
                         is enabled; each enabled field is one line so rows align; _value() renders
                         each field, e.g. driver_name -> "Last, First"). It returns the rows in
                         the engine's three groups; a run cell is a time, the state code the run
                         was closed with, or — in the not-yet-ranked block only — the slot key a
                         DNS button posts. The tally below the table names the outcomes
                         (Starters / DNS / DNC / DSQ): "classified / not classified" never said
                         which one was being looked at. ResultsIndexView (the
                         landing list), ResultsClassView (one class), ResultsOverallView
                         (a scoring-method × counted-run group, with an extra Class column and the
                         General columns) all sync identities then compute. ResultsSettingsView: the
                         show_overall toggle, General columns, and per-class additions.
                         ResultsTieResolveView (JSON endpoint results:tie-resolve): recomputes the
                         table, validates a posted manual ordering, saves the ManualTieResolution
                         (with the score it was made at, sweeping dead ones).
                         The **PDF endpoints** all build sections and hand them to pdf.py:
                         export-class (one class), export-overall (one Overall group), export-all
                         (every running class + every Overall table in one file), export-sample
                         (the made-up rows sample_section() builds, so the settings page can
                         preview the layout before an event has any results) and pdf-logo-remove.
                         A filename carries a class name, which an organiser types, so the
                         Content-Disposition header is built by Django's own
                         `content_disposition_header()` rather than interpolated. Three
                         separate things went wrong when it was an f-string: a class named
                         `A"; x` closed the quoted string and appended a second `filename=`
                         of its choosing; a name in a script the header's latin-1
                         encoding can't hold came out RFC-2047-encoded and unreadable to
                         every browser; and a name with a newline in it raised
                         BadHeaderError, i.e. a 500. Django's builder handles all three,
                         reaching for RFC 5987 `filename*=` only when it is needed.
                         class_section()/overall_section() are the shared payload: title,
                         scoring_label, class_label (what `#class` resolves to), layout and rows.
                         A class heading is cclass.display_name(), never "Class " + name.
                         All reuse competitions.ActiveCompetitionMixin. The two PDF logos are
                         assigned straight from request.FILES (there is no ModelForm here), so
                         nothing checks them unless this page does — `_clean_logo` is an alias
                         for apps/results/logos.clean_upload, and the *import* path
                         (apps/transfer/importers) goes through logos.clean_bytes. One module
                         for both doors is the point: the import used to write whatever bytes
                         the archive carried under whatever name it asked for, which is how a
                         crafted `.zip` could plant an HTML file under /media/ and have the app
                         serve it from its own origin. Size, then Pillow's own
                         verification, then a name we generate rather than one we were given.
                         A refused logo is a message and the rest of the settings still save.
  pdf.py                 The **results-PDF export**: the same tables, on A4, for the notice board.
                         render_results_pdf() takes the *sections* views.class_section /
                         overall_section already built (layout + row dicts — the screen and the
                         paper are one computation, so they cannot disagree) and lays each on its
                         own page(s) with ReportLab: a header row repeated on every page, columns
                         scaled to span the full width from relative weights (_COLW), rows that
                         only ever break *between* rows, and every body row the same height (each
                         info field one line fitted to its column, run/total cells always two,
                         all vertically centred) — the print of the fixed layout the web table
                         uses. Each page carries the configured header (two optional logos
                         left/right with the rich header text centred *between* them) and footer
                         (rich text, export date/time in the machine's local zone bottom-left,
                         page/total bottom-right). Orientation is the layout's, or auto-fit from
                         the column count. Wildcards are resolved per section, because `#class`
                         means the table on *that* page.
  logos.py               The one door a results-PDF logo comes through, for both the
                         settings page (clean_upload, from request.FILES) and the import
                         (clean_bytes, from the archive). Size, then Pillow's own
                         verification, then a filename **we** generate — the archive's own
                         name is never used. It exists because the two doors were guarded
                         differently: the import wrote whatever bytes it was given under
                         whatever name it asked for, so a crafted `.zip` could plant
                         `evil.html` under /media/ and have the app serve it as text/html
                         from its own origin — with `SuspiciousFileOperation` one
                         `../` away. Same shape as the pdfmarkup sanitiser below, and the
                         same lesson: one check per *column*, not one per page.
  pdfmarkup.py           The header/footer's two halves. **Wildcards**: WILDCARDS is the token
                         vocabulary (#name, #date, #event_date_long, #year, #increment,
                         #discipline, #class) with the description the editor lists;
                         wildcard_values() resolves them for a competition and resolve_wildcards()
                         substitutes. **Rich text**: the editor's HTML is restricted to <b>, <br>
                         and size spans (`pdf-sz-small/medium/large`; the footer keeps no sizes) —
                         sanitize_header()/sanitize_footer() are the only door it goes through,
                         and to_reportlab_markup() translates what survives into ReportLab's
                         mini-markup. Three callers share it: the settings form's save, the
                         *import* path (apps/transfer/importers._import_results) and the template
                         filter below. Sanitising in one of those only is what made an imported
                         file able to run script in the importer's session.
  templatetags/
    pdf_markup.py        pdf_header / pdf_footer: re-sanitise the stored PDF header/footer as the
                         settings page loads it back into its contenteditable. Replaces a bare
                         |safe on a column an *import* can also write — never put that back.
  admin.py               The results models registered for the Django admin.
templates/results/       index.html (class + Overall cards), results_class.html, results_overall.html
                         (both include _results_table.html, which renders the ranked + unranked
                         tables from _results_head.html, _results_midcells.html and
                         _results_runcell.html — the fixed
                         multi-line layout: Rank | Bib | [Class] | Name block (driver+co-driver bold,
                         club, e-mail, phone) | Street/City | Vehicle | Licence/Birthday/Birth-year |
                         Training + counted run cells (time as mm:ss.xxx over "+N s" penalty) | the
                         scoring value (mm:ss.xxx over +gap to 1st)), and results_settings.html. The
                         last column shows the value competitors are ranked by, headed per method:
                         "Total" (aggregate sum), "Best run" (best-run min), "Difference" (regularity
                         spread). All rows are the same height (each line reserves its height, run and
                         total cells always render two lines) and vertically centred. A tied
                         competitor's rank cell
                         carries a flag (red pending / green resolved); clicking it opens the inline
                         editor (static/js/results_tie.js) — the tied rows become draggable and their
                         ranks editable, and Save posts to results:tie-resolve. Overall pages split by
                         scoring method AND counted-run count; the sidebar lists them above the
                         per-class pages. A class whose setup can never rank anybody (no counted
                         runs, or a regularity test over a single run) says so above its empty
                         table — CompetitionClass.scoring_warning, the same sentence the Classes
                         setup page shows on the tile.
                         **State codes**: competitors settled on one (DNS/DNC/DSQ) ride at the
                         foot of the *ranked* table with the code in the last column — their
                         event is over, they are just not in the placings. The not-yet-ranked
                         block below has **no Total column** (they have no total, and the cell
                         only ever echoed "REGISTERED" back) and puts a **DNS button** where a
                         run's time would be; static/js/results_status.js posts its slot key to
                         timing:run-status, which makes the run and closes it (a competitor who
                         never started has no run to name — see apps/timing/runstatus.py).
                         results_settings.html also carries the **PDF layout editor**: two
                         contenteditable boxes (header/footer) with bold, three sizes and a
                         wildcard insert list, the orientation choice, the two logo uploads with
                         their heights, and a "sample PDF" button — all driven by
                         static/js/results_pdf_editor.js, which only ever produces the restricted
                         markup pdfmarkup.sanitize_* accepts (the server re-checks; the editor is
                         convenience, not the guard).
apps/transfer/          Getting the data out: the **automatic backup** (the section's landing
                        page), an Export page and a three-step Import wizard — sidebar section
                        "Backup", one access-control page key `import_export` for all three.
                        The two halves answer different questions and are deliberately not the
                        same feature: a *backup* is the whole database on a timer, for getting
                        the event back after a laptop dies; an *export* is one event as a
                        portable `.zip`, for moving or archiving it.
                        The wizard's state is a staged file plus the session. Two export scopes:
                        a whole **event** (competition + type + classes + marshal posts +
                        participants/entries/assignments + timing + results config) and a whole
                        **competition type** (the type + every participant registered under it,
                        no event). An export is a `.zip`: `data.json` plus `media/` for the
                        results-PDF logos, so it is self-contained on a machine that has no
                        access to the source's MEDIA_ROOT.
  schema.py              The document's shape: format/version, the per-model field lists, and
                         dump()/load(). Every row carries a `ref` — its pk on the *source*
                         system, used only as a local id — because an import must remap all pks
                         onto the target's own (Django's own deserializer restores the original
                         pks, which would clobber unrelated rows). Values are encoded by JSON and
                         decoded back through the model field's own to_python(), so the two
                         directions can't drift — and then checked against that field's own
                         validators, so a damaged document is refused as a TransferError instead
                         of writing a value SQLite accepts but every later read of the row chokes
                         on (the same trap the timing views' bounds close, from the other door).
                         Deliberately not carried: auto timestamps,
                         MarshalPost.claim_token/claim_seen (which *device* holds a post) and
                         Competition.is_active (an import must never take over the running event).
                         TimingSignal.received_at *is* carried — arrangement.py orders runs by
                         arrival, so dropping it would reshuffle an imported event.
  exporters.py           The writing half: which rows each scope collects and in what order.
                         event_document() walks a competition (type, classes, marshal posts,
                         participants + entries + class assignments, timing signals and runs and
                         their marshal penalties, the results column settings / PDF layout / tie
                         resolutions) and type_document() takes a competition type with every
                         participant registered under it. Each returns `(document, media)` —
                         media being the PDF logo files the archive carries alongside data.json —
                         and filename() builds the download name. It is a *substitution*
                         sanitiser — anything not alphanumeric, `-` or `_` becomes a dash —
                         not an encoding: the name is only ever a suggestion here, so
                         reducing it is fine and losing a character costs nothing. That is a
                         different problem from results/views.py's, which has to put a
                         class name a person typed into a *header* and so hands it to
                         Django's builder intact.
  archive.py             The .zip read/write, and the one place a hand-picked file meets the app:
                         every way it can be wrong (not a zip, not ours, newer version, damaged,
                         too big) comes back as a TransferError sentence. Its JSON encoder
                         subclasses DjangoJSONEncoder
                         to *undo* that class's ECMA-262 truncation of times to milliseconds —
                         this is a timing system, so device_time/received_at must round-trip exact.
                         Every entry is read through _read_entry against a budget
                         (MAX_DATA_BYTES / MAX_MEDIA_ENTRY_BYTES / MAX_MEDIA_TOTAL_BYTES): a zip
                         declares how far it expands, so reading one without looking lets a 60 KiB
                         file take as much memory as whoever wrote it chose. Checked against
                         ZipInfo.file_size *and* against what actually comes out, since the header
                         is only a claim (a mismatch fails the CRC — caught, not raised raw).
                         MAX_UPLOAD_BYTES caps the file itself at the view; deploy/Caddyfile's
                         request_body sits just above it so the app's message wins over a bare 413.
  merge.py               Deciding what an imported participant means here (the novel part).
                         Each incoming row is matched against the participants already registered
                         under the same type on two rules — same name + date of birth, or same
                         licence number — and classified NEW / IDENTICAL (reuse the row untouched)
                         / CONFLICT (recognisable but the records disagree, or several candidates).
                         A licence match with a different name is offered but never auto-merged.
                         Comparison is case- and whitespace-insensitive. A CONFLICT is resolved by
                         the operator: keep them apart, or merge and pick a winner per differing
                         field (Difference carries a translated label for the review table).
  importers.py           plan() inspects a document without writing (the review step renders it);
                         commit() writes it in one transaction. Builds a `ref -> new object` map
                         per section and looks every FK up through it. Two places hide pks *inside*
                         text and are remapped explicitly — the Auto timing order's slot keys and a
                         ManualTieResolution's scope/members — since a stale pk there silently
                         misorders an imported event instead of failing. A type that already exists
                         by name can be reused / overwritten from the file / registered separately.
                         The results-PDF header/footer is the one field a document carries as
                         *markup*, and the settings page renders it into an editor — so
                         _import_results runs it through pdfmarkup.sanitize_header/footer, the same
                         door the editor's own save path uses. (The template sanitises again on the
                         way out; see apps/results/templatetags/pdf_markup.py. Sanitising in one
                         place only is what made an imported file able to run script in the
                         importer's session.)
                         Two incoming competitors merged onto one participant would break
                         one-entry-per-competition, so the second keeps the first's entry and the
                         operator is warned.
  csvimport.py           The *other* import, offered by the same Import page (no sub-page of
                         its own): registering a list of participants for the **active**
                         competition from a spreadsheet. No pk remapping and no merge wizard — a CSV is
                         written by a human, so instead it is unforgiving up front: the file is
                         checked whole and imported only if every line is good, and every
                         complaint carries the spreadsheet line number (the header is line 1).
                         All checks run even after one fails, so one upload lists everything
                         wrong. Which columns a file needs is not fixed — columns_for() follows
                         the active competition's type via CompetitionType.PARTICIPANT_INFO,
                         exactly like the participant form, and sample_csv() hands out a matching
                         example (semicolon-separated + BOM, so Excel opens it in columns).
                         Headers are matched loosely (case/punctuation plus an ALIASES table),
                         dates in ISO or German form, and the file may be UTF-8 or cp1252.
                         A participant already registered under the type with the same name and
                         date of birth is *reused*, never duplicated; someone already entered in
                         the competition is an error. Bibs are optional — rows without one are
                         given the lowest free numbers (_bib_allocator fills gaps).
                         Not handled: class assignment. Imported starters get a bib but no
                         ClassAssignment, so a Manual-assignment competition still needs them
                         assigned afterwards.
  models.py              BackupSettings — the *only* model in this app: one row (pk forced to
                         1) holding the destination folder, the interval (1–10 minutes), how
                         many copies to keep, and what happened last time. The last-attempt
                         fields are persisted rather than held in the thread because "when was
                         the last good copy" is the question the page exists to answer and has
                         to survive a restart. destination_problem() is checked when the
                         setting is saved *and* before every copy — a stick is unplugged far
                         more often than a setting is changed, and a backup that has quietly
                         been failing since lunchtime is worse than none.
  backup.py              The copy and the timer. Three things about the copy, each of which
                         was a wrong first attempt: it uses **SQLite's own online-backup API**,
                         not a file copy (the rig is writing through the database, and in WAL
                         mode the recent writes are in the `-wal` file beside it, so a copied
                         file is torn *and* short); **in one step** (`pages=-1`), because a
                         batched backup gives up its read lock between batches and SQLite
                         *restarts* it whenever another connection writes — under sustained
                         writes it never finishes, and one step costs nothing since WAL readers
                         don't block the writer; and **written to a `.partial` and renamed**,
                         so an interrupted copy is never sitting there under a plausible name.
                         prune() keeps the newest N (480 copies of a growing database is a full
                         stick, and then the copy that fails is the newest one). BackupRunner is
                         the thread — ticks every 2 s so switching it off takes effect now, never
                         lets an exception end itself, and records the failure on the row instead
                         of raising at nobody. autostart() from config/asgi.py, like cp540's.
                         There is deliberately **no "back up now" button**: the point is that the
                         operator doesn't have to remember, and a button invites them to think
                         they should. Saving a destination runs a copy immediately instead.
  staging.py             Where an uploaded archive waits between the wizard's steps: a temp file
                         under a random token that only the session knows. Archives carry personal
                         data, so a staged file is deleted on commit/cancel and stale ones swept.
  folders.py             Browsing the *host's* folders, so the destination can be picked
                         instead of typed. A file input can't do this job: the browser
                         would offer the folders of whichever machine is displaying the
                         page, and the point of this app is that other people open it over
                         the venue network — a Desktop path from a marshal's phone means
                         nothing to the laptop doing the writing. Being a directory-listing
                         endpoint, it is deliberate about three things: **folders only**
                         (never a file name, never contents), **no path is trusted**
                         (resolved and checked to be a directory, so a `..` walk and junk
                         both come back as one sentence), and it is **gated** by the same
                         page key as the rest of the section. It does *not* report whether a
                         folder is writable — that means writing a probe file, and browsing
                         must not leave a trail of them; the one folder that matters is
                         checked by the form on Save.
  views.py               BackupView (the settings form; a save validates the destination, so a
                         bad one is refused while the operator is still looking at it, then
                         starts/stops the runner) + backup_status (JSON, polled by
                         static/js/backup_page.js — which reloads only when the last attempt
                         actually changed and the operator is not in the middle of something:
                         typing, *or* holding a dialog open, since a copy a minute meant a
                         reload a minute and the folder picker closed under whoever was three
                         folders deep in it) + backup_folders (the folder listing above).
                         ExportView (page + the .zip download), ImportView — the one Import page,
                         offering both kinds of file and dispatching on which file field was
                         submitted: an *archive* is step 1 of the wizard (parsed on upload so a
                         wrong one is rejected while the file picker is still in front of the
                         operator), a *participant CSV* is registered on the spot and re-renders
                         the page with the result or the per-line faults. Only the CSV half needs
                         an active competition, so the page still works without one.
                         ImportReviewView (step 2: renders the plan, and
                         on POST commits), cancel_import. The review form names its inputs by the
                         incoming participant's ref — `choice-<ref>` ("create" / "merge:<pk>") and
                         `field-<ref>-<pk>-<name>` per differing field, keyed by candidate so
                         switching candidate can't inherit the other's choices. Anything absent
                         keeps that match's default, so an untouched review screen does the
                         obvious thing.
templates/transfer/      export.html (event + type tiles with what each file would contain),
                         import.html — both file pickers on one page: the export archive, then
                         the participant list, which doubles as the CSV specification (a folded
                         <details> holding the column table — name / detail / required / example
                         — for the active competition's type, opened automatically when a file
                         was refused, plus the sample download and, after a POST, either the
                         result or the per-line list of what was wrong) —, import_review.html
                         (counts, the competition-type choice, and one block per participant to
                         review: candidate radios plus a field-by-field existing/imported diff
                         table) and import_done.html.
```

### Timing UI (under the sidebar "Timing" menu)

- **Settings** (`timing/settings/`) — device + start/finish channel (Save is right there, next to
  the channels; inputs **1–4** only, since that is what the devices have, and the same number for
  both means one light barrier alternating start/finish — the timing pages then show which of the
  two the next pulse will be, because that phase is what a stray pulse inverts). Selecting the CP540 reveals a "CP540 connection" block (IP + TCP port, defaults
  192.168.1.50:7000, kept across device switches) with Connect/Disconnect: Connect starts the
  CP540 reader thread (apps/timing/cp540.py), Disconnect stops it, and each button greys out when it
  doesn't apply (already connected / already idle). IP/port are read-only while connected. Selecting
  any device other than the CP540 stops the reader (only one source writes at a time). "Start
  simulator" opens the Simulator in a new tab. A live status pill + raw-line log below the form
  (polling timing:cp540-status) shows the device stream verbatim for debugging. The reader
  reconnects on its own after a drop, so the pill also reads "Reconnecting…"; while it is not
  connected, both timing pages carry a red **device-link banner** (`_device_alarm.html`) saying
  times are not being recorded — the operator is never left timing against a dead link. Connecting
  is remembered (`TimingSettings.reader_enabled`), so a server restart mid-event brings the link
  back by itself (`cp540.autostart` from `config/asgi.py`) instead of leaving the device selected
  and nothing reading it; only Disconnect (or selecting another device) turns that off.

Every live view — Manual timing, Auto timing, Marshal Posts and the Dashboard — also shows whether
its own **WebSocket** is up (`_live_connection.html`: a quiet "Live" pill, a red bar while it is
not), reconnects with backoff, and **re-fetches on every reconnect**, because a signal that arrived
during the outage is otherwise invisible until the next one happens to arrive. All of that is
`static/js/live_socket.js`; no view opens a socket of its own.
- **Simulator** (`timing/simulator/`) — a standalone new-tab device emulator: a running clock, an
  auto-incrementing running number (with reset), a 2×4 pad (ports 1–4 light barrier, M1–M4 manual
  → same port, is_manual), and an on-page log. Each press POSTs a signal to `timing:signal`.
- **Manual timing** (`timing/manual/`, sidebar label "Manual timing", URL name `manual`) — the
  operator's live view for the active competition: start/finish
  times (shown at the type's precision, truncated) paired into runs (newest first), with bib, class,
  run, and penalty entry (−/+ steppers, out of the tab order), live over a WebSocket. Entering a bib
  looks up the name, sets the class (updating it if the bib changes, clearing it if the bib is cleared),
  and auto-selects the next not-yet-done run (P then C); an unknown bib is flagged but kept. Tab out of
  the bib field lands on the Class dropdown for a multi-class participant. The dropdown has one slot per
  class the participant is entered into — a class entered more than once shows as "Klasse 2 (1)/(2)"
  (TimedRun.class_occurrence tracks which), each with its own runs; a run already recorded, or a class
  whose runs are all done, is disabled, and the default class advances to the first slot not yet
  completed. Manual times are tinted vs light-barrier ones. In marshal mode a run the operator does
  not own has its penalty steppers **disabled** with the reason on hover: the marshal posts own that
  number and the run's own counts are ignored, so the steppers used to take a value that changed
  nothing. Nudging a stepper also no longer takes a run over — only entering a bib/class/run does.
  Hover between the header and the top row for a **+** to pre-enter an upcoming starter (an empty
  placeholder row); incoming starts fill placeholders oldest-first, so times populate bottom-to-top.
  A **Status** column closes a run without a time — DNF / DNC / DNS / DSQ, for that run only; the
  row takes an amber stripe down its leading edge (a *marker*, not a fill — as a full-bleed row
  tint, fifteen consecutive non-starters made the table a wall of amber with the rows that still
  needed work invisible in it), the Total cell carries the code, and it stops being a placeholder
  an incoming time could fill. The code is stated once: the Status control keeps only a border and
  a tinted cell to say it is set, because the two cells are neighbours with penalties off and both
  used to shout the same word.
  **Double-click** a Start, Finish or Run time (or an empty slot) to type it in by hand when the device
  didn't fire — a keyed-in time is a green "entered" chip (run time green + underlined), distinct from a
  measured one, and the run's total honours it. Ignoring is a **drag** to the Ignored-times panel on the
  right (`templates/timing/_ignored.html` + `static/js/ignored_panel.js`, shared with Auto timing);
  drag a chip back onto a run's slot (or double-click it) to use it (rejected with a wiggle if it
  would put a start after its finish). A time keyed in over a measured one keeps the measured one on the
  list. The **competitor's name has its own column** and takes every pixel the fixed columns don't
  need (`table-layout: fixed`, `.tt-name` unsized): it used to be a 7.5 rem line stacked under the bib
  input, so the one thing that can't be reconstructed from the numbers around it was the one thing
  being clipped — while three penalty steppers took three times that width. Every other column *is*
  fixed, so entering a bib still never shifts them, and each is sized to its content because every rem
  taken there is a rem the name doesn't get. Above the Ignored panel is a red **Lock** switch
  (`_input_lock.html`, fixed width so toggling never resizes it; shared with Auto timing via
  `timing:input-lock`): while on it sends every incoming time straight to the ignore list — a pause
  without disconnecting — pulsing red and syncing across open views over the WebSocket. It states its
  consequence in words in *both* states ("Times are being recorded." / "…nothing is being recorded."),
  because off it was an unlabelled grey pill whose whole explanation was a `title` nobody hovers
  mid-run. The page intro is in a **help pop-up**: the topbar "?" (base.html `topbar_actions` block)
  opens a page-internal modal (`help_modal` block); reusable by any page, and now the convention for
  every page-level explanation (see Design system).
- **Auto timing** (`timing/auto/`) — the order-driven live view. The start order (run order × start
  pattern) runs down the left as draggable tiles ("#3 C1"), grouped by run with a "Run · <classes>"
  divider that sticks to the top of the list and is replaced by the next run's as it scrolls up (so the
  header names the run shown at the top, not the running one). Each tile shows the total time (run +
  penalties), and dragging saves a persisted override — "Reset order", which *discards* that override,
  is deliberately the quietest control on the page rather than the filled flame button it was, which
  outranked the competitor on course. The column is wide enough for a real name (it capped at 20 rem,
  of which the name got ~150 px) and the list uses the page's height rather than a flat 70vh, which is
  what left the middle column standing half empty. The list follows the
  current starter automatically until the operator scrolls away; a "▲/▼ current" cue re-engages it.
  Incoming device times bind to the order positionally — no bib typing — while a run pre-entered on
  Manual timing claims its own slot (shown pre-filled) and incoming times step over it. The right shows
  the previous / current / next competitor with start, finish, run time and **total time**; **current**
  is the run with the latest timing activity (a fresh finish for an earlier starter surfaces it, not just
  the last to start) — which with two runners genuinely on course does flip back and forth, so the
  Marshal Posts board holds its competitor rather than following every flip (see below).
  With an *empty* start order the column says which piece of setup is missing — no running class, no
  start pattern, nobody registered, or classes granting no runs — and links to the page that fixes it. Double-click a Start/Finish/Run time here too to key one in by hand (entered times
  highlighted) — on an *upcoming* competitor with no run yet too: the slot's key creates the run
  (_run_from_slot), and keying a start makes them current. Scrolling the tiles (mouse wheel, one
  competitor per notch) or clicking a start-order tile browses the field without changing the current —
  a browse offset from the real current; the centred tile frames blue and reads "Next up"/"Previous", and
  the browse snaps back when a new starter becomes current. Beside the times, one box per marshal
  post shows its pylon/task/stop-line counts ("2 P · 1 T · SL"), turning green with a 🔒 once submitted;
  a 🔒 button to their left locks every post at once, and the timekeeper can +/- the run's Pylons, Task
  and Stop line counts (a marshal-driven run nudges an adjust on top of the posts; an operator-owned or
  non-marshal run's steppers edit its own counts, shared with Manual timing). Clicking a box opens a
  speech-bubble pop-up under it: a locked post shows +/-
  steppers to edit each task's pylons (and toggle the stop line) plus an **Unlock** button; an unlocked
  post shows the read-only breakdown and a **Lock** button. A **Status** row of DNF / DNC / DNS /
  DSQ buttons closes the shown run without a time (press the one already set to clear it) — offered
  on an *upcoming* competitor too, since a did-not-start is exactly the case with nothing recorded;
  the start-order tile then reads the code instead of a total.
  Ignore a wrong time by dragging it to the
  Ignored-times panel (the same rail as Manual timing, with the red **Lock**
  switch above it), and drag it back onto a slot to re-pair. A time chip can also be dragged from the
  current competitor onto another tile's Start/Finish slot — including an *upcoming* competitor with no
  run yet (their run is made from the slot key), moving a mis-attributed time onto the right starter.
  A run with **no slot in the start order** (more starts than the order expects) is a real time nobody
  owns: it renders as a flame-framed alarm tile reading "Unattributed time", says in a sentence what to
  do about it, and offers a one-click "Move to Ignored" — it used to render as `#? (kein Starter)` in
  exactly the same card treatment as a competitor, with no route out beyond knowing that times can be
  dragged. The page intro is behind the topbar "?" help pop-up.
- **Marshal Posts** is a top-level sidebar item (below Timing) — the marshal's phone surface (see the
  competitions app). It reads the current competitor from Auto timing over the timing WebSocket and
  pushes every tap and the final submit back (with the per-task detail) so the boxes above fill and go
  green. Once submitted the board shows a 🔒 and locks; a timekeeper Unlock reopens it, and the board
  resumes its exact per-task state for editing. Confirming a post claims it for that device
  (heartbeated); the post shows "— in use" and can't be claimed on another device until released or the
  claim goes stale. A tap the network swallowed is not lost: it waits in a localStorage outbox and
  keeps being retried, the footer says "Sending…" / "not sent yet", and a submit that never reached
  the server is delivered when the page next loads. The board follows the current competitor, but
  **not while it holds unsubmitted taps**: two runners on course make "current" oscillate, and a
  swap mid-judgement would discard what the marshal had already entered. An amber bar names the new
  competitor and waits for a tap ("Switch"); submitting releases the hold, and if the timing side
  flips back to the competitor being judged the offer simply disappears.

### Adding a real device connector

The **current** path (TimingSignal → TimedRun) has one real driver, the CP540 (apps/timing/cp540.py):
a reader thread that parses device lines and calls `ingest.record_signal()`. A new real device that
feeds the current timing views is the same shape — read/parse its stream, hand each signal to
`record_signal(running_number, port, is_manual, device_time)` — not the `TimingDeviceConnector` ABC.

The ABC below is the **legacy** connector-loop path (old `TimingEvent` feed), kept for reference:
implement `TimingDeviceConnector` (apps/timing/connectors/base.py) — `connect()`, `disconnect()`,
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
uv run python manage.py collectstatic    # required for any DEBUG=False run (see DEPLOYMENT.md)
uv run python manage.py makemessages -l de --no-obsolete        # after touching any translatable string
uv run python manage.py makemessages -d djangojs -l de --no-obsolete
uv run python manage.py compilemessages -l de                   # .mo files are committed — always recompile
uv run pytest                            # tests (pytest-django) — needs collectstatic first, see below
```

`runserver` alone drives the whole app, including the Dashboard at `/` and the Manual/Auto timing views
(they read `TimingSignal` -> `TimedRun` and update live over the `timing_live` WebSocket group). The
legacy `run_timing_connector` loop is only needed to feed the *old* `TimingEvent` connector-loop path.

**`collectstatic` is a prerequisite of the test suite, not only of a deployment.** `STORAGES` uses
WhiteNoise's *manifest* storage in every mode, so `{% static %}` resolves through
`staticfiles/staticfiles.json` — which is gitignored build output. A checkout that has never run
`collectstatic` fails most of the suite with "Missing staticfiles manifest entry", because every page
render 500s; the tests pass on a working machine only because that artefact is already lying there.
Run it once after cloning (and after adding a static file). The manifest's strictness is worth
keeping — it is what turns a `{% static %}` pointing at a file that doesn't exist into a failed test
rather than a dead timing view — but it does mean CI runs `collectstatic` before `pytest`.

## Deployment

`runserver` is for development only. A real event runs **one** Daphne process, optionally behind
Caddy for TLS — the whole procedure (install, configure, release, race-morning checklist,
troubleshooting) is in **DEPLOYMENT.md**, with the artefacts in `deploy/`. What differs from dev:

- **Static files are served by WhiteNoise**, not by `runserver`, so `manage.py collectstatic` into
  `STATIC_ROOT` (`staticfiles/`, gitignored) is a required release step — skip it and every page
  renders unstyled with dead timing views. Names are content-hashed
  (`CompressedManifestStaticFilesStorage`), so a changed stylesheet can never go stale on an
  operator's laptop; in DEBUG the plain names are used.
- **Uploaded media** (results-PDF logos) is written at runtime, so WhiteNoise can't serve it —
  `config/urls.py` wires `django.views.static.serve` for `/media/` in *both* modes
  (`DJANGO_SERVE_MEDIA=False` hands it to a reverse proxy instead).
- **Fonts are self-hosted** (`static/fonts/*.woff2` + `static/css/fonts.css`, licence in
  `static/fonts/OFL.txt`). Never reintroduce a `fonts.googleapis.com` link: a venue has no uplink,
  and it would send every visitor's IP to a third party. `config/tests.py` fails if one appears.
- **The secret key** must come from `DJANGO_SECRET_KEY` — settings raises `ImproperlyConfigured`
  when `DEBUG` is off and the checked-in development key would be used.
- Environment variables are documented in `.env.example`; nothing loads it automatically.

## Design system

Everything visual comes from the token block at the top of `static/css/main.css`. The rules below
each exist because breaking one is what made the app read as several products stitched together:

- **No raw colour, spacing, font-size, duration or z-index outside the token block.**
  `--space-1…10` (a 4px grid) for padding, gap and margin; `--text-2xs…4xl` for type; `--radius-*`;
  `--font-mono`; `--dur-1…5` for transitions; `--z-*` for stacking; `--sidebar-w`; the palette plus
  the `--success` and `--amber` families. A value that appears twice is a token. The scales are
  closed sets: a component that needs a step which isn't there means the *scale* is missing a step.
  This replaced 33 distinct font sizes, 25 gaps and 25+ paddings, which is why the same relationship
  used to be expressed slightly differently on every page — and later seven transition durations
  (several a rounding apart, so the same interaction felt different per component) and an
  eleven-rung z-index ladder in which **200 was used twice**, by the sidebar and by the
  event-changed bar, so which of two overlapping *fixed* elements won was settled by document
  order. Pinned by `config/tests.py::TestTheScalesAreClosed`. Two things are deliberately not on a
  scale, because they are a component's own dimension rather than a step: a scroll cap and the
  simulator clock's fluid `clamp()`. **Breakpoints can't be tokens** — `@media` cannot read a
  custom property — so the five are listed in the token block instead, to keep the set closed.
- **Every dialog is the app's own, and goes through `modalController`.** Nothing calls
  `window.confirm`/`alert`/`prompt`: a browser dialog wears the OS's styling, can't be
  translated by us, states its question as one unformatted string (which is why callers were
  gluing `"\n\n"` into it) and puts the answer behind a control that looks nothing like the
  page. `window.appConfirm({title, body, accept, danger})` and `window.appAlert(…)` in
  `shell.js` return a promise and fill the shared dialog in `base.html`. The **one exception**
  is `beforeunload` on tab close — the browser's is the only thing that can stop a tab
  closing, and the string is ignored by every browser anyway. `shell.js::modalController` is
  the other half: focus in, Tab wrapped, focus restored to whatever opened it, Escape and
  backdrop handled once — including for a dialog the *server* rendered already open (the
  type-change, penalty-loss and bib-change confirmations), which is the case that had a
  question on screen with the keyboard behind it. Pinned by `TestEveryDialogIsTheAppsOwn`
  and `TestModalsManageFocus`; a page toggling a modal's `.hidden` itself fails.
- **`form.submit()` is never what you want.** It skips HTML5 constraint validation *and*
  every `submit` listener, so an invalid form posts and the page's own submit handlers never
  run — including, in one case, the destructive-save guard that was itself a submit listener.
  Use `requestSubmit()`.
- **A focus ring is never taken away, only quietened.** The global `:focus-visible` outline is
  outranked on specificity by any component rule (`form input:focus` beats `input:focus-visible`),
  so a component's own `outline: none` silently removed the keyboard indicator from every input in
  the app — and on the PDF header editor it sat on the *element*, so no state brought it back. A
  component that wants its own soft ring for the pointer scopes the suppression with
  `:focus:not(:focus-visible)`. `TestKeyboardFocusIsVisible` fails on a bare one.
- **The app is light-only, and says so by staying silent.** `color-scheme` is deliberately *not*
  declared. Naming a dark scheme without shipping dark rules is what made browsers paint inputs,
  selects, date pickers and scrollbars dark against a permanently light page. A real dark theme
  means redefining the tokens under `prefers-color-scheme` — and only then re-declaring
  `color-scheme`.
- **One measure, one exception.** `.content` is `--content-max` on every page. The two timing views
  are operator screens rather than documents, so they take the wider `--content-max-wide` through
  `{% block content_class %}content--wide{% endblock %}` — declared in the stylesheet, not injected
  as an inline `<style>` override by whichever page felt cramped. Wide is still **bounded**: with
  `max-width: none` the run table stretched to any monitor and its one unsized column (the
  competitor's name) pocketed the difference. The Manual timing table is now `width: auto` with
  every column sized, so a row is the sum of its columns and nothing is left over to inflate a
  cell; the table and the Ignored rail centre together.
- **A field label is sentence case; uppercase micro-caps are for things that aren't labels** (table
  column headings, stat-tile captions, status pills, section eyebrows). Setup and Settings used to
  shout theirs (`GERÄT`, `TRAININGSLÄUFE`) while every other page spoke normally — and all-caps is
  worst exactly where German puts its longest compounds. Likewise **page-level explanation lives
  behind the topbar "?"** (`topbar_actions` + `help_modal`, reusable by any page); only a hint
  attached to a specific control stays in the body.
- **Flame means "seconds added", and nothing else.** `--flame` is the penalty colour — the
  results table's `.rt-pen`, the penalty chips, `pdf._PEN` — so anything else wearing it
  reads as penalised. The Dashboard's *total time* did, which made a clean run look
  punished. A figure that needs emphasis takes it from size or weight.
- **A state is marked, not filled.** A full-bleed row tint is only legible while the state
  is rare: forty closed runs is an ordinary afternoon, and the Manual timing table became a
  wall of amber with the rows that still needed work invisible in it. A stripe down the
  leading edge reads at any density.
- **"Nothing to operate" is a heading and a centred `.empty-state` card**, the shape Marshal
  Posts uses — never a bare paragraph at the top of a blank page, which reads as a page
  that failed to load. `.empty-state > p` carries its own measure, because the two timing
  views are `content--wide`.

Two more that are about *saying the same thing the same way*:

- **A time is truncated, never rounded** — `calc.run_time`, `resolved_run_time` *and*
  `format_clock`. The last of those used `f"{x:.3f}"`, which rounds; invisible on an
  already-truncated value, which is why it survived, but it is also handed sums and
  differences (run + penalty, gap to the winner). A run that crosses midnight is measured
  rather than dropped: both device times are clock *times*, so 23:59:59 → 00:00:02 is a
  wrap, not a negative run (`calc.MAX_WRAP_GAP_US` is what keeps a genuinely mis-paired
  time from coming back as twenty-three hours).
- **`calc.format_clock` is the one way an elapsed time is written** — `mm:ss.xxx`, everywhere: both
  timing views, the Dashboard, the results tables, the PDFs. `format_precision` is for callers that
  need the bare number, not for display. A penalty is a different quantity (whole seconds added,
  never measured) so it is written differently — and only one way, by `calc.format_penalty`
  (`+5 s`). Pinned by `test_every_view_writes_a_run_time_the_same_way`.
- **A page's own name is singular; "Results" is the list of them.** The Results landing page is
  "Results"; each class page is "Result Class 7" (`Ergebnis Klasse 7`) and the Overall page
  "Result Overall", in the topbar and the browser tab alike. The PDF headline matches
  (`Class 1 · Result · Aggregate times`).
- **`_("…")` inside an f-string is never extracted.** xgettext does not look inside f-strings, so
  such a string only translates by accident — when the same msgid happens to exist elsewhere.
  Bind it to a name first (`word = _("Result")`), then interpolate. This is why the PDF headline
  read "Ergebnisse" for years: it was borrowing the sidebar's msgid.
- **Django's `{# #}` is single-line only.** Its lexer matches `{#.*?#}` without DOTALL, so a comment
  that wraps is rendered onto the page for the operator to read. This escaped review twice; multi-
  line commentary goes in `{% comment %}…{% endcomment %}`, and `config/tests.py` now checks every
  template.

## Security

Four rules to keep in mind when adding anything to this app:

- **A login is not authorisation.** `AccessControlMiddleware` gates URLs by page key, so any view
  reachable from two pages (the marshal endpoints), or from outside the gate at all
  (`pages.OPEN`, i.e. `timing:signal`), has to decide for itself who is calling — see
  `_signal_authorized` and `_timekeeper_required` / `_marshal_may_write` in `apps/timing/views.py`.
  A new open or shared endpoint needs the same treatment, and a test that a *scoped* role is
  refused (the shared `client` fixture is a superuser, so it proves nothing here).
- **Every value that arrives in a file is hostile until checked**, and it gets checked on the way
  in *and* on the way out. The stored-XSS hole came from sanitising the PDF header only in the
  editor's save path while an import wrote the same column raw. Uploads are size-capped at the
  view and, for a zip, per entry against the declared *and* actual expanded size
  (`apps/transfer/archive.py`). Numbers keyed in by an operator are bounded in
  `apps/timing/views.py` because SQLite stores out-of-range values rather than refusing them.
  Every id a client sends goes through `_as_pk()` first: handing a non-numeric one to
  `filter(id=…)` makes Django raise while it prepares the query, which is a 500 rather than
  a 404.
- **Nothing on a page may be inline.** The app ships a strict Content-Security-Policy
  (`config/csp.py`): `script-src 'self'`, no `'unsafe-inline'`, no nonce. A CSP cannot tell
  our inline `<script>` from an injected one, so allowing ours allows the attack it exists
  to stop. Every script therefore lives in `static/js/`, page data crosses over through
  `json_script` (`window.pageData(id)` reads it defensively), strings through `gettext()`
  and the djangojs catalog, and form controls are found by `data-` marker rather than by
  the id Django rendered. `config/tests.py` fails on an inline `<script>`, a `style="…"`
  attribute, an `onclick=`, template syntax left in a `.js` file, or an escaped quote
  opening an argument — the last two being ways a script dies silently while the page
  still renders.
- **Every change is recorded.** `apps/audit.py` — see the layout section.

Failed logins are throttled and logged (`apps/accounts/throttle.py`), and the login throttle
also caps attempts per address, not just per (username, IP). Known and deliberately not
done yet: the WebSocket consumers check a page role, but there is no per-competition
scoping on them — a signed-in user holding a live page sees the nudges for whichever
event is active, which is the only event there is.

## Performance

The live endpoints are re-fetched by **every open browser on every incoming time**, so their cost
is paid once per competitor crossing a beam per screen. Two rules keep that affordable, and both
are pinned by tests (`test_live_endpoint_cost_does_not_grow_with_the_field`,
`test_live_endpoints_never_write`, and the results equivalents) because neither failure is visible
until an event is big enough to hurt:

- **The cost must not grow with the field.** Anything per row, per competitor or per dropdown
  option belongs in a batch read: `views._RowContext` (Manual timing), `resultscalc.RunIndex`
  and `resultscalc.event_data` (results — the second is what a *multi-table* export reads
  once instead of per class), `autotiming.all_runs` + one `apply_bindings` pass (Auto
  timing, Dashboard). The running classes are the other thing a loop must not ask for:
  `starters_by_class`, `run_groups`, `starters_by_run`, `start_lists`,
  `classes_for_participant`, `class_for_birth_year` and the results column vocabulary all
  take an optional `running=`, because age-based assignment resolves a competitor's class
  by walking them and asking inside the loop cost one query per starter — 114 at 100,
  against a flat 15 for manual. The rule was only ever *tested* for manual assignment,
  which is why nothing noticed; both are pinned now.
- **A read must not write.** See `autotiming.sync_bindings` — a GET that writes takes the lock the
  CP540 reader thread needs to record a time, and several browsers refreshing on one nudge raced
  each other on the same rows.

Measured on a real Daphne process, 200 starters with 400 runs already recorded (the state a club
event reaches by mid-afternoon), before → after the stage-5 work:

| | before | after |
|---|---|---|
| `timing:arrangement`, one client | 1415 ms (1413 queries) | **95 ms** (15 queries) |
| `results:class`, 200 competitors | 629 queries | **29 queries** |
| six clients refreshing flat out, worst recorded signal | 1851 ms | **796 ms** |

And from the later round on the same harness shape (query counts only):

| | before | after |
|---|---|---|
| `timing:auto-state`, age-based assignment, 20 / 50 / 100 starters | 34 / 64 / 114 | **14 flat** |
| `results:export-all`, 1 / 3 / 6 classes | 42 / 80 / 137 | **24 / 32 / 44** |
| `timing:auto-state` payload, 200 starters | 389 KiB | **259 KiB** |
| …the same with 4 marshal posts watching 6 tasks each | 1341 KiB | **260 KiB** |

No signal was lost and no refresh failed in either run, so SQLite against a live
multi-user event is **survivable at 200 starters** and does not force Postgres.
The payload headroom is now taken too: a nudge still
carries nothing and each client re-downloads the state, but what it downloads no longer
repeats itself. A marshal post's box is derived entirely from the *post* until somebody
records against the run, so it is sent **once** as `posts` and an item with nothing
recorded sends `marshals: null`; the page substitutes it (`auto_timing.js`). At four posts
watching six tasks that was 958 KiB of a 1341 KiB payload. The penalty steppers likewise go
only to items that have a run. A delta protocol is what is left, and it buys ~10 KiB a
refresh against a gzip that already exists in `deploy/Caddyfile` — not worth its failure
mode (a screen quietly wrong mid-event). The harness that produced these numbers is a scratchpad script, not part of
the repo; re-create it from this table's shape if you need to re-measure.

## Notes

- `CHANNEL_LAYERS` uses `InMemoryChannelLayer` — fine for a single local process. Switch to
  `channels_redis` only if this ever needs to run multi-process/multi-host. Its queues are bound to
  the server's event loop, so a background thread (the CP540 reader) can't `group_send` directly — a
  live consumer records the loop and `services.notify_live` schedules nudges onto it. That
  single-process requirement is enforced, not assumed: `config/singleinstance.py` (called from
  `config/asgi.py`) locks `run/server.lock`, so a second server is refused in a deployment. It steps
  aside on its own if the channel layer is ever swapped for a cross-process one.
- SQLite runs in **WAL mode** with a 30 s busy timeout (`OPTIONS['timeout']` in settings +
  per-connection PRAGMAs in `apps/timing/apps.py`), so the CP540 reader thread and web requests
  writing at once don't collide into "database is locked" and drop a time. A time that still can't be
  written lands in `timing_unrecorded.log` (see ingest.py) — never silently lost.
- The Dashboard at `/` is the **organiser overview** (apps/timing/dashboard.py + dashboard_overview.js):
  a read-only live status view derived from the same start order and runs the timing views use.
- The three `TimingEvent` bullets below describe the **legacy** connector-loop path (the old
  connector-loop *feed*, not the current Dashboard), kept working but slated to be redone; the current
  operator surfaces are the Manual timing and Auto timing views built on `TimingSignal` -> `TimedRun`.
- Every timing pulse is written to the DB (`TimingEvent`) before/as it's broadcast, so a dropped
  WebSocket or crashed view never loses data.
- `TimingEvent.bib_number` is matched against the current competition's `EventEntry.bib_number` at
  ingestion time to resolve a display name (bibs live on `EventEntry`, scoped per competition, not on
  `Participant`); the participant FK is nullable since a pulse may arrive before a bib is registered.
