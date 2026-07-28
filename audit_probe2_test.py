"""Audit probes, round 2 — written from reading the code, to confirm or refute
specific suspected defects. NOT part of the shipped suite.

Each test names the hypothesis in its docstring. A test that FAILS here is a
confirmed bug (the assertion states the *correct* behaviour); a test that passes
refutes the hypothesis.
"""
import datetime
import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import RoleAccess
from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.timing import calc
from apps.timing.models import TimedRun, TimingSettings, TimingSignal

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------- helpers

def make_type(**kw):
    kw.setdefault("name", "Motorcycle")
    kw.setdefault("penalties_enabled", True)
    kw.setdefault("pylon_penalty", 5)
    kw.setdefault("task_penalty", 10)
    kw.setdefault("stop_line_penalty", 20)
    return CompetitionType.objects.create(**kw)


def make_competition(ctype=None, **kw):
    ctype = ctype or make_type()
    kw.setdefault("name", "Test Slalom")
    kw.setdefault("date", datetime.date(2026, 7, 1))
    kw.setdefault("is_active", True)
    return Competition.objects.create(competition_type=ctype, **kw)


def running_class(competition, name="1", **kw):
    cc = competition.classes.get(name=name)
    cc.is_running = True
    cc.run_position = kw.pop("run_position", 0)
    for k, v in kw.items():
        setattr(cc, k, v)
    cc.save()
    return cc


def add_starter(competition, cc, bib, first="A", last="B", **pkw):
    p = Participant.objects.create(
        competition_type=competition.competition_type,
        first_name=first, last_name=last,
        date_of_birth=datetime.date(2000, 1, 1), **pkw,
    )
    EventEntry.objects.create(participant=p, competition=competition, bib_number=bib)
    ClassAssignment.objects.create(participant=p, competition_class=cc)
    return p


def scoped_client(pages):
    """A logged-in client whose role grants only `pages`."""
    user = User.objects.create_user(username=f"scoped-{'-'.join(pages) or 'none'}", password="x")
    group = Group.objects.create(name=f"role-{'-'.join(pages) or 'none'}")
    RoleAccess.objects.create(group=group, pages=list(pages))
    user.groups.add(group)
    c = Client()
    c.force_login(user)
    return c


# ============================================================ A. HTTP header injection / crash

def test_pdf_export_filename_survives_a_quote_in_the_class_name(client):
    """CLAUDE.md claims SEC-6 is fixed with filename*=UTF-8''… — verify the class
    name can no longer break Content-Disposition apart."""
    comp = make_competition()
    cc = running_class(comp, "1")
    cc.name = 'A"; attachment; filename="evil.pdf'
    cc.save()
    resp = client.get(reverse("results:export-class", args=[cc.pk]))
    assert resp.status_code == 200
    cd = resp["Content-Disposition"]
    # The raw quote must not appear unescaped inside the header.
    assert cd.count('filename') == 1, f"header split apart: {cd!r}"


def test_pdf_export_filename_survives_a_non_latin1_class_name(client):
    """A class named in Cyrillic/Greek: a raw filename= header is latin-1 encoded
    by Django and would raise. RFC 5987 encoding is required."""
    comp = make_competition()
    cc = running_class(comp, "1")
    cc.name = "Кла"          # non-latin-1
    cc.save()
    resp = client.get(reverse("results:export-class", args=[cc.pk]))
    assert resp.status_code == 200


def test_export_archive_filename_survives_a_non_latin1_competition_name(client):
    """Same for the transfer export's Content-Disposition."""
    comp = make_competition(name="Слалом")
    resp = client.post(reverse("transfer:export"), {"target": "competition", "pk": comp.pk})
    assert resp.status_code == 200


# ============================================================ B. 500s from unvalidated ids

@pytest.mark.parametrize("url_name,payload", [
    ("timing:run-update", {"run_id": "abc"}),
    ("timing:run-status", {"run_id": "abc", "status": "dnf"}),
    ("timing:ignore", {"signal_id": "abc", "ignored": True}),
    ("timing:pair", {"signal_id": "abc", "run_id": "abc", "slot": "start"}),
    ("timing:set-time", {"run_id": "abc", "slot": "start", "time": "00:00:01.000"}),
    ("timing:set-runtime", {"run_id": "abc", "run_time": "12.5"}),
    ("timing:run-delete", {"run_id": "abc"}),
    ("timing:auto-adjust", {"run_id": "abc", "pylon_adjust": 1}),
    ("timing:marshal-submit", {"run_id": "abc", "post": 1}),
])
def test_timing_endpoints_reject_a_non_numeric_id_without_500(client, url_name, payload):
    """A client sending a non-numeric id must get a 4xx, not an unhandled
    ValueError from Django's int coercion."""
    make_competition()
    resp = client.post(reverse(url_name), data=json.dumps(payload),
                       content_type="application/json")
    assert resp.status_code < 500, f"{url_name} 500s on a non-numeric id"


def test_run_update_rejects_a_non_numeric_class_key_without_500(client):
    """_parse_class_key filters CompetitionClass on a raw string pk."""
    comp = make_competition()
    run = TimedRun.objects.create(competition=comp)
    resp = client.post(
        reverse("timing:run-update"),
        data=json.dumps({"run_id": run.id, "class_key": "abc:0"}),
        content_type="application/json",
    )
    assert resp.status_code < 500


def test_run_update_rejects_a_unicode_digit_bib_without_500(client):
    """_as_positive_int uses str.isdigit(), which is True for '²' while int('²')
    raises."""
    comp = make_competition()
    run = TimedRun.objects.create(competition=comp)
    resp = client.post(
        reverse("timing:run-update"),
        data=json.dumps({"run_id": run.id, "bib_number": "²"}),
        content_type="application/json",
    )
    assert resp.status_code < 500


def test_penalty_count_accepts_a_unicode_digit_without_500(client):
    """_as_count has the same isdigit()/int() mismatch."""
    comp = make_competition()
    run = TimedRun.objects.create(competition=comp)
    resp = client.post(
        reverse("timing:run-update"),
        data=json.dumps({"run_id": run.id, "pylon_count": "²"}),
        content_type="application/json",
    )
    assert resp.status_code < 500


def test_participant_set_bib_rejects_a_non_numeric_participant_without_500(client):
    make_competition()
    resp = client.post(reverse("participants:set-bib"),
                       data=json.dumps({"participant": "abc", "bib": "1"}),
                       content_type="application/json")
    assert resp.status_code < 500


def test_timing_signal_bounds_the_running_number(client):
    """running_number is a PositiveIntegerField; SQLite will happily take 10**30
    (or raise OverflowError). The endpoint should refuse it."""
    make_competition()
    TimingSettings.load()
    resp = client.post(
        reverse("timing:signal"),
        data=json.dumps({"running_number": 10 ** 30, "port": 1, "time": "10:00:00.000"}),
        content_type="application/json",
    )
    assert resp.status_code < 500
    assert not TimingSignal.objects.filter(running_number__gt=2 ** 31).exists()


# ============================================================ C. access control

def test_a_results_only_role_cannot_read_the_live_timing_arrangement():
    """The shared `client` fixture is a superuser, so role scoping is untested.
    A Results-only role must not reach the Timing endpoints."""
    make_competition()
    c = scoped_client(["results"])
    assert c.get(reverse("timing:arrangement")).status_code == 403


def test_a_marshal_only_role_cannot_lock_a_post():
    comp = make_competition()
    MarshalPost.objects.create(competition=comp, number=1)
    run = TimedRun.objects.create(competition=comp)
    c = scoped_client(["marshal_posts"])
    resp = c.post(reverse("timing:marshal-lock"),
                  data=json.dumps({"run_id": run.id, "post": 1}),
                  content_type="application/json")
    assert resp.status_code == 403


def test_a_participants_only_role_cannot_post_a_timing_signal():
    make_competition()
    TimingSettings.load()
    c = scoped_client(["participants"])
    resp = c.post(reverse("timing:signal"),
                  data=json.dumps({"running_number": 1, "port": 1, "time": "10:00:00.000"}),
                  content_type="application/json")
    assert resp.status_code == 403


def test_uploaded_media_is_not_world_readable(settings, tmp_path):
    """Everything in this app requires a login; /media/ is exempted by the
    middleware, so an uploaded file is fetchable by anyone on the network."""
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "leak.txt").write_text("secret")
    anon = Client()
    resp = anon.get("/media/leak.txt")
    assert resp.status_code in (302, 403, 404), "media served to an anonymous client"


def test_a_non_superuser_gets_403_not_a_login_redirect_on_the_accounts_endpoints():
    """_superuser_required redirects an authenticated non-superuser to the login
    page, which is a confusing loop rather than a refusal."""
    c = scoped_client(["dashboard"])
    resp = c.post(reverse("accounts:user-create"), {"username": "x", "password": "Zx9!qwerty"})
    assert resp.status_code == 403
    assert not User.objects.filter(username="x").exists()


# ============================================================ D. privacy defaults

def test_results_columns_do_not_default_to_showing_contact_details(client):
    """With no saved ResultColumnSettings, general_columns() returns *every*
    available column — so a printed result sheet carries e-mail, phone and the
    home address of every competitor by default."""
    from apps.results.models import ResultColumnSettings
    comp = make_competition()
    running_class(comp, "1")
    cols = ResultColumnSettings.general_columns(comp)
    assert "email" not in cols and "phone" not in cols and "street" not in cols, cols


def test_duplicate_check_does_not_leak_participants_of_another_type(client):
    """SEC-7: participant_check searches Participant.objects.all()."""
    other = CompetitionType.objects.create(name="Go-Cart")
    Participant.objects.create(competition_type=other, first_name="Erika",
                               last_name="Mustermann",
                               date_of_birth=datetime.date(1990, 5, 5),
                               license_number="SECRET-1", club="Geheimclub")
    make_competition()   # active type = Motorcycle
    resp = client.get(reverse("participants:check"), {"license_number": "SECRET-1"})
    assert resp.json()["matches"] == [], resp.json()


# ============================================================ E. timing arithmetic

def test_a_run_that_crosses_midnight_still_produces_a_time():
    """calc.run_time works on seconds-since-midnight, so 23:59:59 → 00:00:02 is
    negative and silently yields no time at all."""
    start = datetime.time(23, 59, 59)
    finish = datetime.time(0, 0, 2)
    assert calc.run_time(start, finish, 3) == Decimal("3.000")


def test_format_clock_never_rounds_a_time_up():
    """format_clock formats the fractional part with %.Nf, which rounds. It is
    documented as 'assumed already truncated' but is fed sums elsewhere."""
    assert calc.format_clock(Decimal("12.9996"), 3) == "00:12.999"


# ============================================================ F. data integrity

def test_only_one_competition_can_be_active_at_a_time():
    """Nothing at the DB level enforces it; get_current() silently picks one."""
    ctype = make_type()
    make_competition(ctype, name="A")
    make_competition(ctype, name="B")
    assert Competition.objects.filter(is_active=True).count() == 1


def test_a_signal_arriving_with_no_active_competition_is_still_reachable():
    """record_signal stores it with competition=None and never places it: the
    time exists in the database but no screen in the app can ever show it."""
    from apps.timing.ingest import record_signal
    make_type()
    TimingSettings.load()
    signal = record_signal(1, 1, False, datetime.time(10, 0, 0))
    assert signal is not None
    # Now a competition is selected — the time should be recoverable.
    comp = make_competition()
    reachable = TimingSignal.objects.filter(competition=comp).exists()
    assert reachable, "a time recorded before a competition was picked is orphaned"


def test_changing_the_competition_type_asks_before_deleting_registrations(client):
    """GeneralView deletes every EventEntry of a foreign type with no
    confirmation — only an after-the-fact message."""
    ctype = make_type()
    other = CompetitionType.objects.create(name="Go-Cart")
    comp = make_competition(ctype)
    cc = running_class(comp, "1")
    add_starter(comp, cc, 1)
    assert EventEntry.objects.filter(competition=comp).count() == 1
    client.post(reverse("competitions:general"), {
        "competition_type": other.pk, "name": comp.name, "date": "2026-07-01",
        "assignment_method": "manual",
    })
    assert EventEntry.objects.filter(competition=comp).count() == 1, \
        "registrations deleted without confirmation"


def test_creating_a_competition_does_not_silently_steal_the_active_one(client):
    """select_competition confirms when others are signed in; creating one just
    takes over."""
    ctype = make_type()
    running = make_competition(ctype, name="Live event")
    # Someone else is signed in.
    other = User.objects.create_user(username="marshal", password="x")
    Client().force_login(other)
    client.post(reverse("competitions:add"), {
        "competition_type": ctype.pk, "name": "New", "date": "2026-08-01",
        "assignment_method": "manual",
    })
    running.refresh_from_db()
    assert running.is_active, "creating a competition switched the live event with no warning"


def test_two_desks_cannot_both_assign_the_same_bib(client):
    """The uniqueness check is a read-then-write; the DB constraint turns the
    loser into an unhandled IntegrityError."""
    comp = make_competition()
    cc = running_class(comp, "1")
    p1 = add_starter(comp, cc, 1, first="A", last="One")
    p2 = Participant.objects.create(competition_type=comp.competition_type,
                                    first_name="B", last_name="Two",
                                    date_of_birth=datetime.date(2000, 1, 1))
    # Simulate the race: the conflict check passes, then the row appears.
    from django.db import IntegrityError
    try:
        EventEntry.objects.create(participant=p2, competition=comp, bib_number=1)
    except IntegrityError:
        pytest.skip("constraint works; the question is whether the view handles it")
    assert False


# ============================================================ G. imports / uploads

def test_an_imported_logo_cannot_plant_an_html_file_in_media(settings, tmp_path):
    """The importer writes media straight from the archive with no image check,
    and /media/ is served unauthenticated with a filename-derived content type."""
    settings.MEDIA_ROOT = tmp_path
    from apps.transfer import archive, importers

    ctype_row = {"ref": 1, "name": "Motorcycle"}
    document = {
        "format": "slalomtiming-export", "version": 1, "scope": "event",
        "competition_type": ctype_row,
        "competition": {"ref": 1, "name": "Imported", "date": "2026-07-01",
                        "assignment_method": "manual", "start_pattern": [],
                        "auto_timing_order": []},
        "classes": [], "participants": [], "entries": [], "class_assignments": [],
        "marshal_posts": [], "timing": {},
        "results": {"pdf_layout": {"ref": 1, "header_html": "", "footer_html": "",
                                   "orientation": "portrait", "image_left": "evil.html"}},
    }
    raw = archive.write(document, {"evil.html": b"<script>alert(1)</script>"})
    doc, media = archive.read(raw)
    plan = importers.plan(doc)
    importers.commit(plan, resolutions={}, media=media)
    planted = list(tmp_path.rglob("*.html"))
    assert not planted, f"import planted a servable HTML file: {planted}"


def test_an_import_with_a_traversing_media_name_is_refused_cleanly(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    from apps.transfer import archive, importers
    from apps.transfer.schema import TransferError

    document = {
        "format": "slalomtiming-export", "version": 1, "scope": "event",
        "competition_type": {"ref": 1, "name": "Motorcycle"},
        "competition": {"ref": 1, "name": "Imported", "date": "2026-07-01",
                        "assignment_method": "manual", "start_pattern": [],
                        "auto_timing_order": []},
        "classes": [], "participants": [], "entries": [], "class_assignments": [],
        "marshal_posts": [], "timing": {},
        "results": {"pdf_layout": {"ref": 1, "header_html": "", "footer_html": "",
                                   "orientation": "portrait",
                                   "image_left": "../../../../pwned.png"}},
    }
    raw = archive.write(document, {"../../../../pwned.png": b"\x89PNG"})
    doc, media = archive.read(raw)
    plan = importers.plan(doc)
    try:
        importers.commit(plan, resolutions={}, media=media)
    except TransferError:
        pass  # a clean refusal is fine
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"traversing media name raised {type(exc).__name__}: {exc}")


def test_an_import_with_two_general_column_rows_is_refused_cleanly(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    from apps.transfer import archive, importers
    from apps.transfer.schema import TransferError

    document = {
        "format": "slalomtiming-export", "version": 1, "scope": "event",
        "competition_type": {"ref": 1, "name": "Motorcycle"},
        "competition": {"ref": 1, "name": "Imported", "date": "2026-07-01",
                        "assignment_method": "manual", "start_pattern": [],
                        "auto_timing_order": []},
        "classes": [], "participants": [], "entries": [], "class_assignments": [],
        "marshal_posts": [], "timing": {},
        "results": {"columns": [
            {"ref": 1, "columns": [], "show_overall": True},
            {"ref": 2, "columns": [], "show_overall": False},
        ]},
    }
    raw = archive.write(document, {})
    doc, media = archive.read(raw)
    plan = importers.plan(doc)
    try:
        importers.commit(plan, resolutions={}, media=media)
    except TransferError:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"duplicate General row raised {type(exc).__name__}: {exc}")


def test_marshal_detail_json_is_size_bounded(client):
    """A marshal's phone posts `detail` straight into a JSONField with no cap."""
    comp = make_competition(penalties_by_marshal_posts=True)
    MarshalPost.objects.create(competition=comp, number=1, tasks="1")
    run = TimedRun.objects.create(competition=comp)
    huge = {"tasks": {str(i): {"pylons": 1} for i in range(50000)}}
    resp = client.post(reverse("timing:marshal-submit"),
                       data=json.dumps({"run_id": run.id, "post": 1, "detail": huge}),
                       content_type="application/json")
    from apps.timing.models import MarshalPenalty
    mp = MarshalPenalty.objects.filter(timed_run=run).first()
    assert mp is None or len(json.dumps(mp.detail)) < 100_000, \
        "an unbounded blob was stored on the run"


# ============================================================ H. performance

def test_age_based_assignment_does_not_query_per_participant(django_assert_num_queries):
    """AgeAssignment.classes_for calls competition.class_for_birth_year, which
    runs a query — inside a loop over every entry in starters_by_class()."""
    comp = make_competition(assignment_method="age")
    cc = running_class(comp, "1", age_from=0, age_to=99)
    for i in range(1, 31):
        p = Participant.objects.create(
            competition_type=comp.competition_type, first_name=f"P{i}",
            last_name="X", date_of_birth=datetime.date(2000, 1, 1))
        EventEntry.objects.create(participant=p, competition=comp, bib_number=i)
    comp = Competition.objects.get(pk=comp.pk)
    with django_assert_num_queries(5):
        comp.starters_by_class()


def test_live_auto_state_cost_is_flat_under_age_based_assignment(client, django_assert_num_queries):
    comp = make_competition(assignment_method="age")
    running_class(comp, "1", age_from=0, age_to=99)
    for i in range(1, 41):
        p = Participant.objects.create(
            competition_type=comp.competition_type, first_name=f"P{i}",
            last_name="X", date_of_birth=datetime.date(2000, 1, 1))
        EventEntry.objects.create(participant=p, competition=comp, bib_number=i)
    with django_assert_num_queries(30):
        client.get(reverse("timing:auto-state"))


# ============================================================ I. logging / audit

def test_a_successful_login_is_recorded_somewhere(settings, caplog):
    """throttle.note_success logs at INFO, but the project configures no LOGGING,
    so Python's last-resort handler drops anything below WARNING — there is no
    record of who signed in."""
    import logging
    User.objects.create_user(username="op", password="Zx9!qwerty-long")
    root = logging.getLogger()
    assert any(h.level <= logging.INFO for h in root.handlers) or root.level <= logging.INFO, \
        "no handler would capture the INFO-level login record"


def test_the_project_configures_logging_to_a_file(settings):
    """Nothing persists the security log (failed logins, unrecorded times) on a
    packaged Windows install where stderr goes nowhere."""
    assert getattr(settings, "LOGGING", None), "no LOGGING configuration at all"
