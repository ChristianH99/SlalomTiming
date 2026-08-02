"""The page registry: the single source of truth for the app's gateable "pages".

Access control is *page-level* and coarse — six functional areas, each covering a
set of (app_name, url_name) URL patterns. A role (Django Group, via RoleAccess)
grants a subset of the six page keys; a user's access is the union of their roles'
pages (a superuser gets all of them).

A URL may belong to more than one page (the marshal endpoints are reachable from
both the Timing view and the Marshal Posts view), so access is granted when the
user holds *any* page a URL belongs to.
"""

from django.utils.translation import gettext_lazy as _

# key -> human label, in sidebar order. This ordering is what the User Access
# page renders the page checkboxes in.
PAGES = [
    ("dashboard", _("Dashboard")),
    ("competition_setup", _("Competition Setup")),
    ("participants", _("Participants")),
    ("timing", _("Timing")),
    ("marshal_posts", _("Marshal Posts")),
    ("results", _("Results")),
    ("import_export", _("Backup")),
]

PAGE_KEYS = [key for key, _ in PAGES]
PAGE_LABELS = dict(PAGES)

# The marshal endpoints live under the timing app but drive the Marshal Posts
# page too, so they belong to both pages (either page grants them).
_MARSHAL_ENDPOINTS = {
    ("timing", name)
    for name in (
        "marshal-state", "marshal-submit", "marshal-unlock", "marshal-lock",
        "marshal-lock-all", "marshal-task-edit", "marshal-claim",
        "marshal-release", "marshal-claims",
    )
}

# Closing a run with a state code (DNF/DNC/DNS/DSQ) is a timekeeper's action from
# either timing view *or* from the results table's not-yet-ranked block, so like
# the marshal endpoints it belongs to two pages.
_RUN_STATUS = {("timing", "run-status")}

# page key -> set of (app_name, url_name) it covers.
PAGE_URLS = {
    "dashboard": {
        ("timing", "dashboard"),
        ("timing", "dashboard-state"),
    },
    "competition_setup": {
        ("competitions", name)
        for name in (
            "list", "add", "general", "classes", "runorder", "penalties",
            "delete", "select", "duplicate", "archive", "archived-rules",
            "type-list", "type-add", "type-settings", "type-delete",
        )
    } | {("results", "settings")},
    # Bib assignment is part of registration — the same desk, the same people —
    # so it rides on the Participants key rather than becoming a seventh page
    # that every role would have to be granted separately.
    "participants": {
        ("participants", name)
        for name in ("list", "check", "set-bib", "set-dsq", "add", "edit", "delete",
                     "bib-assignment")
    },
    "timing": {
        ("timing", name)
        for name in (
            "settings", "cp540-status", "input-lock", "simulator", "manual",
            "arrangement", "run-update", "run-add", "run-delete", "ignore",
            "pair", "set-time", "set-runtime", "auto", "auto-state",
            "auto-reorder", "auto-reset-order", "auto-adjust",
        )
    } | _MARSHAL_ENDPOINTS | _RUN_STATUS,
    "marshal_posts": {("competitions", "marshal-posts")} | _MARSHAL_ENDPOINTS,
    "results": {
        ("results", name)
        for name in (
            "index", "class", "overall", "tie-resolve", "pdf-logo-remove",
            "export-all", "export-sample", "export-overall", "export-class",
        )
    } | _RUN_STATUS,
    # Moving whole events and every participant's personal data in and out of the
    # system is its own responsibility — deliberately not folded into Competition
    # Setup, so it can be granted (or withheld) on its own.
    "import_export": {
        ("transfer", name)
        for name in ("backup", "backup-status", "backup-folders",
                     "export", "import", "review",
                     "cancel", "csv-sample")
    },
}

# URLs that must never be gated, and the whole of the reason each one is:
#   * the timing device posts its signals and cannot log in (the view authorises
#     itself instead — see apps/timing/views.py _signal_authorized);
#   * /healthz is what a monitor or the reverse proxy asks, and a check that needs
#     a session is not a check. It answers "ok" or "error" and nothing else, so
#     being open costs nothing (config/health.py).
# An app_name of "" is a route in the root URLconf, outside any include().
OPEN = {("timing", "signal"), ("", "health")}


def pages_for_url(app_name, url_name):
    """The set of page keys a (app_name, url_name) belongs to. Empty means the
    URL is an unmapped utility endpoint — any authenticated user may reach it."""
    return {key for key, urls in PAGE_URLS.items() if (app_name, url_name) in urls}


def user_pages(user):
    """The set of page keys a user may access. Superusers get all of them;
    otherwise it's the union of every role (Group) the user belongs to."""
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(PAGE_KEYS)
    granted = set()
    for group in user.groups.all():
        access = getattr(group, "access", None)
        if access:
            granted.update(access.pages or [])
    return granted
