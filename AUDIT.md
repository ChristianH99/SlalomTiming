# Pre-release audit — Slalom Timing

Branch `audit`, 2026-07-28. Scope: everything that must be settled before this app is
built, installed and served to **multiple users on a local but public network**.

Every finding was verified against the code — most of them by a probe that failed at
the time of writing. The probe suites are committed beside this file:

| File | What it is |
| --- | --- |
| `audit_probe2_test.py` | 39 targeted probes; **30 failed** at audit time, **8 now** |
| `audit_matrix_test.py` | 39 configuration-matrix tests; **2 fail** |
| `audit_measure_test.py` | query/payload measurements (all pass, they print numbers) |

A **failing** probe is a confirmed defect: its assertion states the behaviour the app
*should* have, so a fix flips it to green. Never weaken one to make it pass.

Baseline at audit time: the shipped suite was **603 passed, 0 failed** (8 m 29 s).

**Progress:** §1 (release blockers), §2 (security & access control), §3 (data
integrity) and §5 (performance) are complete. **§4 (privacy) is not** — five of its eight items were
carried by §1–§3, but **PRV-4 (retention and bulk erase) and PRV-5 (a privacy
notice) are untouched**, and PRV-6's waiver contradicts the run-book (DOC-5).
§6–§9 still open, plus PRV-4/PRV-5 and DOC-5.

---

## 0a. Owner decisions (2026-07-28)

Recorded here so a later audit does not re-raise them. **Waived** means "working as
intended, do not change"; **deferred** means "not now, note it".

| ID | Decision |
| --- | --- |
| SEC-K | **Waived.** Accounts are assigned; password changes are a superuser's job by design. |
| INT-1 / UI-1 | **Changed scope.** An empty start pattern is the *default* and is legitimate. Auto timing must then show a message with a link to set the pattern and **no timing UI at all** — not alarm cards. |
| INT-4 | **Waived.** With no competition selected, times are allowed to be lost. |
| INT-5 | **Reduced.** Warn on Create and on Import-with-activate, but let the user continue. |
| INT-6 | **Deferred.** Acceptable for now; a later *archive* feature should snapshot a competition so past results are frozen. |
| INT-12 | **Waived** — calendar-year age is correct. Document it. |
| INT-16 | **Waived** — a deleted participant's rows should show the bib and no name. That is the wanted behaviour. |
| PRV-6 | **Waived** — the operator is not supposed to copy the database anywhere. |
| §4 (privacy) | Plaintext `.zip` exports are fine for now. **New ask:** protect the database itself if that is practical. |
| OPS-4 | **Waived** — replaying `timing_unrecorded.log` is unlikely enough to leave alone. |
| UI-4 | **Changed scope.** Show the last 10 per column with a "show more"; drop the arrival age (duplicate information). No filtering. |
| UI-5 | **Waived.** |
| UI-8 | **Changed scope.** Always prefix "Class" (translated) — drop the conditional in `display_name()`. |
| UI-16 | **Extended.** The unsaved-changes guard must also fire the browser's own dialog when the tab is closed. |
| UI-19 | **Waived** — Ctrl-P is not supported. |

---

## 0. Verdict

**Not ready to expose to multiple users on a shared network.** Six blockers (§1). Beyond
those the app is functionally solid: it survived every combination of precision, scoring
method, penalty mode, assignment method, barrier setup, language and field size I threw
at it (37 of 39 matrix tests pass, including 200 starters at a flat 15 queries per live
refresh).

The two systemic weaknesses are **robustness against malformed input** (any non-numeric
id 500s the timing endpoints) and **defaults chosen for a single-operator laptop** that
become wrong the moment a second person is on the network.

---

## 1. Release blockers

### BLK-0 — The signing key is published in a public repository ✅ FIXED
`config/settings.py`

`SECRET_KEY = 'django-insecure-qvzii-…'` was a literal in a public repo, so every
reader of the repository held the key that signs this app's session cookies. Anyone
who could reach a development server could forge a session for any account,
superuser included. No history rewrite takes a published key back — the only fix is
to stop using it.

Now: each checkout mints its own into `DATA_DIR/.secret_key` (gitignored), so a
fresh clone still runs with no setup but nobody shares a key; a deployment
(`DEBUG=False`) must supply `DJANGO_SECRET_KEY` from the environment and the
generated file is never consulted; the packaged build already generated a
per-installation key on first run and now also **refuses to package** if `DEBUG` is
on, if there is no key, or if a developer's generated key is in the payload
(`build/launcher.py::selftest`, `build/build.ps1`). Three tests hold the line,
including one that fails if a key literal ever reappears in `settings.py`.

---

### BLK-1 — An imported archive plants an executable file on the app's own origin ✅ FIXED
`apps/transfer/importers.py:370`, `apps/results/models.py:169`, `config/urls.py:49`

`_import_results` writes the PDF logo straight from the archive with **no image
validation**: `getattr(layout, f"image_{side}").save(name, ContentFile(content))`. The
filename comes from the document. `/media/` is served by `django.views.static.serve`
with a content type guessed from the extension, and the access-control middleware
exempts it (`apps/accounts/middleware.py:27`).

So importing a crafted `.zip` writes `media/results_logos/<pk>/evil.html` and serves it
as `text/html` from the app's own origin, to anyone, without a login. With no CSP
(SEC-11, still open) that is stored XSS against every operator session.

The interactive upload path is checked (`results/views.py::_clean_logo` → Pillow via
`forms.ImageField`); the import path is not. This is exactly the SEC-1 pattern the
codebase already fixed for the PDF header — "sanitising in one door only" — repeated for
the media door.

*Evidence:* `audit_probe2_test.py::test_an_imported_logo_cannot_plant_an_html_file_in_media` fails.

**Fix:** run imported media through the same `_clean_logo`; ignore the document's
filename and generate your own; serve `/media/` with `Content-Disposition: attachment`
or behind the login gate.

---

### BLK-2 — Any malformed id 500s the timing endpoints ✅ FIXED
`apps/timing/views.py` (all mutate endpoints), `apps/participants/views.py:311,375`,
`apps/timing/runstatus.py:79`

`TimedRun.objects.filter(id=payload.get("run_id"))` with `run_id: "abc"` raises
`ValueError` from Django's int coercion — an unhandled 500. Confirmed on **nine**
endpoints: `run-update`, `run-status`, `ignore`, `pair`, `set-time`, `set-runtime`,
`run-delete`, `auto-adjust`, `marshal-submit`, plus `participants:set-bib`,
`participants:set-dsq` and `_parse_class_key` (`class_key: "abc:0"`).

Two more of the same family:

* `_as_positive_int` / `_as_count` use `str.isdigit()`, which is `True` for `"²"` while
  `int("²")` raises. `{"bib_number": "²"}` → 500.
* `timing:signal` does **not** bound `running_number`. `10**30` reaches
  `PositiveIntegerField` → `OverflowError` → 500. CLAUDE.md claims "numbers keyed in by
  an operator are bounded in `apps/timing/views.py` because SQLite stores out-of-range
  values rather than refusing them" — that is true of the penalty counts and the typed
  run time, and false of the running number.

On a network with marshals' phones, one flaky client turns every tap into a 500. It is
also a free error-page oracle for anyone who reaches the endpoints.

*Evidence:* 15 failing probes under `test_timing_endpoints_reject_a_non_numeric_id_without_500`
and neighbours.

**Fix:** one `_as_pk()` helper used by every endpoint; make `_as_count`/`_as_positive_int`
ASCII-only; bound `running_number` at the signal door.

---

### BLK-3 — Results tables and PDFs publish contact details by default ✅ FIXED
`apps/results/models.py:96-105`

`general_columns()` returns **every available column** when no `ResultColumnSettings`
row is saved. For a type that collects them, a fresh competition's result table — and
the PDF that goes on the notice board — carries every competitor's **e-mail address,
phone number, street and city**.

The organiser has to *notice* and turn them off. For a German club event that is a
straightforward GDPR problem, and the result sheet is the one artefact that leaves the
building.

*Evidence:* `test_results_columns_do_not_default_to_showing_contact_details` fails.

**Fix:** default to `driver_name`, `club`, `birth_year` (+ `training`). Everything else
opt-in. Consider making the *export* column set separate from the *screen* one.

---

### BLK-4 — Content-Disposition is built by raw interpolation (SEC-6 is **not** fixed) ✅ FIXED
`apps/results/views.py:395`, `apps/transfer/views.py:68`

```python
response["Content-Disposition"] = f'inline; filename="{filename}"'
```

with `filename = f"results-{cclass.name}.pdf"`. Measured output for a class named
`A"; attachment; filename="evil.pdf`:

```
inline; filename="results-A"; attachment; filename="evil.pdf.pdf"
```

The header is split apart exactly as described in the finding CLAUDE.md:611 says was
closed with `filename*=UTF-8''<percent-encoded>`. It was not. Two further consequences:

* a class named in Cyrillic → Django RFC-2047-encodes the **whole** header
  (`=?utf-8?b?...?=`), which no browser parses as a Content-Disposition;
* a class name containing a newline → `BadHeaderError` → **HTTP 500**.

`transfer/exporters.py::filename` sanitises to `isalnum() or "-_"`, which is
Unicode-aware, so it has the same non-latin-1 problem. CLAUDE.md:721 claims it is
"percent-encoded, the pattern results/views.py copies" — neither half is true.

**Fix:** `django.utils.http.content_disposition_header()`.

---

### BLK-5 — No logging configuration at all ✅ FIXED
`config/settings.py` (no `LOGGING` key)

`apps/accounts/throttle.py` logs every failed login at WARNING and every **successful**
login at INFO, and its module docstring says "Every failure is logged … SEC-4 was as
much about having nothing to look at afterwards". But the project configures no
`LOGGING`, so:

* the root logger is unconfigured → Python's `lastResort` handler emits **WARNING and
  above only** → *successful logins are never recorded anywhere*;
* nothing is written to a **file**. On the packaged Windows build the server runs from
  `launcher.py` and stderr goes to a console window that is closed at the end of the
  day. The security log does not survive the event.
* `ingest._capture_unrecorded` logs "signal could not be stored" the same way.

*Evidence:* `test_the_project_configures_logging_to_a_file` fails.

**Fix:** a `LOGGING` dict with a rotating file handler under `DATA_DIR`; INFO for
`apps.accounts.login` and `apps.timing`.

---

### BLK-6 — Changing a competition's type deletes registrations with no confirmation ✅ FIXED
`apps/competitions/views.py:85-95`, `_clear_foreign_registrations`

`GeneralView.post` detects a changed `competition_type` and immediately
`entries.delete()` + `ClassAssignment...delete()`, reporting afterwards: *"Removed N
registration(s) that didn't belong to …"*. Every other destructive action in this app
confirms first (`PenaltiesView._penalties_lost`, `bibs.bib_change_effect`,
`CompetitionDeleteView`'s counts, `select_competition`'s `confirm_switch`). This one
does not, and the dropdown sits on the most-visited setup page.

The recorded `TimedRun` rows survive but lose their competitor: their `bib_number` no
longer resolves to an `EventEntry`.

*Evidence:* `test_changing_the_competition_type_asks_before_deleting_registrations` fails.

**Fix:** count first, refuse without `confirm_type_change`, same shape as
`PenaltiesView`.

---

## 2. Security and access control

Access control itself is **sound** — I could not get past it. A Results-only role is
refused the timing arrangement, a Marshal-only role is refused `marshal-lock`, a
Participants-only role is refused `timing:signal`, and a non-superuser is refused the
accounts endpoints (4 probes, all pass). Every URL in the resolver is either mapped to a
page, superuser-gated, or deliberately open. Below are the gaps around it.

| ID | Sev | Finding |
| --- | --- | --- |
| SEC-A | ✅ FIXED | **`/media/` was world-readable.** `middleware.py:27` returns early for `MEDIA_URL`, before the authentication check. Verified: an anonymous `GET /media/audit-leak.txt` returns **200 with the file body**. `config/tests.py:68` currently *asserts* this behaviour, so it reads as deliberate — but the app's premise is "login required everywhere", and this is the one route by which uploaded content leaves it. It is also what makes BLK-1 exploitable unauthenticated. |
| SEC-B | ✅ FIXED | **`timing:signal` is `csrf_exempt` and accepts session auth.** A cross-origin form can post a JSON body; the only thing stopping it is Django's default `SameSite=Lax` session cookie. Nothing in the code states that dependency. Split the door: token-only for devices, CSRF-protected for the browser Simulator. |
| SEC-C | ✅ FIXED | **SEC-7 confirmed open — `participants:check` leaks across competition types.** `Participant.objects.all()` is searched by licence number and name; the response carries **name, club and licence number** of participants registered under *other* disciplines. Any user with the Participants page gets an unthrottled licence-number oracle. *Evidence:* `test_duplicate_check_does_not_leak_participants_of_another_type` fails. |
| SEC-D | ✅ FIXED | **The WebSocket has no origin check.** `config/asgi.py:43` wraps the router in `AuthMiddlewareStack` but not `AllowedHostsOriginValidator`. Browsers do not apply same-origin policy to WebSockets, so any page an operator visits can open `ws://…/ws/timing/live/` with their cookies and read the `competition` nudge (the event's name) and see when times land. |
| SEC-E | ✅ FIXED | **SEC-12 confirmed open** — `consumers.py:9` checks `is_authenticated` only, no page role. Correctly listed as open in CLAUDE.md. |
| SEC-F | ✅ FIXED | **Login throttling has no per-IP cap.** `throttle._key` is `(username, IP)`, so one host can spray *unlimited* usernames at 10 attempts each without ever being blocked, and account enumeration is unlimited. Add a second counter on IP alone. |
| SEC-G | ✅ FIXED | **`marshal_submit` stores unbounded JSON.** `detail` goes into a `JSONField` with no size or shape check. A 1.19 MB blob was stored from one request and is then re-serialised into `auto-state` for every open browser on every nudge. *Evidence:* `test_marshal_detail_json_is_size_bounded` fails. |
| SEC-H | ✅ FIXED | **SEC-11 confirmed open — no CSP**, and `base.html` carries ~130 lines of **inline `<script>`** in four IIFEs, plus `json_script` blocks. Adding a CSP is therefore not a one-line change; move that JS to `static/js/` first. |
| SEC-I | ✅ FIXED | **SEC-9 confirmed open — no audit trail.** Nobody can answer "who changed this result?" A timekeeper, a marshal and an organiser all write to the same rows. On a multi-user network this is the difference between a protest you can settle and one you cannot. |
| SEC-J | ✅ FIXED | `marshal_release` compares the claim token with `==`, not `secrets.compare_digest` (`views.py:491`) — inconsistent with `_marshal_may_write`, which does. |
| SEC-K | ⊘ WAIVED | **No self-service password change.** Only a superuser can reset a password, from the User Access page. An operator whose password was set on race morning by someone else cannot change it. |
| SEC-L | ✅ FIXED | A superuser resetting **their own** password is logged out immediately — `user_update` calls `set_password`/`save` without `update_session_auth_hash`. |
| SEC-M | ✅ FIXED | **No account deactivation.** `is_active` is never exposed; the only way to remove access is to delete the user. |
| SEC-N | ✅ FIXED | `_superuser_required` redirects an authenticated non-superuser to the **login page** rather than returning 403 — a confusing loop. (The middleware already 403s the pages, so this only affects the POST endpoints.) |
| SEC-O | ✅ FIXED | `safe_next` permits an absolute `http://<same-host>/…`, i.e. a protocol downgrade, because `url_has_allowed_host_and_scheme` is called without `require_https`. |
| SEC-P | ✅ FIXED | Sessions are never pruned. Nothing runs `manage.py clearsessions`, it is not in DEPLOYMENT.md, and `other_signed_in_users` scans that table on every competition switch. Expired session rows are also retained personal data. |
| SEC-Q | ✅ FIXED | The CP540 reader appends to `buffer` with no line-length cap (`cp540.py:228`). A device that never sends `\n` grows it unbounded. We dial out to a configured address, so the trust level is moderate — but a venue LAN is not trusted. |

---

## 3. Data integrity and timing correctness

| ID | Sev | Finding |
| --- | --- | --- |
| INT-1 | ✅ FIXED | **A missing start pattern silently breaks Auto timing with no diagnostic.** Verified on the real development database: `RRR Seifenkistenrennen 2026` has `start_pattern == []`, so `computed_slots()` returns **0** and all 70 started runs become orphans. The Auto timing page renders as **70 consecutive flame-bordered "Nicht zugeordnete Zeit" alarm cards** and never says why — because `_empty_reason()` (`autotiming.py:314`) is only consulted `if not items`, and with orphans present `items` is non-empty. Meanwhile the Manual view shows the same event perfectly. `Competition.save()` seeds `DEFAULT_BLOCKS` only for **new** competitions and migration `0011_competition_start_pattern` has **no backfill**, so every pre-existing competition is in this state. |
| INT-2 | ✅ FIXED | **Nothing enforces one active competition.** `get_current()` is `filter(is_active=True).first()` — ordered by `-date`. Two rows with `is_active=True` (via the admin, a bad import, an interrupted transaction) means the app silently serves one of them and the operator has no way to see the conflict. Needs a `UniqueConstraint(fields=["is_active"], condition=Q(is_active=True))`. *Evidence:* `test_only_one_competition_can_be_active_at_a_time` fails as written (it asserts the invariant, which is unenforced). |
| INT-3 | ✅ FIXED | **A run that crosses midnight produces no time.** `calc.run_time` subtracts seconds-since-midnight, so 23:59:59 → 00:00:02 is negative and returns `None`. The same applies to a CP540 whose internal clock wraps at 24 h (`cp540.parse_time` explicitly wraps). The run silently shows blank instead of 3.000 s. *Evidence:* `test_a_run_that_crosses_midnight_still_produces_a_time` fails. |
| INT-4 | ⊘ WAIVED | **A time fired with no active competition is unrecoverable.** `record_signal` stores it with `competition=None`, never places it, never broadcasts. `reconcile()` is scoped to a competition, so it never picks it up. The time is in the database and no screen in the app can ever show it. Contradicts the module's own "a time is never allowed to vanish". *Evidence:* `test_a_signal_arriving_with_no_active_competition_is_still_reachable` fails. |
| INT-5 | ✅ FIXED | **Creating a competition silently steals the active one.** `CompetitionCreateView.form_valid` runs `Competition.objects.exclude(...).update(is_active=False)` with no `other_signed_in_users` check — while `select_competition` goes to real trouble to confirm exactly that. Creating next month's event mid-race moves every open screen. The same applies to **import with "activate"** (`transfer/views.py:199`). *Evidence:* `test_creating_a_competition_does_not_silently_steal_the_active_one` fails. |
| INT-6 | ◔ NOTED | **Changing a `CompetitionType` retroactively rewrites finished results.** `timing_precision`, the four penalty amounts and `tie_break` are read live by `resultscalc`, and the type is shared by every competition of that discipline. Editing a penalty amount in November re-ranks July's event. No warning, no versioning. |
| INT-7 | ✅ FIXED | **Bib assignment has a read-then-write race.** Both the form and `participant_set_bib` check `conflict.exists()` and then insert. Two registration desks on one bib → `IntegrityError` → 500, not a friendly "already taken". Catch it. |
| INT-8 | ✅ FIXED | **`TimedRun.start_signal`/`finish_signal` are `SET_NULL`.** Deleting a `TimingSignal` (available in the Django admin) silently blanks a run's time rather than refusing. For a system whose stated rule is "this app does not delete recorded times", `PROTECT` is the right choice. |
| INT-9 | ✅ FIXED | **`format_clock` rounds.** `f"{(v - whole):.{precision}f}"` on `Decimal("12.9996")` at precision 3 gives `00:12.999`… actually `00:13.000` — it rounds *up*, contradicting "truncated, never rounded". It is documented as "assumed already truncated", but it is also handed `score - first_score` (`views.py:202`) and `rt + penalty`. One un-truncated caller and a time reads a millisecond fast. *Evidence:* `test_format_clock_never_rounds_a_time_up` fails. |
| INT-10 | ✅ FIXED | **Date of birth is unbounded.** A participant born in the year 3000 saves fine and gives every age-based class a negative age. *Evidence:* `test_a_far_future_and_far_past_date_of_birth_are_refused` fails. |
| INT-11 | ✅ FIXED | **Overlapping age ranges resolve arbitrarily.** `class_for_birth_year` returns the *first* running class whose range matches, in `(position, name)` order, with no warning that two classes overlap or that a range gap leaves competitors unassigned. |
| INT-12 | ✅ FIXED | Age is computed as `competition.date.year - birth_year` — calendar-year age, not age on the day. Legitimate, but undocumented and invisible to the organiser. |
| INT-13 | ✅ FIXED | `auto_reorder` accepts duplicate slot keys (it filters against `known` but not for repeats) and has no length cap. |
| INT-14 | ✅ FIXED | `_import_results` creates `ResultColumnSettings` rows without dedup: a document with two General rows raises `IntegrityError` inside the import transaction instead of a `TransferError`. Same class of problem for a traversing media filename, which raises `SuspiciousFileOperation`. *Evidence:* two failing probes. |
| INT-15 | ✅ FIXED | `NumberedCanvas._page_x` is a **class attribute** mutated per export (`pdf.py:638`). Two concurrent PDF exports at different orientations race over the page-number position. |
| INT-16 | ⊘ WAIVED | Deleting a `Participant` leaves their runs orphaned by design (the confirm page says so) — but the results table then shows a blank name for a ranked competitor rather than anything explanatory. |

---

## 4. Privacy

The system holds name, date of birth, address, e-mail, phone, club, licence number and
vehicle for every competitor, retained across events, and exports the lot in a plaintext
`.zip`.

* **PRV-1 (✅ FIXED, §1)** — BLK-3: contact details were on by default in the
  published results.
* **PRV-2 (✅ FIXED, §1)** — SEC-A: uploaded media was world-readable.
* **PRV-3 (✅ FIXED, §2a)** — SEC-C: the duplicate check leaked across disciplines.
* **PRV-4 (OPEN)** — **No retention policy and no bulk erase.** Participants persist forever;
  there is no "delete everyone from before year X", and no way to honour an erasure
  request other than deleting one participant at a time (which silently blanks their
  name in past results).
* **PRV-5** — **No privacy notice anywhere** in the UI, and no record of consent. For a
  German club running this on a public network, that is the paperwork side of the same
  gap.
* **PRV-6** — **The database is unencrypted** and the packaged build puts it in
  `%LOCALAPPDATA%`. DEPLOYMENT.md tells the operator to copy `db.sqlite3` to a USB
  stick, which is the right advice and also an unencrypted copy of everyone's address.
  Worth one sentence in the run-book.
* **PRV-7** — SEC-P: expired sessions are retained indefinitely.

---

## 5. Performance

Measured (`audit_measure_test.py`), on top of the numbers already in CLAUDE.md:

| Scenario | Before | After |
| --- | --- | --- |
| `timing:auto-state`, **manual** assignment | 15 flat | **15 flat** |
| `timing:auto-state`, **age-based** assignment | 34 / 64 / 114 at 20 / 50 / 100 starters | **14 flat** |
| `results:export-all`, 30 starters | 42 / 80 / 137 at 1 / 3 / 6 classes | **24 / 32 / 44** |
| `timing:auto-state` payload, 200 starters, no posts | 389 KiB | **259 KiB** |
| `timing:auto-state` payload, 200 starters, **4 marshal posts** | 1341 KiB | **260 KiB** |
| `timing:dashboard-state` | 22 | **22** |
| `timing:arrangement` | 15 | **15** |

* **PRF-1 (✅ FIXED)** — **The documented "cost must not grow with the field" rule holds only
  for Manual assignment.** `AgeAssignment.classes_for` → `Competition.class_for_birth_year`
  → `self.classes.filter(is_running=True)`, called once **per participant** inside
  `starters_by_class()`. That is exactly `N + 14` queries, re-paid by every open browser
  on every incoming time. `test_live_endpoint_cost_does_not_grow_with_the_field` only
  exercises Manual, so nothing catches it.
* **PRF-2 (✅ FIXED)** — **`results:export-all` rebuilds `RunIndex` once per section**
  (~19 queries/class). The whole point of `RunIndex` was to read the event's runs once;
  "export everything" still reads them once per class.
* **PRF-3 (✅ FIXED)** — **`link_state()` copies the entire 400-entry CP540 ring buffer** on
  every live refresh (`cp540.snapshot()` → `list(self._log)`), to read two fields. Both
  timing pages call it on every nudge.
* **PRF-4 (✅ FIXED)** — `arrangement.reconcile()` runs on **every incoming signal** and
  builds an `id__in` list of every placed signal (2 × run count). At 600 runs that is a
  1200-parameter `IN` clause on the timing thread, per signal.
* **PRF-5 (✅ FIXED)** — `snapshot()` does `list(deque)` while the reader thread appends —
  `RuntimeError: deque mutated during iteration` is possible on the settings poll.
* **PRF-6 (✅ FIXED)** — `autotiming.marshal_state` calls `current_run()`, which re-runs
  `apply_bindings` over the whole event, for every marshal phone poll.
* **PRF-7 (✅ FIXED)** — PRF-6 from CLAUDE.md (payload size) confirmed: nudges carry no
  payload, so every client re-downloads the full `auto-state`. Still the next lever.

---

## 6. Operability / deployment

DEPLOYMENT.md is genuinely good — the TLS decision, the single-process rule, the race-day
checklist and the troubleshooting table are all there. What is missing:

* **OPS-1 (High)** — **No automatic backup.** The run-book says so out loud ("There is no
  automatic backup yet") and offers a manual `VACUUM INTO` between runs. For a
  multi-user event where the database *is* the event, a scheduled `VACUUM INTO` on a
  timer is a small feature and the difference between a hiccup and a lost day.
* **OPS-2 (✅ FIXED)** — BLK-5 gave the app a log file; §2c gave it an audit trail
  beside it, and both are named in the race-day checklist.
* **OPS-3 (Med)** — **No health endpoint.** Nothing to point a check at; "is it up" means
  loading a page.
* **OPS-4 (Med)** — **`timing_unrecorded.log` has no replay path.** The run-book says the
  time "needs entering by hand". A `manage.py replay_unrecorded` would close the loop.
* **OPS-5 (Med)** — **No `clearsessions`** in the release or race-day steps (SEC-P).
* **OPS-6 (Low)** — DEPLOYMENT.md says `timing_unrecorded.log` is "in the project root";
  it is in `DATA_DIR`, which is only the project root for a checkout — not for the
  packaged Windows install the same document describes.
* **OPS-7 (Low)** — `.env.example` documents no logging and no backup variables.
* **OPS-8 (Low)** — `ALLOWED_HOSTS` is described as "required (non-empty) once DEBUG is
  off" but nothing checks it; the failure surfaces as `DisallowedHost` on every request
  instead of at startup, unlike the secret-key check right above it.

---

## 7. UI / UX / layout

The design system is real and mostly held to: **zero raw hex colours** outside the token
block, one inline `style=` in the whole template tree, no `<style>` blocks, a skip link,
`prefers-reduced-motion` respected, and `--space-*`/`--text-*` used almost everywhere.
What follows is what is left.

### 7.1 Broken or misleading states

* **UI-1 (High)** — **The Auto timing page in its most common failure mode is 100 %
  alarm.** With no start pattern (INT-1), every tile and both competitor panes render as
  flame-bordered "Unattributed time" cards, each repeating the same two-line instruction
  paragraph verbatim, with an empty "Next up" card below. An alarm that fires on every
  row is not an alarm. The page needs to detect "no slots at all" and show
  `_empty_reason()` instead — and the instruction paragraph belongs once, above the
  list, not in each card.
* **UI-2 (High)** — **The Manual timing table is a wall of amber.** A status tint is
  applied as a full-bleed row fill; with 15 consecutive DNS rows the table is unreadable
  and ordinary rows would be invisible among them. Make the tint a left border or tint
  only the Status cell.
* **UI-3 (Med)** — **The Status column and the Total column both read "DNS"** on every
  such row — the same fact twice, side by side, in a table where horizontal space is the
  scarce resource.
* **UI-4 (Med)** — **The Ignored-times rail is unusable once it fills.** The real
  database has **230 ignored times**, aged "42 Std." / "43 Std.", from previous
  sessions. The documented decision not to offer a clear-all is right (they are the only
  record the device fired), but the rail then needs age filtering, a
  "today only" default, or an archive fold — not a 230-item list beside a live timing
  table.
* **UI-5 (Med)** — The rail's **Start / Finish columns render as two independent lists
  side by side**, so unrelated entries line up and read as pairs.
* **UI-6 (Low)** — `#?` is still the bib placeholder on an unattributed tile, despite
  CLAUDE.md describing it as replaced.
* **UI-7 (Low)** — On the Dashboard, the current competitor's **total time is rendered in
  the flame colour**, which everywhere else in this app (including `pdf._PEN`) means
  *penalty*. A clean run's total reads as penalised.

### 7.2 Consistency

* **UI-8 (Med)** — **The sidebar names classes bare** (`{{ cc.name }}` → "1", "2",
  "Bobbycar Mini") while the page it links to is titled "Ergebnis Klasse Bobbycar Mini".
  The documented rule is that `display_name()` is the one way a class is written where
  it has to be named as a class. `base.html:65` is the exception.
* **UI-9 (Med)** — **Focus rings are removed on most inputs.** There is a correct global
  `:focus-visible { outline: 2px solid var(--flame) }` at line 1406 — and then
  `form input:focus, form select:focus, form textarea:focus { outline: none }` at 879,
  plus five more component-level `outline: none`s. Keyboard users get a border-colour
  change only. WCAG 2.4.7 / 2.4.11.
* **UI-10 (Med)** — **`z-index` is not tokenised** and the ladder is ad-hoc: 3, 5, 10,
  20, 30, 40, 199, 200, 200, 300, 1000 — with **200 used twice** by unrelated components
  (sidebar and one other). The stylesheet's own rule is "a value that appears twice is a
  token".
* **UI-11 (Low)** — Same for **transition durations** (0.05 / 0.06 / 0.15 / 0.2 / 0.25 /
  0.6 s, several repeated) and **breakpoints** (480 / 700 / 820 / 900 / 1100 px).
* **UI-12 (Low)** — The sidebar width `240px` is hard-coded at `main.css:167` and again
  at `:269`. Should be `--sidebar-w`.
* **UI-13 (Low)** — Seven remaining raw lengths outside the scale: `main.css:1072-1076`
  (4px / 3px / -1px chevron nudges), `:1495` (1.6rem), `:1905` (15rem), `:2378` (1rem),
  `:3500` (`font-size: 0.85em`).

### 7.3 Interaction and accessibility

* **UI-14 (Med)** — **No modal focus management.** Both `base.html` dialogs (and the help
  modals) are `role="dialog" aria-modal="true"` but nothing moves focus in, traps it, or
  restores it on close. Tab goes straight behind the overlay.
* **UI-15 (Med)** — **Django messages are not announced.** `base.html:118` renders them
  as a plain `<ul>` with no `role="status"` / `aria-live`, so "Timing settings saved"
  never reaches a screen reader. They also have no dismiss control.
* **UI-16 (Med)** — **The unsaved-changes guard covers only in-app link clicks.** Closing
  the tab, pressing Back, or an external link all discard silently — no `beforeunload`,
  no `popstate`.
* **UI-17 (Med)** — The guard's **"Save changes" calls `form.submit()`**, which bypasses
  HTML5 constraint validation and any `submit` listener. An invalid form posts. Should be
  `requestSubmit()`.
* **UI-30 (✅ FIXED)** — *(reported after §2b, pre-existing)* **The Manual timing
  table sat in a band of empty white.** `.timing-live-main` is `flex: 0 1 auto`,
  so it sized to its *widest* child — and that was the time legend below the
  table, whose three items on one line came to 1174px against a 1063px table. The
  card therefore ran 111px past its own last column. Verified pre-existing by
  measuring the same numbers on the pre-§2b code. The column now takes its width
  from the table (`width: max-content`) and the two full-width strips below it
  (legend, barrier phase) are taken out of the intrinsic measurement, so the
  legend wraps to the table instead of stretching it.
* **UI-18 (Med)** — **The Manual timing layout never stacks.** `.timing-live-body` is a
  plain flex row with no `flex-wrap` and no breakpoint, while its sibling `.auto-layout`
  stacks at ≤1100 px. On a tablet at the finish line the Ignored rail crushes the table.
* **UI-19 (Low)** — **No `@media print`** anywhere. Printing a results page from the
  browser prints the sidebar. (The PDF export is the intended path, but people press
  Ctrl-P.)
* **UI-20 (Low)** — **The sidebar collapse state is not persisted** — it resets on every
  navigation, so an operator who wants the full width has to collapse it on every page.
* **UI-21 (Low)** — `aria-expanded="true"` is hard-coded on the hamburger in the HTML,
  so the server-rendered state is wrong on mobile until `syncAria()` runs.
* **UI-22 (Low)** — `<tr role="button">` on the participant row (`participant_list.html:53`)
  overrides the row semantics for screen readers.
* **UI-23 (Low)** — The **`+` strip to pre-enter a starter** on Manual timing is
  hover-only with no persistent affordance — undiscoverable, and unreachable by touch.
* **UI-24 (Low)** — On the results table, a DNS on a **training** run displays "DNS" while
  the tally below reads "DNS: 0". Both are correct (a practice state code has no bearing
  on the result) and together they look like a bug.
* **UI-25 (Low)** — "PDF exportieren" is a filled flame primary button, louder than the
  result it sits above — the same critique the codebase already applied to Auto timing's
  "Reset order".
* **UI-26 (Low)** — The Dashboard's class board leaves a ragged gap when the class count
  is not a multiple of three.
* **UI-27 (✅ FIXED)** — `base.html` carried ~130 lines of inline JS in four IIFEs
  while every other script lived in `static/js/`. Also the blocker for SEC-H; both
  went together in §2b.
* **UI-28 (Low)** — *(found during §2b)* The participant form shows German page
  furniture around **English field labels** ("First name", "Last name", "Date of
  birth"): those come from `Participant`'s field names, which carry no
  `verbose_name`, so there is nothing for the catalogue to translate. Every other
  label on the page is translated, which makes it read as a half-finished form.
* **UI-29 (✅ FIXED)** — *(found during §2b)* `gettext("a" + "b")` in
  `auto_timing.js` put `"a"` in the catalogue while the browser looked up `"ab"`,
  so the "unattributed time" sentence had been rendering in English in a German
  UI. It is the JS twin of the `_("…")`-inside-an-f-string trap CLAUDE.md already
  warns about, and nothing checks for either.

---

## 8. Documentation vs code

CLAUDE.md is unusually accurate — I checked its claims about `_RowContext`, `RunIndex`,
`sync_bindings`, `live_socket.js` exclusivity, the multi-line `{# #}` check, the
`format_clock` rule and the open SEC items, and all of those hold, with the tests it
names actually present. Four things are wrong:

| ID | Where | Claim | Reality |
| --- | --- | --- | --- |
| DOC-1 | CLAUDE.md:611 | The PDF filename "goes out as `filename*=UTF-8''<percent-encoded>` … (SEC-6)" | It is raw f-string interpolation and demonstrably splits the header (BLK-4). |
| DOC-2 | CLAUDE.md:721 | `transfer.filename()` is "percent-encoded, the pattern results/views.py copies" | It is a character-substitution sanitiser, and there is no percent-encoding in either place. |
| DOC-3 | `competitions/views.py:651` | `MarshalPostsView`: "Submitting is a no-op stub for now — the transmission back into the system is a later feature." | Stale by several features. `marshal_posts.js` posts to `timing:marshal-submit` with a retrying outbox. |
| DOC-5 | DEPLOYMENT.md §5 vs the PRV-6 waiver | The run-book tells the operator to copy `db.sqlite3` to a USB stick | The owner's position is that the database is not to be copied anywhere. That instruction is also the app's only backup (OPS-1), so resolving this means deciding what the backup *is*. |
| DOC-4 | CLAUDE.md "Security", `views.py:29-47` | "Every value that arrives in a file is hostile until checked"; "numbers keyed in by an operator are bounded" | Imported media is not checked at all (BLK-1); `running_number` is not bounded (BLK-2). |

Also: DEPLOYMENT.md's `timing_unrecorded.log` location (OPS-6), and the CLAUDE.md
performance table's "cost does not grow with the field" which is Manual-assignment-only
(PRF-1).

---

## 9. Test-coverage gaps

The 603-test suite is strong on behaviour and weak on hostility. What it does not do:

* **TST-1** — **No malformed-input tests.** Nothing posts a non-numeric id, a Unicode
  digit, an over-long integer or a wrong-typed JSON value at any endpoint. That is the
  whole of BLK-2.
* **TST-2** — **Role scoping is barely tested.** The `client` fixture is a superuser
  (`conftest.py`), so almost every view test proves nothing about access control. The
  four probes I wrote are the shape needed, per shared/open endpoint.
* **TST-3** — **The performance tests only cover Manual assignment** (PRF-1) and do not
  cover `export-all` (PRF-2).
* **TST-4** — **No test crosses midnight** (INT-3), and none exercises the device-clock
  wrap that `cp540.parse_time` explicitly implements.
* **TST-5** — **No test for an empty start pattern with recorded runs** (INT-1) — the
  state the real database is in.
* **TST-6** — **No concurrency tests**: two writers on one bib, two PDF exports, a GET
  racing the reader thread.
* **TST-7** — **No import-hostility tests**: crafted media, duplicate rows, traversing
  names. `apps/transfer/tests.py` tests the happy path and the archive budgets.
* **TST-8** — **No accessibility assertions** (focus order, aria-live, labels).
* **TST-10** — *(found during §2b)* Nothing checks that a `static/js` file
  parses. Moving 1,778 lines out of templates broke two files with an escaped
  quote where a string should open; the server was perfectly happy, the page
  rendered, and the script was simply dead. `config/tests.py` now has a cheap
  heuristic for that exact signature — a real JS parse in CI would be better.
* **TST-9** — Nothing asserts the design-system rules the CSS comment states (no raw
  colour / length outside the token block) — those would be cheap file tests, and
  UI-10..13 are what slipped through.

---

## 10. Suggested order

**Before any build:** BLK-0 … BLK-6 — **done**, see the ✅ marks above (SEC-A came
with BLK-1).

**Before more than one user is on the network:** SEC-A, SEC-B, SEC-C, SEC-F, SEC-G,
INT-1, INT-2, INT-5, OPS-1, OPS-2.

**Before an event of any size:** INT-3, INT-4, INT-7, PRF-1, PRF-2, UI-1, UI-2, UI-4.

**Then:** the rest of §3–§7, DOC-1..4, and TST-1/TST-2 as the two suites that would have
caught most of the above.
