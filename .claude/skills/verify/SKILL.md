---
name: verify
description: Launch and drive the SlalomTiming Django app to observe a change working end to end.
---

# Verifying SlalomTiming

Django 6 + Channels, `uv`-managed, SQLite. Everything runs locally.

## Launch

```bash
uv run python manage.py migrate            # after any model change
uv run python manage.py runserver 8973     # then drive http://127.0.0.1:8973/
```

Do **not** pass `--noreload`. Without the autoreloader the process serves the
template it first read, so edits made while it's up silently don't appear and
you verify stale code. If you must use it, restart the server after every edit.

`run_timing_connector` is only needed for the live dashboard at `/`; every
other page works with `runserver` alone.

## Driving it

Browser, via the claude-in-chrome tools.

- **Click by `ref`, not by coordinate.** Screenshots come back scaled (1568px
  wide) while the viewport is 2048px, so coordinates read off a screenshot land
  in the wrong place and the click silently does nothing. `read_page` with
  `filter: "interactive"` gives refs that work.
- **Confirm a POST actually happened** by tailing the runserver output —
  `HTTP POST … 302` means the form saved and redirected, `200` means it
  re-rendered with errors. A page that looks unchanged usually means the click
  never landed.
- The toggle switches (`.switch`) hide their checkbox with `opacity: 0`, so they
  don't show up in the interactive tree. Drive them with
  `document.getElementById("id_<field>").click()` via `javascript_tool`.
- Setup pages carry `data-unsaved-guard`: editing a field then clicking a nav
  link raises the "Unsaved changes" modal, and its "Save changes" button posts
  with `next` set and lands you on the page you were heading to. Worth
  exercising on any new setup page.

## Checking what persisted

```bash
uv run python manage.py shell -c "
from apps.competitions.models import CompetitionType
print(CompetitionType.objects.get(pk=5).__dict__)
"
```

The dev DB has real data in it. If you mutate a record to test, restore it
afterwards.

## Gotchas

- A raw `fetch` POST from `javascript_tool` omits any field you don't list, and
  Django reads a missing checkbox as `False` — so unrelated toggles get cleared.
  Fine for probing one field, but don't mistake the result for a bug.
- Widget attrs like `min="0"` are client-side only. To check server-side
  validation, POST the bad value directly rather than typing it into the input.
