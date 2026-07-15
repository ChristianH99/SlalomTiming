"""The start pattern: the order participants take their runs within a run.

One pattern is stored per competition (``Competition.start_pattern``) and replayed
for every run the run order defines. A pattern is a list of blocks. A block takes
starters ``window`` at a time (None = all of them at once) and, for each such
window, plays its chips in order: every starter in the window takes that chip's
run before the next chip starts. The block then slides its window down the start
list and repeats until every starter has been through it.

So ``[Block(2, (PRACTICE, COUNTED)), Block(None, (COUNTED,))]`` over bibs 1–5 is::

    1P 2P 1C 2C | 3P 4P 3C 4C | 5P 5C     block 1, three passes of the window
    1C 2C 3C 4C 5C                        block 2, one pass over everyone

Chips name a run *type*, not a run number: each starter consumes their own
practice/counted runs in the order the chips come, which is why the counted chip
in block 1 is everyone's first counted run and the one in block 2 is their second.
That also lets one pattern serve classes with different practice_runs/counted_runs
— a starter who has used up a type sits the chip out rather than over-running.

Like ``assignment``, this module imports no models: it works on ``Starter`` values
the caller builds, so it can be imported from ``models.py`` without a cycle.
"""

from collections import Counter
from dataclasses import dataclass

PRACTICE = "practice"
COUNTED = "counted"
RUN_TYPES = (PRACTICE, COUNTED)
RUN_TYPE_LABELS = {PRACTICE: "Practice", COUNTED: "Counted"}
RUN_TYPE_SHORT = {PRACTICE: "P", COUNTED: "C"}

# A window is a hand-entered number of starters; clamp it so a typo can't make
# the preview expand into something enormous.
MAX_WINDOW = 99

# Upper bound on the preview's made-up starters. Preview-only — never stored.
MAX_DUMMY_STARTERS = 200


@dataclass(frozen=True)
class Block:
    """One repeating section of the pattern."""

    window: int | None  # starters per pass; None = all of them in one pass
    chips: tuple[str, ...]  # run types, in the order they're played


@dataclass(frozen=True)
class Starter:
    """One participant's entry into one class — the unit the pattern schedules.

    A participant entered into the same class twice (or into two classes of the
    same run) is two starters sharing a bib, which is why ``key`` identifies the
    entry-in-a-class rather than the participant.
    """

    key: tuple
    bib: int
    name: str
    class_name: str
    practice_runs: int
    counted_runs: int

    def allowance(self, run_type):
        return self.practice_runs if run_type == PRACTICE else self.counted_runs


@dataclass(frozen=True)
class Slot:
    """A single start: who goes, in which run type, and their how-manieth of it."""

    starter: Starter
    run_type: str
    run_number: int  # 1-based within its own type

    def label(self):
        return f"#{self.starter.bib} {RUN_TYPE_SHORT[self.run_type]}{self.run_number}"


def parse(data):
    """Build blocks from stored/posted JSON, dropping anything malformed rather
    than raising — the pattern is user-arranged UI state, not a trusted schema."""
    if not isinstance(data, list):
        return []
    blocks = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        raw_chips = raw.get("chips")
        if not isinstance(raw_chips, list):
            continue
        chips = tuple(chip for chip in raw_chips if chip in RUN_TYPES)
        if not chips:  # a block with no runs in it would schedule nothing
            continue
        blocks.append(Block(window=_parse_window(raw.get("window")), chips=chips))
    return blocks


def _parse_window(value):
    """A window of None, 0, or junk all mean "everyone at once"."""
    try:
        window = int(value)
    except (TypeError, ValueError):
        return None
    if window < 1:
        return None
    return min(window, MAX_WINDOW)


def serialize(blocks):
    return [{"window": block.window, "chips": list(block.chips)} for block in blocks]


def passes(blocks, starters):
    """Play the pattern over ``starters`` (already in start order), grouped the
    way it reads: a list per block, of a list per window pass, of that pass's
    Slots. ``expand`` flattens this into the plain start order."""
    if not starters:
        return []
    taken = Counter()  # (starter key, run type) -> runs of that type used so far
    result = []
    for block in blocks:
        size = block.window or len(starters)
        block_passes = []
        for start in range(0, len(starters), size):
            pass_slots = []
            for run_type in block.chips:
                for starter in starters[start : start + size]:
                    used = taken[(starter.key, run_type)]
                    if used >= starter.allowance(run_type):
                        continue  # no run of this type left to give
                    taken[(starter.key, run_type)] = used + 1
                    pass_slots.append(Slot(starter, run_type, used + 1))
            block_passes.append(pass_slots)
        result.append(block_passes)
    return result


def expand(blocks, starters):
    """The pattern as a flat start list: every Slot, in the order it starts."""
    return [
        slot
        for block_passes in passes(blocks, starters)
        for pass_slots in block_passes
        for slot in pass_slots
    ]


def shortfalls(blocks, starters):
    """Starters the pattern doesn't fully schedule: ``(starter, {run type: still
    owed})`` for anyone left with runs their class grants but the pattern never
    plays. An empty list means every starter's runs are covered exactly."""
    scheduled = Counter(
        (slot.starter.key, slot.run_type) for slot in expand(blocks, starters)
    )
    missing = []
    for starter in starters:
        owed = {
            run_type: starter.allowance(run_type) - scheduled[(starter.key, run_type)]
            for run_type in RUN_TYPES
            if starter.allowance(run_type) - scheduled[(starter.key, run_type)] > 0
        }
        if owed:
            missing.append((starter, owed))
    return missing
