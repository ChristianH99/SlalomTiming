"""Parsing and rendering the task-number specs marshal posts are assigned.

A spec is the free-text a user types to say which numbered tasks (gates) a post
watches, e.g. ``"1, 5, 9, 11-15, 20"``. It parses to a set of ints and renders
back to compressed ranges. Kept apart from the model so the Penalties page and
the Marshal Posts page can share one grammar."""

import re

_TOKEN = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


class TaskSpecError(ValueError):
    """A task spec couldn't be parsed."""


def parse(text):
    """Parse a spec into a sorted list of unique task numbers.

    Accepts comma-separated single numbers and ``a-b`` ranges (either order),
    ignoring blank tokens. Raises TaskSpecError on anything malformed or a task
    number below 1."""
    if not text:
        return []
    numbers = set()
    for token in text.split(","):
        if not token.strip():
            continue
        match = _TOKEN.match(token)
        if not match:
            raise TaskSpecError(f"“{token.strip()}” isn't a task number or range.")
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) is not None else low
        if match.group(2) is not None and high < low:
            low, high = high, low
        if low < 1:
            raise TaskSpecError("Task numbers start at 1.")
        numbers.update(range(low, high + 1))
    return sorted(numbers)


def to_ranges(numbers):
    """Collapse a set/list of ints into ``(low, high)`` inclusive spans."""
    ordered = sorted(set(numbers))
    spans = []
    for n in ordered:
        if spans and n == spans[-1][1] + 1:
            spans[-1][1] = n
        else:
            spans.append([n, n])
    return [(low, high) for low, high in spans]


def format_ranges(numbers):
    """Render task numbers as compressed ranges, e.g. ``"1-3, 5, 9-12"``."""
    return ", ".join(
        str(low) if low == high else f"{low}-{high}"
        for low, high in to_ranges(numbers)
    )


def summary(numbers):
    """A one-line assignment summary for the combined tasks of all posts."""
    if not numbers:
        return "No tasks assigned yet"
    return f"Tasks {format_ranges(numbers)} assigned"
