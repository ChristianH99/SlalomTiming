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
    rank: int | None = None
    inspect: bool = False
    skipped: bool = False  # a lower repeat entry of a participant already ranked


# ----- persisting Auto timing's positional identity onto its runs -----

def sync_identities(competition):
    """Fold Auto timing's slot binding onto the runs so results read one
    representation. Delegates to ``autotiming.sync_bindings`` — the same routine
    the Auto timing view runs — so a run's identity and penalties match whichever
    screen recorded it."""
    autotiming.sync_bindings(competition)


# ----- penalties / totals -----

def run_penalty_seconds(run, competition):
    """Whole penalty seconds a run adds. Delegates to autotiming so results, the
    Auto view and the Manual view all agree: marshal-post totals + the timekeeper's
    signed adjust for a marshal-driven run, otherwise the run's own counts (which
    includes a run the operator owns even in marshal mode)."""
    return autotiming.penalty_seconds(run, competition)


def _counted_run_totals(competition, bib, cclass, occurrence, precision):
    """``{run_number: RunResult}`` for a competitor's finished counted runs. When a
    run number was recorded more than once the lowest total is kept."""
    runs = (
        TimedRun.objects.filter(
            competition=competition, bib_number=bib,
            competition_class=cclass, class_occurrence=occurrence,
            run_type=TimedRun.RunType.COUNTED,
        )
        .select_related("start_signal", "finish_signal")
        .prefetch_related("marshal_penalties")
    )
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


def _competitor(competition, cclass, entry, occurrence, precision):
    by_number = _counted_run_totals(
        competition, entry.bib_number, cclass, occurrence, precision
    )
    counted = cclass.counted_runs or 0
    runs = [
        by_number.get(number, RunResult(number, None, 0, None))
        for number in range(1, counted + 1)
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
    )


# ----- ranking -----

def _separated(a, b, tie_break):
    """Whether two equal-score competitors are pulled apart by the tie-break rule.
    Fastest-run separates them when their single fastest run differs; Manual never
    does (a human must decide)."""
    if tie_break == CompetitionType.TieBreak.FASTEST_RUN:
        return a.best_total != b.best_total
    return False


def _rank(competitors, tie_break):
    """Assign ranks to the complete competitors in place. Sorted by score, then by
    fastest run and bib for a stable order. A participant's repeat entries after
    their first ranked one are skipped; equal scores the tie-break can't separate
    share a rank and are flagged for inspection."""
    ordered = sorted(
        competitors,
        key=lambda c: (c.score, c.best_total if c.best_total is not None else Decimal(0), c.bib),
    )
    seen = set()
    position = 0
    previous = None
    for competitor in ordered:
        if competitor.participant_id in seen:
            competitor.skipped = True  # lower repeat of an already-ranked participant
            continue
        seen.add(competitor.participant_id)
        position += 1
        if previous is not None and competitor.score == previous.score \
                and not _separated(previous, competitor, tie_break):
            competitor.rank = previous.rank
            competitor.inspect = True
            previous.inspect = True
        else:
            competitor.rank = position
        previous = competitor
    return ordered


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
    competitors = []
    for starter in starters:
        entry_pk, _, occurrence = starter.key
        entry = entries.get(entry_pk)
        if entry is None:
            continue
        competitors.append(_competitor(competition, cclass, entry, occurrence, precision))

    complete = [c for c in competitors if c.complete]
    ranked = _rank(complete, competition.competition_type.tie_break)
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
