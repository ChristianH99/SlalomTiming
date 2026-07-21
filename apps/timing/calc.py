"""Time and penalty arithmetic for the live timing view.

Times are shown at the competition type's device precision and **truncated, never
rounded** — a run is only ever as fast as the device fully resolved, so extra
digits are cut rather than rounded up or down.
"""

from decimal import ROUND_DOWN, Decimal


def _micros(t):
    """A datetime.time as microseconds since midnight."""
    return ((t.hour * 3600 + t.minute * 60 + t.second) * 1_000_000) + t.microsecond


def run_time(start_time, finish_time, precision):
    """Elapsed seconds between two device times as a Decimal truncated to
    `precision` places, or None when either time is missing or the finish is not
    after the start. Works in integer microseconds so truncation never rounds."""
    if start_time is None or finish_time is None:
        return None
    delta_us = _micros(finish_time) - _micros(start_time)
    if delta_us < 0:
        return None
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
    None). The value is assumed already truncated — this only pads decimals."""
    if value is None:
        return ""
    return f"{Decimal(value):.{precision}f}"
