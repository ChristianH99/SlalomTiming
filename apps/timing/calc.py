"""Time and penalty arithmetic for the live timing view.

Times are shown at the competition type's device precision and **truncated, never
rounded** — a run is only ever as fast as the device fully resolved, so extra
digits are cut rather than rounded up or down.
"""

from decimal import ROUND_DOWN, Decimal


def _micros(t):
    """A datetime.time as microseconds since midnight."""
    return ((t.hour * 3600 + t.minute * 60 + t.second) * 1_000_000) + t.microsecond


# The longest a run may take before a finish "earlier" than its start is read as
# a clock wrap rather than as a mis-paired time. A slalom run is under two
# minutes; an hour is far past anything real and far short of a full day, so a
# genuinely wrong pairing (a finish from this morning on an afternoon start)
# still comes back as no time rather than as twenty-three hours.
MAX_WRAP_GAP_US = 3600 * 1_000_000


def run_time(start_time, finish_time, precision):
    """Elapsed seconds between two device times as a Decimal truncated to
    `precision` places, or None when either time is missing or the pair makes no
    sense. Works in integer microseconds so truncation never rounds.

    Both values are clock *times*, not instants, so a run that begins at 23:59:59
    and ends at 00:00:02 subtracts to −86 397 s. That is not an error, it is
    midnight — and a device whose internal clock wraps at 24 h (the CP540's does;
    see cp540.parse_time) does the same thing at whatever hour it was switched
    on. It used to return None, so the run simply had no time and nobody was told
    which of the two nights it happened on. A negative delta within
    MAX_WRAP_GAP_US is now read as one wrap.
    """
    if start_time is None or finish_time is None:
        return None
    delta_us = _micros(finish_time) - _micros(start_time)
    if delta_us < 0:
        wrapped = delta_us + 86_400 * 1_000_000
        if wrapped > MAX_WRAP_GAP_US:
            return None       # too far apart to be a wrap: a wrong pairing
        delta_us = wrapped
    return truncate_seconds(delta_us, precision)


def truncate_seconds(micros, precision):
    """Integer microseconds -> Decimal seconds truncated to `precision` places."""
    kept_units = micros // (10 ** (6 - precision))  # count of smallest kept unit
    return Decimal(kept_units).scaleb(-precision)


def resolved_run_time(run, precision):
    """A run's elapsed time at `precision`: the operator's typed `manual_run_time`
    when set (used if the device gave no usable start/finish), otherwise computed
    from the paired start/finish signals. Both are truncated, never rounded."""
    manual = getattr(run, "manual_run_time", None)
    if manual is not None:
        return Decimal(manual).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
    return run_time(
        run.start_signal.device_time if run.start_signal_id else None,
        run.finish_signal.device_time if run.finish_signal_id else None,
        precision,
    )


def total_penalty(run, competition_type):
    """Penalty seconds a run adds (whole seconds), or 0 when the type runs without
    penalties. Each error count is multiplied by its per-error amount."""
    if not competition_type.penalties_enabled:
        return 0
    return (
        run.pylon_count * (competition_type.pylon_penalty or 0)
        + run.task_count * (competition_type.task_penalty or 0)
        + run.stopline_count * (competition_type.stop_line_penalty or 0)
    )


def format_precision(value, precision):
    """Fixed-decimal string of a Decimal/int at the given precision (blank for
    None). The value is assumed already truncated — this only pads decimals.

    Not a display formatter: an elapsed time shown to anybody goes through
    ``format_clock``. This is for the places that need the bare number.
    """
    if value is None:
        return ""
    return f"{Decimal(value):.{precision}f}"


def format_clock(value, precision):
    """A run/total time as ``mm:ss.xxx`` (fractional digits at `precision`), blank
    for None. The value is assumed already truncated. Minutes are zero-padded and
    unbounded (a 75s time is ``01:15``). A negative value keeps its sign.

    **The one way an elapsed time is written in this app** — the timing views,
    the Auto view, the Dashboard, the results tables and the PDFs all render a
    run time through here, so the same quantity never appears in two notations
    on two screens (or, as it used to, on the same one).
    """
    if value is None:
        return ""
    v = Decimal(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if precision > 0:
        # Truncated, not formatted. `f"{x:.3f}"` rounds — on an already-truncated
        # value that is invisible, which is why it survived, but this is also
        # handed sums and differences (a run + its penalty, a gap to the winner)
        # and one of those rounding up prints a time a competitor did not drive.
        # The whole app's rule is "as fast as the device fully resolved, never
        # faster", and it has to hold here too.
        v = v.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
    whole = int(v)
    minutes, seconds = divmod(whole, 60)
    if precision > 0:
        frac = str(int((v - whole).scaleb(precision))).zfill(precision)
        return f"{sign}{minutes:02d}:{seconds:02d}.{frac}"
    return f"{sign}{minutes:02d}:{seconds:02d}"


def format_penalty(seconds):
    """Penalty seconds as ``+5 s``, blank when there is none.

    A penalty is a different quantity from a time — a whole-second amount added,
    never measured — so it is written differently on purpose, and always the same
    way. It is never rendered at the device's decimal precision: the amounts a
    competition type carries are whole seconds, so ``+5.00 s`` claimed a
    resolution the number does not have.
    """
    if not seconds:
        return ""
    return f"+{int(seconds)} s"
