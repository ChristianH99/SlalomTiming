# The test suite — what is in it, and what is worth keeping

A review of every test in the project, **2026-07-31**. Written because the suite has grown
to a number that looks alarming, and the first thing worth saying is that the number is
misleading.

**1392 test cases, from 671 test functions, in 9 files, in ~20 minutes.**

---

## 1. Why 1392 is not 1392 things

540 of those cases are one function run many times over a list. Six functions account for
nearly a third of the whole suite:

| Expanded into | Test |
| ---: | --- |
| 210 | `hostility_tests` — a hostile payload is refused, not crashed (10 values × 21 discovered endpoints) |
| 47 × 6 | `config/tests` — six file checks, each run over all 47 templates (inline script, inline style, event handlers, multi-line `{# #}`, dialog labelling, control naming) |
| 28 | `config/tests` — every `.js` file is structurally whole |
| 21 × 3 | `hostility_tests` — empty body / non-JSON body / JSON scalar, over the discovered endpoints |
| 17 | `config/tests` — each page marks exactly its own sidebar entry |

These are the cheapest tests in the project — most read a file and never touch the
database — and they are the ones with the best failure story: they catch a *class* of bug
rather than an instance. The whole point of the endpoint-discovery ones is that they find
their own targets from the URLconf, so an endpoint added next month is covered the day it
is added.

**So do not judge this suite by its case count.** Judge it by wall-clock time and by how
many of those 671 functions earn their keep.

---

## 2. Where the suite actually is

| File | Cases | Functions | Subject |
| --- | ---: | ---: | --- |
| `config/tests.py` | 459 | 94 | Deployment (things that only break with `DEBUG` off), plus the cross-cutting file checks: CSP, design-system scales, keyboard focus, dialogs, the sidebar registry, JS structure |
| `config/hostility_tests.py` | 277 | 8 | What happens when a client is unkind. Discovers every JSON endpoint from the URLconf and asks each the same hostile questions. Plus the threaded concurrency tests |
| `apps/timing/tests.py` | 201 | 168 | Signal → arrangement → run, both timing views, CP540, marshal claims, live cost ceilings |
| `apps/competitions/tests.py` | 128 | 111 | Competition/type/class models, setup pages, start patterns, marshal-post setup |
| `apps/transfer/tests.py` | 118 | 118 | Export/import archives, the merge wizard, CSV import, automatic backup, folder picker |
| `apps/participants/tests.py` | 74 | 64 | Participant CRUD, bib rules, duplicate check, type-driven fields |
| `apps/results/tests.py` | 65 | 56 | Scoring, ranking, ties, state codes, columns, the PDF |
| `config/matrix_tests.py` | 39 | 21 | The configuration space: precision × scoring × penalty mode × assignment × barrier × language × field size |
| `apps/accounts/tests.py` | 31 | 31 | Page registry, gating, login throttle, account management, audit trail |

---

## 3. Where the 20 minutes actually goes

The slowest 45 tests total under a minute between them. There is **no hotspot** — the time
is the flat cost of ~900 database-backed tests each setting up a competition, and the
per-test cost of Django's fixtures.

The slowest individual tests, and whether the time is justified:

| Time | Test | Verdict |
| ---: | --- | --- |
| 5.3s | `TestHostThrottle::test_many_usernames_from_one_address_are_eventually_refused` | **Speed up.** It loops to the real configured limit (50 attempts). Override the setting down to 3 and it costs nothing; the logic under test is identical. |
| 4.8s | `test_cp540_reader_reconnects_after_the_link_drops` | **Speed up.** It waits out the real `_RETRY_DELAYS` backoff. Monkeypatch the delays. |
| 4.6s / 3.9s / 3.8s / 3.1s | the other four login-throttle tests | **Speed up**, same way — all four loop to a real limit. |
| 2.4s | `test_a_deactivated_account_cannot_sign_in` | Fine. |

Roughly 25 seconds is recoverable from six tests by lowering limits and backoffs under
test rather than sitting through them. That is 2 % of the run — worth doing when someone
is next in that file, not worth a dedicated pass.

**The honest answer on the 20 minutes is that it is not concentrated anywhere, so there is
no big win available without deleting coverage.** If the run length becomes a real problem
the answer is `pytest-xdist` (the suite is almost entirely independent), not pruning.

---

## 4. What to keep, and why

Nearly all of it. Specifically, these are the parts I would argue hardest for:

- **`config/hostility_tests.py`.** It discovers its own targets. The bug it was written for
  was reachable on nine endpoints at once *because* the tests that existed named their
  targets one at a time. This is the single highest-value file in the suite per line.
- **The file-parametrised checks in `config/tests.py`** — no inline script, no inline
  style, no `onclick=`, no multi-line `{# #}`, every dialog labelled, every control named,
  every `.js` structurally whole, the design-system scales closed, focus rings not
  removed. Each of these is a rule that is easy to break by accident and invisible when
  broken: the page still renders. They cost milliseconds.
- **`config/matrix_tests.py`** (newly moved in — it was never being collected). A failure
  there is a setup that is legal to save and does not work, which is the bug an operator
  hits on race morning and nobody can reproduce.
- **The cost ceilings** — `test_live_endpoint_cost_does_not_grow_with_the_field`,
  `test_live_endpoints_never_write`, and the results equivalents. Neither failure is
  visible until an event is big enough to hurt, which is exactly when you cannot fix it.
- **The scoring and ranking tests in `apps/results/`.** This is the part of the app that
  produces the thing people argue about.

---

## 5. What could go, if you want the suite smaller

I would not delete any of it purely for size. But if you want candidates, in the order I
would actually consider them:

### [ ] 5.1 The legacy connector-loop tests — 6 functions

`test_persist_pulse_resolves_participant_by_bib`, `test_persist_pulse_without_matching_bib_stores_null_participant`,
`test_persist_pulse_with_no_bib`, `test_timing_event_str_handles_missing_bib`,
`test_get_connector_returns_configured_class`, `test_simulator_emits_pulses`

These cover the `TimingEvent` + connector-loop path, which CLAUDE.md calls legacy and
slated to be redone. They are the only tests in the project guarding code that is on its
way out.

**My recommendation: keep them until that path is actually removed, then delete the code
and the tests in one commit.** Deleting the tests first leaves the legacy code unguarded
while it is still shipping, which is the worst of both. They cost well under a second.

### [ ] 5.2 Nothing else

I looked for the usual candidates and did not find them: there are no tests of Django's
own behaviour, no tests of trivial getters, and no two tests asserting the same thing by
different routes that I would call redundant. The overlap that does exist is deliberate —
`matrix_tests` varies *configuration* while the app suites vary *behaviour*, so a class
that ranks correctly (app suite) and a class that ranks correctly under regularity scoring
at 1/1000 s precision in German (matrix) are different questions.

The 12 tests moved in from the audit probe files this round were chosen the same way: the
ones with no equivalent already in the suite.

---

## 6. Three tests need fixing, not keeping or dropping

`config/hostility_tests.py::TestTwoWritersAtOnce` — the three threaded tests — are the only
non-deterministic ones in the project, and a clean full suite run is green only about half
the time. Two of the three have been seen failing; the failure moves between them.

The confirmed one catches `IntegrityError` where SQLite can also refuse the losing writer
with `OperationalError: database table is locked`. Both mean "this desk did not get the
bib", only one is counted.

Full measurements, the captured failure, and the fix are in `OPEN-ITEMS.md` (N-4). Worth
doing before this branch goes anywhere: a test that fails one run in two teaches people to
re-run the suite rather than read it, and the next real failure gets the same shrug.

Note that these tests are **worth keeping** — they are the only ones that exercise the
app's actual threading model (`transaction=True`, so other threads can see the data), and
writing them is what found a real gap in `record_signal`. The problem is their assertions,
not their existence.

---

## 7. Two things about running it

- **`collectstatic` is a prerequisite, not just a release step.** `STORAGES` uses
  WhiteNoise's *manifest* storage in every mode, so a checkout that has never run it fails
  most of the suite with "Missing staticfiles manifest entry" — every page render 500s.
  This is in CLAUDE.md but not in README.
- **Do not run two pytest processes at once.** The `transaction=True` concurrency tests use
  a real file-backed database rather than the in-memory one, so a parallel run makes them
  fail on lock contention that has nothing to do with the code. (This is how N-4 was
  found, so it was not wasted.)
