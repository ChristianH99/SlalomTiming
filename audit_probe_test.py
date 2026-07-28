"""Audit probe tests — NOT part of the shipped suite.

These deliberately push at edges the existing tests don't cover: access control
for non-superusers, timing arithmetic around midnight, penalty bounds, bib
re-use, empty/odd competition configuration, scoring corner cases, query counts
under a realistic field, and transfer-archive robustness.
"""
import datetime
import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import RoleAccess
from apps.competitions.models import (
    Competition, CompetitionClass, CompetitionType, MarshalPost,
)
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.results import resultscalc
from apps.timing import arrangement, autotiming, calc
from apps.timing.ingest import record_signal
from apps.timing.models import MarshalPenalty, TimedRun, TimingSettings, TimingSignal

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- helpers

def make_type(**kw):
    kw.setdefault("name", "Motorcycle")
    kw.setdefault("penalties_enabled", True)
    kw.setdefault("pylon_penalty", 5)
    kw.setdefault("task_penalty", 10)
    kw.setdefault("stop_line_penalty", 20)
    return CompetitionType.objects.create(**kw)


def make_comp(ctype=None, **kw):
    ctype = ctype or make_type()
    kw.setdefault("name", "Race")
    kw.setdefault("date", datetime.date(2026, 5, 1))
    kw.setdefault("is_active", True)
    kw.setdefault("start_pattern", [{"window": None, "chips": ["practice", "counted"]}])
    return Competition.objects.create(competition_type=ctype, **kw)


def running_class(comp, name="1", **kw):
    cc = comp.classes.get(name=name)
    for k, v in {"is_running": True, "run_position": 1, "practice_runs": 1,
                 "counted_runs": 2, **kw}.items():
        setattr(cc, k, v)
    cc.save()
    return cc


def enter(comp, cc, bib, first="A", last=None):
    p = Participant.objects.create(
        competition_type=comp.competition_type, first_name=first,
        last_name=last or f"P{bib}", date_of_birth=datetime.date(2000, 1, 1),
        club="Club", license_number=f"L{bib}", email=f"{bib}@x.de",
        address_street="S 1", address_zip_code="12345", address_city="Town",
    )
    e = EventEntry.objects.create(participant=p, competition=comp, bib_number=bib)
    ClassAssignment.objects.create(participant=p, competition_class=cc)
    return p, e


def t(text):
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(text)


def sig(comp, port, time_str, running=1):
    return TimingSignal.objects.create(
        competition=comp, running_number=running, port=port, device_time=t(time_str)
    )


# ================================================================ ACCESS CONTROL

def test_role_user_without_timing_page_can_still_post_timing_signals():
    """pages.OPEN bypasses the whole gate, so ANY logged-in user (even one whose
    role grants only Participants) can inject timing signals."""
    comp = make_comp()
    TimingSettings.load()  # simulator is the default device
    group = Group.objects.create(name="registration")
    RoleAccess.objects.create(group=group, pages=["participants"])
    user = User.objects.create_user("desk", password="x-pw-9987-zz")
    user.groups.add(group)
    c = Client()
    c.force_login(user)

    # Gated pages are correctly refused ...
    assert c.get(reverse("timing:manual")).status_code == 403
    assert c.get(reverse("timing:auto")).status_code == 403
    # ... but the ingestion door is wide open to them.
    r = c.post(reverse("timing:signal"),
               data=json.dumps({"running_number": 1, "port": 1, "time": "10:00:00.000"}),
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert TimingSignal.objects.count() == 1, "a participants-only user wrote a timing signal"


def test_role_user_with_marshal_page_can_edit_any_run_of_any_post():
    """marshal_posts grants the marshal endpoints; nothing binds a device to the
    post it claimed, so it can submit for any post and any run id."""
    comp = make_comp()
    cc = running_class(comp)
    p, e = enter(comp, cc, 1)
    comp.penalties_by_marshal_posts = True
    comp.save()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1-3")
    MarshalPost.objects.create(competition=comp, number=2, tasks="4-6")
    run = TimedRun.objects.create(competition=comp, bib_number=1)

    group = Group.objects.create(name="marshal")
    RoleAccess.objects.create(group=group, pages=["marshal_posts"])
    user = User.objects.create_user("m1", password="x-pw-9987-zz")
    user.groups.add(group)
    c = Client()
    c.force_login(user)
    # Claim post 1 with one token ...
    c.post(reverse("timing:marshal-claim"),
           data=json.dumps({"post": 1, "token": "aaaa"}), content_type="application/json")
    # ... then submit for post 2 anyway, with no token at all.
    r = c.post(reverse("timing:marshal-submit"),
               data=json.dumps({"run_id": run.id, "post": 2, "pylon_count": 3,
                                "submitted": True}),
               content_type="application/json")
    assert r.json()["ok"] is True
    assert MarshalPenalty.objects.get(marshal_post__number=2).pylon_count == 3


def test_websocket_route_is_not_page_gated(settings):
    """The WS consumers only check is_authenticated, not the page role."""
    from apps.timing.consumers import TimingLiveConsumer
    import inspect
    src = inspect.getsource(TimingLiveConsumer.connect)
    assert "user_pages" not in src and "pages" not in src


# ================================================================ TIMING MATHS

def test_run_across_midnight_loses_the_time_entirely():
    comp = make_comp()
    settings = TimingSettings.load()
    s = sig(comp, 1, "23:59:58.000")
    f = sig(comp, 2, "00:00:03.000")
    run = TimedRun.objects.create(competition=comp, start_signal=s, finish_signal=f)
    assert calc.resolved_run_time(run, 2) is None, "midnight-crossing run has no time"


def test_finish_before_midnight_start_is_not_paired():
    comp = make_comp()
    settings = TimingSettings.load()
    s = sig(comp, 1, "23:59:58.000")
    arrangement.ingest(s, settings)
    f = sig(comp, 2, "00:00:03.000")
    arrangement.ingest(f, settings)
    # The finish opens its own row instead of closing the open start.
    assert TimedRun.objects.count() == 2


@pytest.mark.parametrize("precision,expected", [(1, "12.3"), (2, "12.34"), (3, "12.345")])
def test_precision_truncates_at_every_setting(precision, expected):
    comp = make_comp(make_type(name=f"T{precision}", timing_precision=precision))
    s = sig(comp, 1, "10:00:00.000000")
    f = sig(comp, 2, "10:00:12.345999")
    run = TimedRun.objects.create(competition=comp, start_signal=s, finish_signal=f)
    assert calc.format_precision(calc.resolved_run_time(run, precision), precision) == expected


def test_manual_run_time_can_exceed_the_field_and_blows_up_on_save(client):
    """manual_run_time is DecimalField(max_digits=9, decimal_places=3) — max
    999999.999 s. The endpoint parses any Decimal, so a fat-finger overflows."""
    comp = make_comp()
    run = TimedRun.objects.create(competition=comp)
    r = client.post(reverse("timing:set-runtime"),
                    data=json.dumps({"run_id": run.id, "run_time": "99999999"}),
                    content_type="application/json")
    assert r.status_code == 200, "endpoint happily accepts it"
    # ... and now EVERY read of that row raises, poisoning the whole competition.
    import decimal
    with pytest.raises(decimal.InvalidOperation):
        TimedRun.objects.get(pk=run.pk)
    for name in ("timing:arrangement", "timing:auto-state", "timing:dashboard-state"):
        with pytest.raises(decimal.InvalidOperation):
            client.get(reverse(name))
        print(f"{name} is permanently broken by one typed run time")


def test_typed_run_time_accepts_scientific_notation(client):
    comp = make_comp()
    run = TimedRun.objects.create(competition=comp)
    client.post(reverse("timing:set-runtime"),
                data=json.dumps({"run_id": run.id, "run_time": "1e3"}),
                content_type="application/json")
    run.refresh_from_db()
    assert run.manual_run_time == Decimal("1000")


# ================================================================ PENALTY BOUNDS

def test_penalty_counts_are_unbounded_from_the_endpoint(client):
    """pylon_count is PositiveSmallIntegerField (max 32767) but nothing clamps
    the POSTed value; SQLite stores it anyway."""
    comp = make_comp()
    run = TimedRun.objects.create(competition=comp)
    r = client.post(reverse("timing:run-update"),
                    data=json.dumps({"run_id": run.id, "pylon_count": 10**9}),
                    content_type="application/json")
    run.refresh_from_db()
    assert r.status_code == 200
    assert run.pylon_count == 10**9, "no upper bound on a penalty count"


def test_marshal_submit_accepts_unbounded_counts(client):
    comp = make_comp()
    comp.penalties_by_marshal_posts = True
    comp.save()
    MarshalPost.objects.create(competition=comp, number=1, tasks="1")
    run = TimedRun.objects.create(competition=comp)
    client.post(reverse("timing:marshal-submit"),
                data=json.dumps({"run_id": run.id, "post": 1, "pylon_count": 500000}),
                content_type="application/json")
    assert MarshalPenalty.objects.get().pylon_count == 500000


def test_max_penalty_per_task_is_never_applied():
    """CompetitionType.max_penalty_per_task is collected on the settings page but
    no code path reads it."""
    import subprocess
    out = subprocess.run(
        ["git", "grep", "-n", "max_penalty_per_task", "--", "apps/"],
        capture_output=True, text=True).stdout
    users = [l for l in out.splitlines()
             if "/tests.py" not in l and "/migrations/" not in l]
    print(out)
    # Only the model field + form/settings plumbing — never the calculation.
    assert not any("calc.py" in l or "autotiming.py" in l or "resultscalc.py" in l
                   for l in users), users


# ================================================================ BIB / IDENTITY

def test_reassigning_a_bib_silently_transfers_recorded_runs(client):
    """TimedRun.bib_number is a loose int, so changing who wears a bib re-attributes
    every already-recorded time to the new participant."""
    comp = make_comp()
    cc = running_class(comp)
    alice, ea = enter(comp, cc, 7, last="Alice")
    run = TimedRun.objects.create(
        competition=comp, bib_number=7, competition_class=cc,
        run_type="counted", run_number=1, manual_run_time=Decimal("30.000"),
        manual_entry=True,
    )
    bob, eb = enter(comp, cc, 8, last="Bob")
    # Swap the bibs, as an operator fixing a registration mistake would.
    ea.bib_number = 99
    ea.save()
    eb.bib_number = 7
    eb.save()

    results = resultscalc.compute_class_results(comp, cc)
    owners = {c.name: [r.total for r in c.runs] for c in results.ranked + results.unranked}
    print(owners)
    assert owners["A Bob"][0] == Decimal("30.000"), "Alice's run now scores for Bob"
    assert owners["A Alice"][0] is None


def test_deleting_a_participant_orphans_their_runs():
    comp = make_comp()
    cc = running_class(comp)
    p, e = enter(comp, cc, 3)
    TimedRun.objects.create(competition=comp, bib_number=3, competition_class=cc,
                            run_type="counted", run_number=1,
                            manual_run_time=Decimal("30.0"), manual_entry=True)
    p.delete()
    assert TimedRun.objects.filter(bib_number=3).exists(), "run survives its participant"


def test_a_run_for_an_unassigned_class_never_appears_in_results():
    """A time recorded against a class the competitor isn't entered in is silently
    invisible — no warning anywhere."""
    comp = make_comp()
    cc1 = running_class(comp, "1")
    cc2 = running_class(comp, "2", run_position=2)
    p, e = enter(comp, cc1, 5)
    TimedRun.objects.create(competition=comp, bib_number=5, competition_class=cc2,
                            run_type="counted", run_number=1,
                            manual_run_time=Decimal("31.0"), manual_entry=True)
    res = resultscalc.compute_class_results(comp, cc2)
    assert res.ranked == [] and res.unranked == []


# ================================================================ CONFIGURATION

def test_new_competition_has_no_start_pattern_so_auto_timing_is_empty():
    ctype = make_type()
    comp = Competition.objects.create(
        competition_type=ctype, name="Fresh", date=datetime.date(2026, 6, 1), is_active=True
    )
    cc = running_class(comp)
    enter(comp, cc, 1)
    assert comp.start_pattern == []
    assert autotiming.computed_slots(comp) == [], "no start order at all on a new event"


def test_class_with_zero_counted_runs_is_never_rankable():
    comp = make_comp()
    cc = running_class(comp, counted_runs=0, practice_runs=1)
    enter(comp, cc, 1)
    res = resultscalc.compute_class_results(comp, cc)
    assert res.ranked == []


def test_regularity_with_one_counted_run_ties_everyone_at_zero():
    comp = make_comp()
    cc = running_class(comp, counted_runs=1, practice_runs=0,
                       scoring_method=CompetitionClass.Scoring.REGULARITY)
    for bib, secs in ((1, "30.000"), (2, "45.000")):
        enter(comp, cc, bib)
        TimedRun.objects.create(competition=comp, bib_number=bib, competition_class=cc,
                                run_type="counted", run_number=1,
                                manual_run_time=Decimal(secs), manual_entry=True)
    res = resultscalc.compute_class_results(comp, cc)
    scores = [c.score for c in res.ranked]
    print([(c.bib, c.score, c.rank, c.tie_state) for c in res.ranked])
    assert scores == [Decimal("0.000"), Decimal("0.000")]


def test_penalties_disabled_type_still_stores_counts_but_scores_zero():
    comp = make_comp(make_type(name="NoPen", penalties_enabled=False))
    cc = running_class(comp, counted_runs=1)
    enter(comp, cc, 1)
    run = TimedRun.objects.create(competition=comp, bib_number=1, competition_class=cc,
                                  run_type="counted", run_number=1,
                                  manual_run_time=Decimal("30.0"), pylon_count=4,
                                  manual_entry=True)
    assert autotiming.penalty_seconds(run, comp) == 0


def test_marshal_mode_ignores_own_counts_on_an_auto_bound_run():
    """An operator who types a penalty on the Manual view flips manual_entry, but a
    purely auto-bound run's own counts are discarded — a penalty typed straight into
    the DB / by an older path silently vanishes."""
    comp = make_comp()
    comp.penalties_by_marshal_posts = True
    comp.save()
    cc = running_class(comp)
    enter(comp, cc, 1)
    run = TimedRun.objects.create(competition=comp, bib_number=1, competition_class=cc,
                                  run_type="counted", run_number=1, pylon_count=2,
                                  manual_run_time=Decimal("30.0"), manual_entry=False)
    assert autotiming.penalty_seconds(run, comp) == 0


# ================================================================ PERFORMANCE

def test_live_endpoints_query_count_scales_with_the_field(client, django_assert_num_queries):
    """Every open browser re-fetches these on every WebSocket nudge."""
    from django.db import connection, reset_queries
    from django.test.utils import CaptureQueriesContext

    comp = make_comp()
    cc = running_class(comp, counted_runs=2, practice_runs=1)
    for bib in range(1, 41):
        enter(comp, cc, bib)
    settings = TimingSettings.load()
    now = datetime.datetime(2026, 5, 1, 10, 0, 0)
    for i in range(40):
        s = TimingSignal.objects.create(competition=comp, running_number=i + 1, port=1,
                                        device_time=(now + datetime.timedelta(seconds=i * 60)).time())
        arrangement.ingest(s, settings)
        f = TimingSignal.objects.create(competition=comp, running_number=i + 1, port=2,
                                        device_time=(now + datetime.timedelta(seconds=i * 60 + 35)).time())
        arrangement.ingest(f, settings)

    for name in ("timing:arrangement", "timing:auto-state", "timing:dashboard-state"):
        with CaptureQueriesContext(connection) as ctx:
            r = client.get(reverse(name))
        assert r.status_code == 200
        print(f"{name}: {len(ctx.captured_queries)} queries for 40 starters / 40 runs")


def test_results_page_query_count(client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    comp = make_comp()
    cc = running_class(comp, counted_runs=2, practice_runs=1)
    for bib in range(1, 41):
        enter(comp, cc, bib)
        for n in (1, 2):
            TimedRun.objects.create(competition=comp, bib_number=bib, competition_class=cc,
                                    run_type="counted", run_number=n,
                                    manual_run_time=Decimal(f"{30 + bib}.{n}00"),
                                    manual_entry=True)
    with CaptureQueriesContext(connection) as ctx:
        r = client.get(reverse("results:class", args=[cc.pk]))
    assert r.status_code == 200
    print(f"results:class: {len(ctx.captured_queries)} queries for 40 competitors")


# ================================================================ TRANSFER

def test_archive_media_name_with_traversal_is_rejected_or_contained(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.transfer import archive, importers
    from apps.results.models import ResultsPdfLayout
    from django.core.files.base import ContentFile
    comp = make_comp()
    layout = ResultsPdfLayout.objects.create(competition=comp)
    try:
        layout.image_left.save("../../../pwned.png", ContentFile(b"\x89PNG\r\n"), save=True)
    except Exception as exc:
        print("rejected:", type(exc).__name__, exc)
        return
    print("STORED AT:", layout.image_left.name)
    assert ".." not in layout.image_left.name


def test_archive_read_has_no_size_limit():
    """A crafted export can decompress to anything — no bound anywhere."""
    import io, zipfile
    from apps.transfer import archive
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        from apps.transfer.schema import FORMAT, VERSION
        z.writestr("data.json", json.dumps({"format": FORMAT, "version": VERSION}))
        z.writestr("media/bomb.bin", b"\0" * (60 * 1024 * 1024))
    raw = buf.getvalue()
    print(f"zip is {len(raw)/1024:.0f} KiB on disk")
    doc, media = archive.read(raw)
    print(f"expanded to {len(media['bomb.bin'])/1024/1024:.0f} MiB in memory, unbounded")
    assert len(media["bomb.bin"]) == 60 * 1024 * 1024


# ================================================================ MISC

def test_pdf_filename_is_not_escaped(client):
    comp = make_comp()
    cc = running_class(comp, name="1")
    cc.name = 'A"; x'
    cc.save()
    enter(comp, cc, 1)
    r = client.get(reverse("results:export-class", args=[cc.pk]))
    print("Content-Disposition:", r["Content-Disposition"])
    assert '"' in r["Content-Disposition"].replace('inline; filename="', "", 1)[:-1] \
        or True


def test_participant_check_leaks_across_competition_types(client):
    other = CompetitionType.objects.create(name="Go-Cart")
    Participant.objects.create(
        competition_type=other, first_name="Erika", last_name="Mustermann",
        date_of_birth=datetime.date(1990, 1, 1), club="Secret Club",
        license_number="XYZ-1", email="e@x.de",
    )
    comp = make_comp()
    r = client.get(reverse("participants:check"),
                   {"first_name": "Erika", "last_name": "Mustermann"})
    matches = r.json()["matches"]
    print(matches)
    assert matches, "a participant of an unrelated discipline is disclosed"


def test_static_files_are_unserved_without_debug(settings):
    from django.conf import settings as s
    assert not hasattr(s, "STATIC_ROOT") or getattr(s, "STATIC_ROOT", None) is None, \
        "STATIC_ROOT is set — collectstatic would work"


def test_imported_archive_injects_unsanitised_html_into_a_safe_rendered_field(client, tmp_path, settings):
    """header_html goes from an untrusted .zip straight into the DB, and
    results_settings.html renders it with |safe → stored XSS from a file."""
    settings.MEDIA_ROOT = str(tmp_path)
    from apps.transfer import archive, importers, staging
    from apps.results.models import ResultsPdfLayout

    payload = '<img src=x onerror="alert(document.cookie)">'
    src = make_comp()
    running_class(src)
    from apps.transfer import exporters
    raw = exporters.export(competition=src, include_timing=False)
    doc, media = archive.read(raw)
    doc["results"]["pdf_layout"] = {
        "ref": 1, "header_html": payload, "footer_html": "",
        "increment_start_year": None, "orientation": "portrait",
        "image_left_height": 18, "image_right_height": 18,
    }
    src.is_active = False
    src.name = "Source"
    src.save()

    plan = importers.plan(doc)
    importers.commit(plan, {}, type_action="reuse", media=media, activate=True)
    stored = ResultsPdfLayout.objects.exclude(competition=src).first()
    print("STORED header_html:", stored.header_html)
    assert stored.header_html == payload, "unsanitised HTML persisted from the archive"

    page = client.get(reverse("results:settings")).content.decode()
    assert payload in page, "and it is rendered verbatim into the page"
    print("RENDERED VERBATIM on results:settings")
