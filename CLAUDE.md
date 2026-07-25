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
config/                  Django project (settings, urls, asgi/wsgi). singleinstance.py takes an
                         exclusive lock on run/server.lock from asgi.py, so only one server process
                         ever serves an event (the channel layer, the CP540 reader thread and its
                         event loop are all per-process — a second worker splits the live updates
                         silently). Refused in a deployment, warned about while DEBUG is on.
                         tests.py holds the deployment tests (the DEBUG=False-only failures).
deploy/                  Serving the app for real: systemd unit, Windows start-server.ps1 and a
                         Caddyfile — all pinned to exactly one Daphne process. Run-book: DEPLOYMENT.md
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
                         returns the ordered runs. Setup UI is a section: a tile list
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
                        surfaced on the live Manual timing view (`timing/manual/`, name `manual`) and the
                        Auto timing view — which share the same runs (see the sync below). The old
                        TimingEvent + connector-loop dashboard is legacy and slated to be redone.
  models.py              TimingSettings (singleton: device [Tag Heuer CP540 / Simulator],
                         single-digit start/finish channel, IP + TCP port for the CP540, plus
                         ignore_incoming — the red operator "Lock" switch on the timing pages: while
                         on, every incoming signal is stored ignored [straight to the ignore list]
                         instead of placed into a run, applying to every device/simulator),
                         TimingSignal (the raw device
                         inbox — running number, port, is_manual, device_time; stamped with the
                         active competition; `ignored`; `entered` == operator typed the time by
                         hand, device failed — distinct from is_manual), TimedRun (one run: a
                         start_signal paired with a finish_signal, each OneToOne so a time is used
                         once, plus the operator's bib/class/run/penalty entry, pylon_adjust/
                         task_adjust/stopline_adjust — the Auto-timing timekeeper's signed +/- to
                         the totals in marshal mode (a Pylons/Task/Stop line stepper each),
                         `manual_run_time` [operator-typed run time, overrides the computed elapsed
                         when the device gave no usable pair] and `manual_entry` [the operator owns
                         this run's identity: it claims its slot in the Auto order and the auto
                         binding won't reassign it]), MarshalPenalty (one per run×marshal-post:
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
                         sync_bindings() then persists each auto run's slot identity (bib/class/run)
                         back onto the TimedRun, so the Manual view reads the same competitor — the
                         Auto→Manual half of the sync. Penalties aren't cached: they're computed live
                         from the posts + the run's own counts (penalty_seconds), so both views agree.
                         current_run()/current_index() pick the run with the latest timing
                         activity (last start, or a finish that just came in for an earlier starter),
                         not merely the last to start. penalty_seconds()/_penalty_lines() are the one
                         penalty source shared with the Manual view and results, additive from real
                         sources: in marshal mode a run's total = the marshal-post counts + the counts
                         the operator keyed directly onto a run they own + the signed timekeeper adjust
                         (an auto-bound run's own counts are a stale cache, ignored); otherwise it is
                         the run's own counts. So a marshal penalty on an operator-selected run, or a
                         penalty typed on Manual timing, shows in both views. serialize() builds the
                         page: per run the grand penalty totals, the
                         total time (run + penalty seconds), and per post a box with counts,
                         submitted/locked state and per-task detail. marshal_state() returns the
                         current competitor + the post's detail.
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
                         (timing_unrecorded.log) and record_signal returns None. Placement is a
                         second retried step — if it loses a lock race the signal is already saved
                         and gets re-placed by arrangement.reconcile() on the next signal. When the
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
                         holds none, so it never alarms.
  views.py               DashboardView (organiser overview) + dashboard-state JSON endpoint;
                         Settings page; standalone Simulator; live Manual timing view + a JSON
                         arrangement endpoint and mutate endpoints (run-update by run id — marks
                         the run manual_entry —, ignore, pair [also takes a slot_key, so a time can be
                         dragged onto an upcoming Auto competitor with no run yet], set-time [type a
                         start/finish by hand, displaced device time kept on the rail], set-runtime
                         [type a run time]; the slot_key path creates the run via _run_from_slot).
                         Every operator-entered number is bounded here, because SQLite stores an
                         out-of-range value rather than refusing it: MAX_RUN_SECONDS + digits-only
                         parsing in _parse_duration (an over-long Decimal makes *every later read*
                         of that row raise, which used to 500 both timing views and the dashboard
                         for the rest of the event), and MAX_PENALTY_COUNT in _as_count/_as_signed.
                         serialize_arrangement()/timing_signal both run sync_bindings so the two
                         views stay in step; serialize_arrangement (and autotiming.serialize) also
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
                         one device per post. All share broadcast_live()/timing_live
                         so a device post, a marshal tap or an unlock nudges every open Auto timing
                         and Marshal Posts page to re-fetch.
  connectors/            (legacy) TimingDeviceConnector ABC + SimulatorConnector for the old
                         connector-loop dashboard; get_connector() reads settings.TIMING_CONNECTOR.
  services.py            run_ingestion() [legacy] + Channels group names (timing_updates,
                         timing_live).
  consumers.py           TimingConsumer (legacy dashboard) + TimingLiveConsumer (pushes refresh
                         nudges to open live views, group "timing_live").
  management/commands/run_timing_connector.py   [legacy] runs the connector loop
templates/timing/        dashboard.html (organiser overview), settings.html, simulator.html
                         (standalone, no app shell), live.html (Manual timing), auto.html (Auto timing),
                         _device_alarm.html (the device-link banner both timing pages include)
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
                         device_alarm.js renders the shared device-link banner from `device_link`.
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
                         scope ("class:<pk>" / "overall:<method>:<runs>") + the member set — an
                         ordered [entry_pk, occurrence, rank] list that applies only while the same
                         competitors are still tied (else ignored).
  resultscalc.py         The scoring/ranking engine. sync_identities() delegates to
                         autotiming.sync_bindings() (the same routine the timing views run), so
                         results read one representation. run_penalty_seconds() is the single penalty
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
                         previous or takes its own position — 1,2 or 1,1 but never 2,1).
                         compute_class_results() ranks one class; overall_groups()
                         lists the distinct (scoring_method, counted_runs) groups (one Overall page
                         each) and compute_overall_results() ranks across a group's classes (dedup by
                         participant *and* class, so one competitor shows once per class). Rankable =
                         a live status with every counted run recorded — except best-run, one run.
                         Incomplete / DNS / DNF / DSQ competitors are returned unranked, and a
                         skipped repeat entry still shows its gap to the winner.
  views.py               build_table() assembles the shared layout + per-row lines both the class
                         and Overall tables render (a column group renders only when a field in it
                         is enabled; each enabled field is one line so rows align; _value() renders
                         each field, e.g. driver_name -> "Last, First"). ResultsIndexView (the
                         landing list), ResultsClassView (one class), ResultsOverallView
                         (a scoring-method × counted-run group, with an extra Class column and the
                         General columns) all sync identities then compute. ResultsSettingsView: the
                         show_overall toggle, General columns, and per-class additions.
                         ResultsTieResolveView (JSON endpoint results:tie-resolve): recomputes the
                         table, validates a posted manual ordering, saves the ManualTieResolution.
                         All reuse competitions.ActiveCompetitionMixin.
templates/results/       index.html (class + Overall cards), results_class.html, results_overall.html
                         (both include _results_table.html, which renders the ranked + unranked
                         tables from _results_head.html and _results_midcells.html — the fixed
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
                         per-class pages.
apps/transfer/          Moving data between Slalom Timing systems: an Export page and a
                        three-step Import wizard (top-level sidebar item "Import / Export",
                        its own access-control page key `import_export`). No models — the
                        wizard's state is a staged file plus the session. Two export scopes:
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
  archive.py             The .zip read/write, and the one place a hand-picked file meets the app:
                         every way it can be wrong (not a zip, not ours, newer version) comes back
                         as a TransferError sentence. Its JSON encoder subclasses DjangoJSONEncoder
                         to *undo* that class's ECMA-262 truncation of times to milliseconds —
                         this is a timing system, so device_time/received_at must round-trip exact.
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
  staging.py             Where an uploaded archive waits between the wizard's steps: a temp file
                         under a random token that only the session knows. Archives carry personal
                         data, so a staged file is deleted on commit/cancel and stale ones swept.
  views.py               ExportView (page + the .zip download), ImportView — the one Import page,
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
  the channels). Selecting the CP540 reveals a "CP540 connection" block (IP + TCP port, defaults
  192.168.1.50:7000, kept across device switches) with Connect/Disconnect: Connect starts the
  CP540 reader thread (apps/timing/cp540.py), Disconnect stops it, and each button greys out when it
  doesn't apply (already connected / already idle). IP/port are read-only while connected. Selecting
  any device other than the CP540 stops the reader (only one source writes at a time). "Start
  simulator" opens the Simulator in a new tab. A live status pill + raw-line log below the form
  (polling timing:cp540-status) shows the device stream verbatim for debugging. The reader
  reconnects on its own after a drop, so the pill also reads "Reconnecting…"; while it is not
  connected, both timing pages carry a red **device-link banner** (`_device_alarm.html`) saying
  times are not being recorded — the operator is never left timing against a dead link.
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
  completed. Manual times are tinted vs light-barrier ones.
  Hover between the header and the top row for a **+** to pre-enter an upcoming starter (an empty
  placeholder row); incoming starts fill placeholders oldest-first, so times populate bottom-to-top.
  **Double-click** a Start, Finish or Run time (or an empty slot) to type it in by hand when the device
  didn't fire — a keyed-in time is a green "entered" chip (run time green + underlined), distinct from a
  measured one, and the run's total honours it. Ignoring is a **drag** to the Ignored-times panel on the
  right (a shared two-column Start/Finish list, `templates/timing/_ignored.html`, used on both timing
  pages); drag a chip back onto a run's slot (or double-click it) to use it (rejected with a wiggle if it
  would put a start after its finish). A time keyed in over a measured one keeps the measured one on the
  list. Column widths are fixed so entering a bib never shifts them, and the run table + Ignored panel are
  centred on the page. Above the Ignored panel is a red **Lock** switch (`_input_lock.html`, fixed width
  so toggling never resizes it; shared with Auto timing via `timing:input-lock`): while on it sends every
  incoming time straight to the ignore list — a pause without disconnecting — pulsing red and syncing
  across open views over the WebSocket. The page intro moved into a **help pop-up**: the topbar "?"
  (base.html `topbar_actions` block) opens a page-internal modal (`help_modal` block); reusable by any
  page.
- **Auto timing** (`timing/auto/`) — the order-driven live view. The start order (run order × start
  pattern) runs down the left as draggable tiles ("#3 C1"), grouped by run with a "Run · <classes>"
  divider that sticks to the top of the list and is replaced by the next run's as it scrolls up (so the
  header names the run shown at the top, not the running one). Each tile shows the total time (run +
  penalties), and dragging saves a persisted override (Reset order re-derives it). The list follows the
  current starter automatically until the operator scrolls away; a "▲/▼ current" cue re-engages it.
  Incoming device times bind to the order positionally — no bib typing — while a run pre-entered on
  Manual timing claims its own slot (shown pre-filled) and incoming times step over it. The right shows
  the previous / current / next competitor with start, finish, run time and **total time**; **current**
  is the run with the latest timing activity (a fresh finish for an earlier starter surfaces it, not just
  the last to start). Double-click a Start/Finish/Run time here too to key one in by hand (entered times
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
  post shows the read-only breakdown and a **Lock** button. Ignore a wrong time by dragging it to the
  Ignored-times panel (the same two-column Start/Finish list as Manual timing, with the red **Lock**
  switch above it), and drag it back onto a slot to re-pair. A time chip can also be dragged from the
  current competitor onto another tile's Start/Finish slot — including an *upcoming* competitor with no
  run yet (their run is made from the slot key), moving a mis-attributed time onto the right starter.
  The page intro is behind the topbar "?" help pop-up.
- **Marshal Posts** is a top-level sidebar item (below Timing) — the marshal's phone surface (see the
  competitions app). It reads the current competitor from Auto timing over the timing WebSocket and
  pushes every tap and the final submit back (with the per-task detail) so the boxes above fill and go
  green. Once submitted the board shows a 🔒 and locks; a timekeeper Unlock reopens it, and the board
  resumes its exact per-task state for editing. Confirming a post claims it for that device
  (heartbeated); the post shows "— in use" and can't be claimed on another device until released or the
  claim goes stale. A tap the network swallowed is not lost: it waits in a localStorage outbox and
  keeps being retried, the footer says "Sending…" / "not sent yet", and a submit that never reached
  the server is delivered when the page next loads.

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
uv run pytest                            # tests (pytest-django)
```

`runserver` alone drives the whole app, including the Dashboard at `/` and the Manual/Auto timing views
(they read `TimingSignal` -> `TimedRun` and update live over the `timing_live` WebSocket group). The
legacy `run_timing_connector` loop is only needed to feed the *old* `TimingEvent` connector-loop path.

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
