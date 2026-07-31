# Open items — Slalom Timing

A fresh pass over the whole project on the `audit` branch, **2026-07-31**, after the
previous round's findings were closed and its paperwork removed.

Tick the ones you want fixed. Nothing here has been changed yet.

**State of the tree:** 1392 tests pass. `manage.py check --deploy` with `DEBUG=False`
is clean. No missing migrations. Both translation catalogs are complete (0 untranslated,
0 fuzzy) and in sync with the code.

---

## 1. New this round

### [ ] N-1 — Deleting the running event tells nobody it happened  · moderate · small fix

`apps/competitions/views.py:542` (`CompetitionDeleteView`)

Switching the active competition is treated as the shared, installation-wide act it is:
`select_competition()` refuses without `confirm_switch`, names every other person signed
in, and announces the new event over the timing WebSocket so open screens re-render on
purpose instead of by surprise.

**Deleting** that same competition — which is strictly more destructive and cannot be
undone — does none of those three things.

Verified: after a POST to `competitions:delete` on the active competition,
`Competition.objects.filter(is_active=True).count()` is **0** and
`notify_competition_changed` was **never called**. The confirmation page does not name
other signed-in users (`select_competition`'s does).

What that looks like at a venue: every Manual timing, Auto timing, Marshal Posts and
Dashboard screen silently becomes "no competition selected", mid-event, with no
explanation on any of them. The marshals' phones stop working and nobody is told why.

The page already spells out the *data* it takes with it (entries, classes, signals,
recorded runs) — that half is good. It is the *live* consequence that is unsaid.

**Suggested fix:** give delete the same three guards select already has — name other
signed-in users on the confirmation page, require an explicit confirm when the target is
the active competition, and broadcast the change so open views say "the event you were
on has gone" rather than going blank.

---

### [ ] N-2 — A large import review dies with a bare 400  · moderate · one-line fix

`templates/transfer/import_review.html:32`, `config/settings.py`

The review form is `<form method="post">` with no `enctype`, so a browser sends it
**urlencoded** — and urlencoded POSTs are subject to `DATA_UPLOAD_MAX_NUMBER_FIELDS`,
which this project never sets, so it is Django's default of **1000**.

The form posts one `choice-<ref>` per conflicting participant plus one
`field-<ref>-<pk>-<name>` per differing field per candidate. These are radio groups, so
every one of them submits a value. With 13 comparable participant fields
(`schema.PARTICIPANT_FIELDS`) the ceiling is:

| differing fields | candidates | fields per conflict | conflicts before a 400 |
| --- | --- | --- | --- |
| 6 | 1 | 7 | ~142 |
| 13 | 1 | 14 | ~71 |
| 13 | 2 | 27 | ~37 |

Verified end to end: 200 participants differing in 6 fields each renders **1400** inputs,
and an urlencoded POST of 1401 fields returns **400 Bad Request**.

That is the merge wizard's main use case — re-importing a club's own roster into a system
that already has those people. The operator gets a bare browser 400: no message, no
partial save, and the staged file is gone.

**Suggested fix:** set `DATA_UPLOAD_MAX_NUMBER_FIELDS` explicitly in `config/settings.py`
with a comment naming this form as the reason. A test that renders a review page for a
realistic roster and asserts the input count stays under the setting would stop it
drifting back.

*Not affected:* the Results settings page is the same shape but uses **checkboxes**, which
only submit when ticked — 60 running classes renders 745 inputs and posts far fewer. It
would need ~80 running classes to matter, which is not a club event.

---

### [ ] N-3 — README describes the app it used to be  · low · docs only

`README.md`. Six drifts, all of which mislead a newcomer, since this is the front door:

1. **"Getting started"** tells the reader to run `manage.py run_timing_connector` in a
   second terminal to see live timing, and calls `/` "the live dashboard". That is the
   **legacy** `TimingEvent` connector-loop path. The current path is the Simulator (or the
   CP540 reader) feeding `TimingSignal` → `TimedRun`, watched on Manual/Auto timing.
2. **Step 14** — "Dashboard — watch live timing events resolve to participant names by
   bib". The Dashboard is now the organiser overview: progress ring, per-class board,
   current competitor, headline counts.
3. **Step 1** — "Name, date of birth and licence number are always asked for". Licence is
   `requires_license`, a per-type toggle in `CompetitionType.PARTICIPANT_INFO`. Only name
   and date of birth are always asked.
4. **Step 8** — "per-class **overrides** (a class can inherit General or set its own)".
   Per-class rows hold **additions only**; a class can add columns General doesn't show
   and cannot override or inherit-and-replace.
5. **"Common commands"** lists `run_timing_connector` with no legacy caveat, and does not
   say that `collectstatic` is a prerequisite of `pytest` (CLAUDE.md does — manifest
   static storage means a fresh checkout fails most of the suite without it).
6. **"Stack"** still says "vanilla JS + WebSocket for the live dashboard".

`start.ps1` / `start.bat` (the one-click dev start) are also not mentioned in Getting
started.

---

### [ ] N-4 — A concurrency test is flaky, and flaky is worse than absent  · moderate · two-line fix

`config/hostility_tests.py::TestTwoWritersAtOnce::test_two_writers_on_one_bib_leave_one_entry`

Four threads race to claim bib 7; the test asserts one row survives and that the other
three were **refused**. It counts a refusal only when the thread caught an
`IntegrityError`.

Under SQLite contention the loser does not always find out by hitting the constraint. It
can be turned away by the lock first, and that arrives as `OperationalError: database
table is locked` — which the `except IntegrityError` does not catch, so the thread dies
with an unhandled exception and is never counted.

Measured:

| how it is run | result |
| --- | --- |
| the test alone, 5 runs | 5 passed |
| the whole `TestTwoWritersAtOnce` class, 3 runs | 1 failed, 0 failed, 2 failed |
| the full suite, clean | passed (1392 passed) |

The failure reads `assert 1 == (4 - 1)` with two `OperationalError: database table is
locked` tracebacks above it. It only appears when the preceding test in the class has
left its eight threads' connections contending, which is why a clean full run is green
and running the file on its own is not.

The invariant the test exists for — **exactly one entry survives** — holds every time. It
is the second assertion that is wrong: it assumes the constraint always decides who
loses, when the lock can decide instead. Both outcomes mean the same thing to an
operator: that desk did not get bib 7.

**Suggested fix:** catch `DatabaseError` (the parent of both `IntegrityError` and
`OperationalError`) rather than `IntegrityError` alone, and say in the docstring that
either is a legitimate way to lose the race.

This matters more than its size. A test that fails two runs in three teaches everyone to
re-run the suite instead of reading it, and the next real failure gets the same shrug.

---

## 2. Carried forward — previously decided, listed so they are not re-raised

These are recorded in the code itself. No action unless you want to revisit one.

| | Item | Standing decision |
| --- | --- | --- |
| [ ] | **A finished result still depends on a live row.** `CompetitionType`'s precision, penalty amounts and tie-break are read live by `resultscalc`, and the type is shared by every competition of that discipline — so editing a penalty amount in November re-ranks July's event. | **Deferred.** The proper answer is an *archive* feature that snapshots a competition when it is signed off. Written up in `apps/competitions/models.py`. |
| [ ] | **WebSocket consumers have no per-competition scoping.** They check a page role, but any signed-in holder of a live page sees the nudges for whichever event is active. | Known, deliberate — there is only ever one active event. Noted in CLAUDE.md. |
| [ ] | **No self-service password change.** | **Waived** — accounts are assigned; resets are a superuser's job by design. |
| [ ] | **No retention sweep, no bulk delete, no privacy notice.** | **Waived (2026-07-31).** `Participant.last_used_at` stays recorded, so the sweep is one query away whenever it is wanted. |
| [ ] | **The database is not encrypted at rest.** | **Waived**, mitigated by data-directory permission hardening (`config/datasecurity.py`). A passphrase kept beside the database protects nothing. |
| [ ] | **`timing_unrecorded.log` has no replay path.** | **Waived** — unlikely enough to leave alone. |
| [ ] | **Exports are plaintext `.zip`.** | Accepted for now. |
| [ ] | **A time fired with no competition selected is unrecoverable.** | **Waived** — with no event selected, times are allowed to be lost. |

---

## 3. Checked this round and found healthy — no action

Recorded so the next pass can skip them, or re-check quickly if something changes.

- **Deployment posture.** `check --deploy` with `DEBUG=False`, a real key and a host set is
  clean. HTTPS is the default with one documented way off it; HSTS defaults to a short
  300 s rather than a year.
- **The one open write endpoint.** `timing:signal` authorises itself three ways and no
  more: a device token (constant-time compared, and an empty configured token can never
  match), or a session that holds the Timing page *and* passes CSRF, or — only while
  `DEBUG` is on — anonymously. A deployment refuses an anonymous tokenless post.
- **Injection surface.** No inline `<script>` or `style=` anywhere; CSP is `script-src
  'self'` with no nonce. Every `innerHTML` interpolation in `static/js/` either escapes
  through `escapeHtml()` or interpolates a number. The only `mark_safe` in the project is
  `templatetags/pdf_markup.py`, which sanitises on the way out.
- **Data minimisation on the phone.** `marshal-state` sends a marshal bib, name and club —
  not address, e-mail or phone.
- **Migrations.** `makemigrations --check` reports no changes.
- **Translations.** 657 German strings in `django.po` and 163 in `djangojs.po`, none
  untranslated, none fuzzy, and `makemessages` finds no new strings. (The committed `.po`
  line references were slightly stale; regenerated and recompiled in this branch.)
- **Housekeeping.** No bare `except:`, no `eval`/`exec`/`pickle`, no `TODO`/`FIXME`/`HACK`
  left in application code.
