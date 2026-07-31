"""Turn recorded runs into ranked class results.

Both timing screens now feed one representation: the Times view writes a run's
bib/class/run directly, and ``sync_identities`` copies Auto timing's positional
binding onto its runs the same way, so results read ``TimedRun`` fields uniformly.

Scoring per ``CompetitionClass.Scoring``:
  * aggregate  – sum of every counted run's total time (run + penalties)
  * best_run   – the single fastest counted run's total time
  * regularity – the spread (max − min) of the counted runs' totals; smaller wins

Lower score is always better. A competitor is *rankable* once enough runs are
recorded — every counted run for aggregate/regularity, but a single full run is
enough for best-run. A participant entered into the same class more than once keeps
only their best result ranked. Equal scores are separated by the type's tie-break
(``CompetitionType.TieBreak``); when it can't separate them (or is Manual) both
share the rank and are flagged for inspection.

A run can also end in a **state code** rather than a time — DNF, DNC, DNS or DSQ
(``TimedRun.Status``, set on the timing views or, for DNS, from the results table).
``_final_status`` turns those into the competitor's own outcome; the short version
is that a state code on a *practice* run means nothing here, one on a counted run
costs the score (DNC) unless best-run scoring can still place them on the runs they
did drive, and an all-DSQ or all-DNS set of counted runs reads as that code. So
every competitor is in exactly one of three groups (``_split``): ranked, finished
on a state code, or still waiting for a run that is neither timed nor marked.
"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils.translation import gettext

from apps.competitions.models import CompetitionClass, CompetitionType
from apps.participants.models import EventEntry
from apps.timing import autotiming, calc
from apps.timing.models import TimedRun


@dataclass
class RunResult:
    run_number: int
    run_time: Decimal | None
    penalty: int
    total: Decimal | None
    # DNF / DNC / DNS / DSQ when the run was closed with a state code instead of a
    # time (``TimedRun.Status``), else "". A run carrying one has no ``total``
    # whatever times sit on it — a disqualified run is usually a measured one.
    status: str = ""
    # The start-order slot this run stands for (``autotiming.slot_key``). Carried
    # so the results table can mark a run that was never recorded DNS: there is no
    # run id to name, only the competitor-and-run it would have been.
    key: str = ""

    @property
    def recorded(self):
        return self.total is not None


@dataclass
class CompetitorResult:
    entry_pk: int
    participant_id: int
    bib: int
    name: str
    occurrence: int
    status: str
    runs: list[RunResult]
    score: Decimal | None
    best_total: Decimal | None
    complete: bool
    # The competitor's own final state code — "dns" / "dnc" / "dsq" — when their
    # counted runs (or the whole event) ended in one instead of a score. Empty for
    # everyone else, ranked or still waiting. See _final_status for the rules.
    final_status: str = ""
    class_pk: int = 0
    class_name: str = ""
    training: list[RunResult] = field(default_factory=list)
    rank: int | None = None
    inspect: bool = False  # an unresolved tie still needing a manual decision (red flag)
    skipped: bool = False  # a lower repeat entry of a participant already ranked
    # Tie handling: competitors sharing a score form a tie group. tie_state is
    # "auto" (a rule separated them), "manual" (a timekeeper ordered them) or
    # "pending" (unresolved — inspect). tie_group ids the group; tie_start/tie_size
    # are its first rank and member count (so a manual edit knows the valid ranks).
    tie_group: str = ""
    tie_state: str = ""
    tie_start: int = 0
    tie_size: int = 0


# ----- persisting Auto timing's positional identity onto its runs -----

def sync_identities(competition):
    """Fold Auto timing's slot binding onto the runs *and persist it*. Delegates to
    ``autotiming.sync_bindings`` — the same routine the timing views' write paths
    run — so a run's identity matches whichever screen recorded it.

    Reading results does **not** need this: ``RunIndex`` binds the runs it has just
    read, in memory. Rendering a table is a GET and must not take the write lock
    the timing rig needs (see autotiming.sync_bindings)."""
    autotiming.sync_bindings(competition)


# ----- penalties / totals -----

def run_penalty_seconds(run, competition):
    """Whole penalty seconds a run adds. Delegates to autotiming so results, the
    Auto view and the Manual view all agree: marshal-post totals + the timekeeper's
    signed adjust for a marshal-driven run, otherwise the run's own counts (which
    includes a run the operator owns even in marshal mode)."""
    return autotiming.penalty_seconds(run, competition)


class RunIndex:
    """The competition's recorded runs, read in one query and grouped by the slot
    they belong to (bib, class, occurrence, run type).

    A results table asked the database for a competitor's runs twice over — once
    for the counted runs and once for the practice ones — per competitor, per
    class: 149 queries for a 40-strong class, 629 for 200, and the "export
    everything" PDF multiplies that by the class count. A whole event's runs are
    one query, so they are read once and the table is assembled from memory.
    """

    def __init__(self, competition):
        runs = autotiming.all_runs(competition)
        # Auto timing identifies a run by its place in the start order, not by a
        # typed bib. Bind these copies before grouping them, so a positionally
        # timed run is filed under the competitor it belongs to — in memory, so
        # rendering a table stays a pure read (see sync_identities).
        autotiming.apply_bindings(competition, runs)
        self._by_slot = {}
        for run in runs:
            key = (run.bib_number, run.competition_class_id,
                   run.class_occurrence, run.run_type)
            self._by_slot.setdefault(key, []).append(run)

    def runs(self, bib, cclass, occurrence, run_type):
        return self._by_slot.get((bib, cclass.pk, occurrence, run_type), ())


def _run_totals(competition, index, bib, cclass, occurrence, precision, run_type):
    """``{run_number: RunResult}`` for a competitor's *settled* runs of
    ``run_type`` — one that produced a time, and one closed with a state code.

    When a run number was recorded more than once the better row is kept: a state
    code beats a time (it is a timekeeper's decision about this run, while a
    second timed row for the same run number is a stray), and between two timed
    rows the lowest total wins."""
    runs = index.runs(bib, cclass, occurrence, run_type)
    by_number = {}
    for run in runs:
        if run.run_number is None:
            continue
        rt = calc.resolved_run_time(run, precision)
        status = run.status or ""
        if rt is None and not status:
            continue  # still to come
        penalty = run_penalty_seconds(run, competition)
        # A run closed with a state code is not scored, whatever time it carries.
        result = RunResult(run.run_number, rt, penalty,
                           None if status else rt + penalty, status)
        previous = by_number.get(run.run_number)
        if previous is None or _supersedes(result, previous):
            by_number[run.run_number] = result
    return by_number


def _supersedes(new, previous):
    """Whether a second row recorded for the same run number replaces the first."""
    if bool(new.status) != bool(previous.status):
        return bool(new.status)          # the timekeeper's decision wins
    if new.status:
        return False                      # two state codes: keep the first
    return new.total < previous.total


# ----- scoring -----

# An event-wide status on the EventEntry -> the competitor's final state code. The
# whole-event disqualification set on the participant list writes DSQ here; DNS and
# DNF are older per-entry values kept working (a did-not-finish leaves them
# unclassified, which is what DNC says).
_ENTRY_FINAL = {
    EventEntry.Status.DSQ: TimedRun.Status.DSQ,
    EventEntry.Status.DNS: TimedRun.Status.DNS,
    EventEntry.Status.DNF: TimedRun.Status.DNC,
}

# Final state codes in the order they are listed below the ranked table.
FINAL_STATUS_ORDER = [TimedRun.Status.DNS, TimedRun.Status.DNC, TimedRun.Status.DSQ]


def _final_status(entry_status, cclass, runs):
    """The competitor's own state code, or "" when their counted runs can still
    produce a score. ``runs`` is their counted runs, one entry per run the class
    grants (unrecorded ones blank).

    Practice runs never reach here: a state code on one has no influence on the
    result at all. The rules over the counted runs are:

      * the whole event disqualified                            → DSQ, at once —
        it is a decision about the competitor, not about a run
      * a counted run still neither timed nor marked            → no status yet.
        A competitor is only moved out of the running once *every* counted run has
        settled one way or the other, so a DNF on their first run doesn't retire
        them while they still have a second to drive
      * every counted run DSQ                                   → DSQ
      * every counted run DNS                                   → DNS
      * best-run scoring with at least one counted run timed    → no status; they
        rank on the runs they did drive
      * anything else with a state code on a counted run        → DNC (a sum or a
        spread over a run that was never driven is not a result)
    """
    from_entry = _ENTRY_FINAL.get(entry_status)
    if from_entry:
        return from_entry
    if any(run.total is None and not run.status for run in runs):
        return ""
    marks = [run.status for run in runs if run.status]
    if not marks:
        return ""
    if len(marks) == len(runs):
        if all(mark == TimedRun.Status.DSQ for mark in marks):
            return TimedRun.Status.DSQ
        if all(mark == TimedRun.Status.DNS for mark in marks):
            return TimedRun.Status.DNS
    if cclass.scoring_method == CompetitionClass.Scoring.BEST_RUN \
            and any(run.total is not None for run in runs):
        return ""
    return TimedRun.Status.DNC


def _score(method, totals):
    """The class's score from its recorded counted-run totals (all present), or the
    partial value used for display. ``None`` when nothing is recorded."""
    if not totals:
        return None
    if method == CompetitionClass.Scoring.BEST_RUN:
        return min(totals)
    if method == CompetitionClass.Scoring.REGULARITY:
        return max(totals) - min(totals)
    return sum(totals)  # AGGREGATE


def _slots(by_number, count, entry, cclass, occurrence, run_type):
    """One ``RunResult`` per run the class grants, blank where nothing is settled
    yet, each carrying the start-order slot key it stands for — which is how the
    results table can mark a run that was never recorded DNS."""
    slots = []
    for number in range(1, count + 1):
        result = by_number.get(number) or RunResult(number, None, 0, None)
        result.key = autotiming.slot_key(
            entry.pk, cclass.pk, occurrence, run_type, number
        )
        slots.append(result)
    return slots


def _competitor(competition, index, cclass, entry, occurrence, precision):
    by_number = _run_totals(
        competition, index, entry.bib_number, cclass, occurrence, precision,
        TimedRun.RunType.COUNTED,
    )
    counted = cclass.counted_runs or 0
    runs = _slots(by_number, counted, entry, cclass, occurrence,
                  TimedRun.RunType.COUNTED)
    practice_by_number = _run_totals(
        competition, index, entry.bib_number, cclass, occurrence, precision,
        TimedRun.RunType.PRACTICE,
    )
    # A state code on a practice run is recorded but has no influence on the
    # result, so training slots are never consulted below.
    training = _slots(practice_by_number, cclass.practice_runs or 0, entry, cclass,
                      occurrence, TimedRun.RunType.PRACTICE)
    totals = [r.total for r in runs if r.total is not None]
    # Best-run only needs one full counted run to place; the other methods need
    # every counted run (a sum/spread over a missing run would be meaningless).
    if cclass.scoring_method == CompetitionClass.Scoring.BEST_RUN:
        enough = len(totals) >= 1
    else:
        enough = counted > 0 and len(totals) == counted
    final_status = _final_status(entry.status, cclass, runs)
    complete = enough and not final_status
    score = _score(cclass.scoring_method, totals) if complete else None
    return CompetitorResult(
        entry_pk=entry.pk,
        participant_id=entry.participant_id,
        bib=entry.bib_number,
        name=str(entry.participant),
        occurrence=occurrence,
        status=entry.status,
        runs=runs,
        score=score,
        best_total=min(totals) if totals else None,
        complete=complete,
        final_status=final_status,
        class_pk=cclass.pk,
        class_name=cclass.name,
        training=training,
    )


# ----- ranking -----

def _separated(a, b, tie_break):
    """Whether two equal-score competitors are pulled apart by the tie-break rule.
    Fastest-run separates them when their single fastest run differs; Manual never
    does (a human must decide)."""
    if tie_break == CompetitionType.TieBreak.FASTEST_RUN:
        return a.best_total != b.best_total
    return False


def _find_manual(manual, member_keys, score):
    """The stored resolution for this tie group, or None. ``manual`` is the
    competition's resolutions already filtered to the scope.

    Both the member set *and* the score have to match: the same competitors can
    end up tied again on a different score after another run, and a decision made
    about that earlier score was never about this one. A resolution stored before
    the score was recorded (``score is None``) never matches, so the tie is put
    back to the timekeeper rather than resolved from a guess."""
    for res in manual:
        if res.score is not None and res.score == score and res.member_keys() == member_keys:
            return res
    return None


def _rank_group(group, tie_break, position, scope, manual):
    """Rank one score-tie group (competitors sharing a score), in place, and return
    it in display order. A single member just takes its position. Otherwise the
    group is a tie: a stored manual resolution wins (its order + ranks, green); else
    the tie-break rule orders them — members it fully separates rank distinctly and
    go green ("auto"), members it can't split share a rank and stay red ("pending")."""
    if len(group) == 1:
        group[0].rank = position
        return group

    tie_group = f"{scope}|{group[0].score}"
    keys = frozenset((m.entry_pk, m.occurrence) for m in group)
    for m in group:
        m.tie_group = tie_group
        m.tie_start = position
        m.tie_size = len(group)

    res = _find_manual(manual, keys, group[0].score)
    if res is not None:
        by_key = {(m.entry_pk, m.occurrence): m for m in group}
        ordered = []
        for entry_pk, occurrence, rank in res.members:
            m = by_key.get((entry_pk, occurrence))
            if m is None:
                ordered = None  # stored order no longer matches; fall back to auto
                break
            m.rank = rank
            m.tie_state = "manual"
            ordered.append(m)
        if ordered is not None:
            return ordered

    # Automatic: order by the tie-break discriminator, assign standard ranks.
    group.sort(key=lambda m: (m.best_total if m.best_total is not None else Decimal(0), m.bib))
    previous = None
    for index, m in enumerate(group):
        if previous is not None and not _separated(previous, m, tie_break):
            m.rank = previous.rank
        else:
            m.rank = position + index
        previous = m
    counts = Counter(m.rank for m in group)
    for m in group:
        if counts[m.rank] > 1:
            m.tie_state = "pending"
            m.inspect = True
        else:
            m.tie_state = "auto"
    return group


def _rank(competitors, tie_break, dedup_key=lambda c: c.participant_id,
          scope="", manual=None):
    """Rank the complete competitors. Sorted by score, then fastest run and bib. A
    competitor's repeat entries (same ``dedup_key``) after their first ranked one are
    skipped (shown, no rank). Competitors sharing a score form a tie group handled by
    ``_rank_group``. Returns the ranked competitors in display order followed by the
    skipped ones. Overall pages key by (participant, class) so one competitor still
    shows once per class."""
    manual = manual or []
    ordered = sorted(
        competitors,
        key=lambda c: (c.score, c.best_total if c.best_total is not None else Decimal(0), c.bib),
    )
    survivors, skipped = [], []
    seen = set()
    for competitor in ordered:
        key = dedup_key(competitor)
        if key in seen:
            competitor.skipped = True  # lower repeat of an already-ranked competitor
            skipped.append(competitor)
        else:
            seen.add(key)
            survivors.append(competitor)

    display, index, position = [], 0, 1
    while index < len(survivors):
        end = index
        while end < len(survivors) and survivors[end].score == survivors[index].score:
            end += 1
        group = _rank_group(survivors[index:end], tie_break, position, scope, manual)
        display.extend(group)
        position += len(group)
        index = end
    return display + skipped


def load_manual(competition, scope):
    """The stored manual tie resolutions for a results table (scope)."""
    return list(competition.tie_resolutions.filter(scope=scope))


def class_scope(cclass):
    """The tie-resolution scope string for a class results table."""
    return f"class:{cclass.pk}"


def overall_scope(method, counted_runs):
    """The tie-resolution scope string for an Overall results table."""
    return f"overall:{method}:{counted_runs}"


def live_tie_keys(ranked):
    """The member sets of the tie groups that exist in a freshly computed table —
    what a stored resolution has to match to still mean anything. Used to sweep
    resolutions whose group has dissolved: a stale row is already ignored on
    render, but nothing ever deleted it, so they accumulated for the life of the
    competition and travelled into every export."""
    groups = {}
    for competitor in ranked:
        if competitor.tie_group:
            groups.setdefault(competitor.tie_group, set()).add(
                (competitor.entry_pk, competitor.occurrence)
            )
    return {frozenset(members) for members in groups.values()}


def validate_resolution(ranked, posted):
    """Validate a posted manual ordering against the freshly computed ``ranked``.

    ``posted`` is an ordered list of ``(entry_pk, occurrence, rank)`` — the display
    order the timekeeper chose and the rank each was given. Returns
    ``(members, error)``: on success ``members`` is the cleaned list to store and
    ``error`` is None; otherwise ``members`` is None and ``error`` explains why.

    The order must cover exactly one whole tie group. Ranks follow standard
    competition ranking: the first is the group's start position ``p``; each later
    one either ties the previous (keeps its number) or takes its own position
    ``p + i`` — so 1,2 or 1,1 is allowed for a leading pair, but never 2,1."""
    by_key = {(c.entry_pk, c.occurrence): c for c in ranked}
    rows = []
    for entry_pk, occurrence, rank in posted:
        competitor = by_key.get((entry_pk, occurrence))
        if competitor is None:
            return None, gettext("Unknown competitor in the submitted order.")
        rows.append((competitor, int(rank)))
    if not rows:
        return None, gettext("No competitors submitted.")

    first = rows[0][0]
    if not first.tie_group or any(c.tie_group != first.tie_group for c, _ in rows):
        return None, gettext("Competitors are not all in the same tie group.")
    if len(rows) != first.tie_size:
        return None, gettext("The submitted order must cover the whole tie group.")

    start = first.tie_start
    previous = None
    members = []
    for index, (competitor, rank) in enumerate(rows):
        if index == 0:
            if rank != start:
                return None, gettext("The first competitor must be rank %(rank)s.") % {"rank": start}
        elif rank != previous and rank != start + index:
            return None, gettext("Ranks must go in order — a tie keeps the lower number.")
        members.append([competitor.entry_pk, competitor.occurrence, rank])
        previous = rank
    return members, None


def _split(competitors):
    """Sort the competitors into the three groups a results table shows:

      * **ranked** — every counted run settled with a time, so they have a score;
      * **status** — settled, but on a state code: they belong at the bottom of the
        final result, ordered DNS, then DNC, then DSQ, and by bib within each;
      * **unranked** — still waiting on a run that is neither timed nor marked.

    Returns ``(complete, status_rows, unranked)``; the caller ranks ``complete``.
    """
    complete = [c for c in competitors if c.complete]
    status_rows = sorted(
        (c for c in competitors if c.final_status and not c.complete),
        key=lambda c: (FINAL_STATUS_ORDER.index(c.final_status), c.bib, c.occurrence),
    )
    waiting = [c for c in competitors if not c.complete and not c.final_status]
    return complete, status_rows, waiting


@dataclass
class ClassResults:
    competition_class: CompetitionClass
    scoring_method: str
    ranked: list = field(default_factory=list)      # complete, in finishing order
    # Settled on a state code (DNS / DNC / DSQ): shown at the foot of the ranked
    # table, since their event is over — they are simply not in the placings.
    status_rows: list = field(default_factory=list)
    unranked: list = field(default_factory=list)    # still missing a counted run


def event_data(competition):
    """The three whole-event reads every results table starts from, done once.

    ``compute_class_results`` used to do all three for itself, so "export
    everything" paid for them once per class: 42 queries for one class, 137 for
    six, on an event whose runs are a single query. A caller rendering more than
    one table reads this once and hands it to each.
    """
    entries = {
        entry.pk: entry
        for entry in competition.entries.select_related("participant").all()
    }
    return {
        "entries": entries,
        # participant pk -> Participant, for the row builder (apps/results/views.py).
        "participants": {e.participant_id: e.participant for e in entries.values()},
        "by_class": competition.starters_by_class(),
        "index": RunIndex(competition),
        # The running classes, asked for by the column vocabulary, the Overall
        # grouping and the export loop — a query each, times the class count.
        "running": competition._running_classes_ordered(),
    }


def compute_class_results(competition, cclass, data=None):
    """The full result of one class: ranked complete competitors (repeat entries and
    ties handled), the ones whose event ended in a state code, then whoever is
    still to finish.

    ``data`` is ``event_data(competition)`` when the caller is rendering several
    tables; without it this reads the event for itself."""
    precision = competition.competition_type.timing_precision
    data = data or event_data(competition)
    entries = data["entries"]
    starters = data["by_class"].get(cclass.pk, [])
    index = data["index"]
    competitors = []
    for starter in starters:
        entry_pk, _, occurrence = starter.key
        entry = entries.get(entry_pk)
        if entry is None:
            continue
        competitors.append(
            _competitor(competition, index, cclass, entry, occurrence, precision)
        )

    scope = class_scope(cclass)
    complete, status_rows, waiting = _split(competitors)
    ranked = _rank(
        complete, competition.competition_type.tie_break,
        scope=scope, manual=load_manual(competition, scope),
    )
    unranked = sorted(waiting, key=lambda c: (c.bib, c.occurrence))
    return ClassResults(
        competition_class=cclass,
        scoring_method=cclass.scoring_method,
        ranked=ranked,
        status_rows=status_rows,
        unranked=unranked,
    )


# ----- cross-class Overall results -----

_METHOD_ORDER = {
    CompetitionClass.Scoring.AGGREGATE: 0,
    CompetitionClass.Scoring.BEST_RUN: 1,
    CompetitionClass.Scoring.REGULARITY: 2,
}


def overall_groups(competition, running=None):
    """The distinct ``(scoring_method, counted_runs)`` groups among the running
    classes — one Overall page each. Returns ordered descriptors so the sidebar and
    the index can list them (method order, then run count)."""
    labels = dict(CompetitionClass.Scoring.choices)
    groups = {}
    for cc in (competition._running_classes_ordered() if running is None else running):
        key = (cc.scoring_method, cc.counted_runs or 0)
        groups.setdefault(key, []).append(cc)
    descriptors = []
    for (method, runs), classes in sorted(
        groups.items(), key=lambda kv: (_METHOD_ORDER.get(kv[0][0], 9), kv[0][1])
    ):
        descriptors.append({
            "method": method,
            "counted_runs": runs,
            "method_label": labels.get(method, method),
            "label": gettext("%(label)s · %(runs)s Runs") % {
                "label": labels.get(method, method), "runs": runs},
            "classes": classes,
        })
    return descriptors


@dataclass
class OverallResults:
    method: str
    method_label: str
    scoring_method: str
    counted_runs: int
    training_runs: int
    ranked: list = field(default_factory=list)
    status_rows: list = field(default_factory=list)
    unranked: list = field(default_factory=list)


def compute_overall_results(competition, method, counted_runs, data=None):
    """Rank competitors across every running class that shares ``method`` and
    ``counted_runs`` into one table. A participant entered in two such classes shows
    once per class (dedup keys by participant *and* class), each with its own row.

    ``data`` is ``event_data(competition)`` when the caller is rendering several
    tables — see there."""
    precision = competition.competition_type.timing_precision
    data = data or event_data(competition)
    entries = data["entries"]
    classes = [
        cc for cc in data["running"]
        if cc.scoring_method == method and (cc.counted_runs or 0) == counted_runs
    ]
    by_class = data["by_class"]
    index = data["index"]
    competitors = []
    for cclass in classes:
        for starter in by_class.get(cclass.pk, []):
            entry_pk, _, occurrence = starter.key
            entry = entries.get(entry_pk)
            if entry is None:
                continue
            competitors.append(
                _competitor(competition, index, cclass, entry, occurrence, precision)
            )

    scope = overall_scope(method, counted_runs)
    complete, status_rows, waiting = _split(competitors)
    ranked = _rank(
        complete, competition.competition_type.tie_break,
        dedup_key=lambda c: (c.participant_id, c.class_pk),
        scope=scope, manual=load_manual(competition, scope),
    )
    unranked = sorted(waiting, key=lambda c: (c.bib, c.class_name, c.occurrence))
    training = max((cc.practice_runs or 0) for cc in classes) if classes else 0
    labels = dict(CompetitionClass.Scoring.choices)
    return OverallResults(
        method=method,
        method_label=labels.get(method, method),
        scoring_method=method,
        counted_runs=counted_runs,
        training_runs=training,
        ranked=ranked,
        status_rows=status_rows,
        unranked=unranked,
    )
