"""Turn recorded runs into ranked class results.

Both timing screens now feed one representation: the Times view writes a run's
bib/class/run directly, and ``sync_identities`` copies Auto timing's positional
binding onto its runs the same way, so results read ``TimedRun`` fields uniformly.

Scoring per ``CompetitionClass.Scoring``:
  * aggregate  – sum of every counted run's total time (run + penalties)
  * best_run   – the single fastest counted run's total time
  * regularity – the spread (max − min) of the counted runs' totals; smaller wins

Lower score is always better. A competitor is *rankable* with a live status once
enough runs are recorded — every counted run for aggregate/regularity, but a single
full run is enough for best-run; the rest are listed unranked. A participant entered
into the same class more than once keeps only their best result ranked. Equal scores
are separated by the type's tie-break (``CompetitionType.TieBreak``); when it can't
separate them (or is Manual) both share the rank and are flagged for inspection.
"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from apps.competitions.models import CompetitionClass, CompetitionType
from apps.timing import autotiming, calc
from apps.timing.models import TimedRun


@dataclass
class RunResult:
    run_number: int
    run_time: Decimal | None
    penalty: int
    total: Decimal | None

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
    """``{run_number: RunResult}`` for a competitor's finished runs of ``run_type``.
    When a run number was recorded more than once the lowest total is kept."""
    runs = index.runs(bib, cclass, occurrence, run_type)
    by_number = {}
    for run in runs:
        rt = calc.resolved_run_time(run, precision)
        if rt is None or run.run_number is None:
            continue
        penalty = run_penalty_seconds(run, competition)
        total = rt + penalty
        prev = by_number.get(run.run_number)
        if prev is None or total < prev.total:
            by_number[run.run_number] = RunResult(run.run_number, rt, penalty, total)
    return by_number


# ----- scoring -----

DNX_STATUSES = {"dns", "dnf", "dsq"}


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


def _competitor(competition, index, cclass, entry, occurrence, precision):
    by_number = _run_totals(
        competition, index, entry.bib_number, cclass, occurrence, precision,
        TimedRun.RunType.COUNTED,
    )
    counted = cclass.counted_runs or 0
    runs = [
        by_number.get(number, RunResult(number, None, 0, None))
        for number in range(1, counted + 1)
    ]
    practice_by_number = _run_totals(
        competition, index, entry.bib_number, cclass, occurrence, precision,
        TimedRun.RunType.PRACTICE,
    )
    training = [
        practice_by_number.get(number, RunResult(number, None, 0, None))
        for number in range(1, (cclass.practice_runs or 0) + 1)
    ]
    totals = [r.total for r in runs if r.total is not None]
    # Best-run only needs one full counted run to place; the other methods need
    # every counted run (a sum/spread over a missing run would be meaningless).
    if cclass.scoring_method == CompetitionClass.Scoring.BEST_RUN:
        enough = len(totals) >= 1
    else:
        enough = counted > 0 and len(totals) == counted
    live = entry.status not in DNX_STATUSES
    complete = enough and live
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


def _find_manual(manual, member_keys):
    """The stored resolution whose member set exactly matches this tie group, or
    None. ``manual`` is the competition's resolutions already filtered to the scope."""
    for res in manual:
        if res.member_keys() == member_keys:
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

    res = _find_manual(manual, keys)
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
            return None, "Unknown competitor in the submitted order."
        rows.append((competitor, int(rank)))
    if not rows:
        return None, "No competitors submitted."

    first = rows[0][0]
    if not first.tie_group or any(c.tie_group != first.tie_group for c, _ in rows):
        return None, "Competitors are not all in the same tie group."
    if len(rows) != first.tie_size:
        return None, "The submitted order must cover the whole tie group."

    start = first.tie_start
    previous = None
    members = []
    for index, (competitor, rank) in enumerate(rows):
        if index == 0:
            if rank != start:
                return None, f"The first competitor must be rank {start}."
        elif rank != previous and rank != start + index:
            return None, "Ranks must go in order — a tie keeps the lower number."
        members.append([competitor.entry_pk, competitor.occurrence, rank])
        previous = rank
    return members, None


@dataclass
class ClassResults:
    competition_class: CompetitionClass
    scoring_method: str
    ranked: list = field(default_factory=list)      # complete, in finishing order
    unranked: list = field(default_factory=list)    # incomplete / DNS / DNF / DSQ


def compute_class_results(competition, cclass):
    """The full result of one class: ranked complete competitors (repeat entries and
    ties handled), then the unranked rest."""
    precision = competition.competition_type.timing_precision
    entries = {
        entry.pk: entry
        for entry in competition.entries.select_related("participant").all()
    }
    starters = competition.starters_by_class().get(cclass.pk, [])
    index = RunIndex(competition)
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
    complete = [c for c in competitors if c.complete]
    ranked = _rank(
        complete, competition.competition_type.tie_break,
        scope=scope, manual=load_manual(competition, scope),
    )
    unranked = sorted(
        (c for c in competitors if not c.complete),
        key=lambda c: (c.bib, c.occurrence),
    )
    return ClassResults(
        competition_class=cclass,
        scoring_method=cclass.scoring_method,
        ranked=ranked,
        unranked=unranked,
    )


# ----- cross-class Overall results -----

_METHOD_ORDER = {
    CompetitionClass.Scoring.AGGREGATE: 0,
    CompetitionClass.Scoring.BEST_RUN: 1,
    CompetitionClass.Scoring.REGULARITY: 2,
}


def overall_groups(competition):
    """The distinct ``(scoring_method, counted_runs)`` groups among the running
    classes — one Overall page each. Returns ordered descriptors so the sidebar and
    the index can list them (method order, then run count)."""
    labels = dict(CompetitionClass.Scoring.choices)
    groups = {}
    for cc in competition._running_classes_ordered():
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
            "label": f"{labels.get(method, method)} · {runs} Runs",
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
    unranked: list = field(default_factory=list)


def compute_overall_results(competition, method, counted_runs):
    """Rank competitors across every running class that shares ``method`` and
    ``counted_runs`` into one table. A participant entered in two such classes shows
    once per class (dedup keys by participant *and* class), each with its own row."""
    precision = competition.competition_type.timing_precision
    entries = {
        entry.pk: entry
        for entry in competition.entries.select_related("participant").all()
    }
    classes = [
        cc for cc in competition._running_classes_ordered()
        if cc.scoring_method == method and (cc.counted_runs or 0) == counted_runs
    ]
    by_class = competition.starters_by_class()
    index = RunIndex(competition)
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
    complete = [c for c in competitors if c.complete]
    ranked = _rank(
        complete, competition.competition_type.tie_break,
        dedup_key=lambda c: (c.participant_id, c.class_pk),
        scope=scope, manual=load_manual(competition, scope),
    )
    unranked = sorted(
        (c for c in competitors if not c.complete),
        key=lambda c: (c.bib, c.class_name, c.occurrence),
    )
    training = max((cc.practice_runs or 0) for cc in classes) if classes else 0
    labels = dict(CompetitionClass.Scoring.choices)
    return OverallResults(
        method=method,
        method_label=labels.get(method, method),
        scoring_method=method,
        counted_runs=counted_runs,
        training_runs=training,
        ranked=ranked,
        unranked=unranked,
    )
