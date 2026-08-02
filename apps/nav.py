"""Which sidebar entry is the current page.

The sidebar used to work this out inline, one literal per entry, by comparing
`request.resolver_match.url_name` against the name it expected. A url_name is
only unique *within* an app, and two apps use the same one: opening Competition
Setup -> Results (`results:settings`) also lit Timing -> Settings
(`timing:settings`) up, because both entries asked the same question and only
one of them named the app. Roughly half the entries did guard on `app_name` and
half did not, so which pairs collided was an accident of who wrote which line —
and the next collision would have been just as quiet.

So the mapping lives here instead, keyed on the *pair* Django has already
resolved. Two properties make the class of bug go away rather than this one
instance of it, and `config/tests.py::TestTheSidebarMarksOnePage` holds both:

  * the sets are pairwise disjoint, so no URL can ever mark two entries;
  * every pair named here exists in the URLconf, so a renamed route fails a test
    instead of silently marking nothing.

`accounts:*` (the User Access link) is deliberately absent: it sits in the
sidebar footer, outside the nav, and has never been marked. So is
`timing:simulator`, which renders standalone without the app shell.
"""

# Sidebar entry id -> the (app_name, url_name) pairs that make it the current
# page. Dotted ids are nested under the parent named before the dot.
ITEMS = {
    "dashboard": {("timing", "dashboard")},

    "setup.manage": {
        ("competitions", name)
        for name in (
            "list", "add", "delete", "select", "duplicate", "archive",
            "archived-rules",
            "type-list", "type-add", "type-settings", "type-delete",
        )
    },
    "setup.general": {("competitions", "general")},
    "setup.classes": {("competitions", "classes")},
    "setup.runorder": {("competitions", "runorder")},
    "setup.penalties": {("competitions", "penalties")},
    # Reads as a Competition Setup page though the view lives in the results app
    # — and this is the pair that used to be mistaken for timing:settings.
    "setup.results": {("results", "settings")},

    "participants": {
        ("participants", name)
        for name in ("list", "add", "edit", "delete", "check", "set-bib", "set-dsq")
    },

    "timing.manual": {("timing", "manual")},
    "timing.auto": {("timing", "auto")},
    "timing.settings": {("timing", "settings")},

    "marshal_posts": {("competitions", "marshal-posts")},

    # The Results parent links to the index, which has no sub-entry of its own;
    # the two sub-entries below are rendered once per Overall group / class, so
    # the template narrows them further by the URL's kwargs.
    "results.index": {("results", "index")},
    "results.overall": {("results", "overall")},
    "results.class": {("results", "class")},

    "backup.backup": {("transfer", "backup")},
    "backup.export": {("transfer", "export")},
    # Step 2 of the import wizard is still the Import page as far as the sidebar
    # is concerned.
    "backup.import": {("transfer", "import"), ("transfer", "review")},
}

# Parent entry -> the entries nested under it. A parent is a link too, but never
# to a page of its own: it is marked when any of its children is.
PARENTS = {
    "setup": ("setup.manage", "setup.general", "setup.classes",
              "setup.runorder", "setup.penalties", "setup.results"),
    "timing": ("timing.manual", "timing.auto", "timing.settings"),
    "results": ("results.index", "results.overall", "results.class"),
    "backup": ("backup.backup", "backup.export", "backup.import"),
}


def current(match):
    """The sidebar entries a resolved URL marks: the entry itself and, when it is
    nested, its parent. Empty for a page with no entry (or an unresolved one, as
    on an error page)."""
    if match is None:
        return frozenset()
    key = (match.app_name, match.url_name)
    active = {name for name, urls in ITEMS.items() if key in urls}
    for parent, children in PARENTS.items():
        if not active.isdisjoint(children):
            active.add(parent)
    return frozenset(active)


# The entries whose page operates on the *active* competition, as opposed to on
# the installation.
#
# One thing reads this: whether an archived event's read-only lock applies to the
# page being rendered (base.html, static/js/read_only.js). It has to, because
# "the current event is signed off" says nothing about the screen that *manages*
# competitions — and a blanket lock turned the list you archive from, the
# duplicate dialog and the device settings into dead pages the moment it worked.
#
# Absent means live, which is the safe direction here: the server-side refusal is
# what actually protects the event (apps/competitions/archiving.py), so a page
# missing from this set shows a control that gets turned away — the behaviour
# before any of this existed — rather than one nobody can use.
EVENT_SCOPED = frozenset({
    "dashboard",
    "setup.general", "setup.classes", "setup.runorder", "setup.penalties",
    "setup.results",
    "participants",
    "timing.manual", "timing.auto",
    "marshal_posts",
    "results.index", "results.overall", "results.class",
})


def context(request):
    """Template context processor: `nav_current`, which base.html asks with
    `{% if 'timing.settings' in nav_current %}`, and `nav_event_scoped` — whether
    this page is about the active competition (see EVENT_SCOPED)."""
    active = current(getattr(request, "resolver_match", None))
    return {
        "nav_current": active,
        "nav_event_scoped": bool(active & EVENT_SCOPED),
    }
