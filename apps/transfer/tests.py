import datetime
import io
import zipfile
from pathlib import Path
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.competitions import archiving
from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.results.models import ManualTieResolution, ResultColumnSettings, ResultsPdfLayout
from apps.timing.autotiming import slot_key
from apps.timing.models import MarshalPenalty, TimedRun, TimingSignal

from . import archive, csvimport, exporters, importers, merge, schema, staging
from .schema import SCOPE_EVENT, SCOPE_TYPE, TransferError

pytestmark = pytest.mark.django_db

BASE = datetime.datetime(2026, 5, 1, 10, 0, 0)


# --- fixtures ----------------------------------------------------------------


def make_type(name="Motorcycle", **overrides):
    values = dict(
        penalties_enabled=True, pylon_penalty=5, task_penalty=10,
        stop_line_penalty=10, max_penalty_per_task=20,
        timing_precision=CompetitionType.Precision.THOUSANDTHS,
        requires_vehicle=True, requires_phone=False,
    )
    values.update(overrides)
    return CompetitionType.objects.create(name=name, **values)


def make_participant(ctype, first="Ada", last="Lovelace", **overrides):
    values = dict(
        date_of_birth=datetime.date(2010, 3, 4), club="Club A",
        license_number="", email="ada@example.org", vehicle="Kart 1",
    )
    values.update(overrides)
    return Participant.objects.create(
        competition_type=ctype, first_name=first, last_name=last, **values
    )


def make_event(ctype=None, with_timing=True):
    """A competition exercising every section of the document: classes, marshal
    posts, entries, assignments, timing, penalties and results config."""
    ctype = ctype or make_type()
    # Exactly one competition may be active (enforced by the database), and
    # creating one in the app makes it current — so the helper does the same.
    Competition.objects.filter(is_active=True).update(is_active=False)
    competition = Competition.objects.create(
        competition_type=ctype, name="Spring Race", date=datetime.date(2026, 5, 1),
        is_active=True, penalties_by_marshal_posts=True,
        start_pattern=[{"window": None, "chips": ["practice", "counted"]}],
    )
    competition.classes.all().delete()  # drop the seeded defaults
    cclass = CompetitionClass.objects.create(
        competition=competition, name="T1", is_running=True,
        practice_runs=1, counted_runs=2, run_position=1, position=0,
    )
    post = MarshalPost.objects.create(
        competition=competition, number=1, tasks="1, 5, 11-15", handles_stop_line=True,
        claim_token="device-abc", claim_seen=timezone.now(),
    )

    participant = make_participant(ctype)
    entry = EventEntry.objects.create(
        participant=participant, competition=competition, bib_number=7,
    )
    ClassAssignment.objects.create(participant=participant, competition_class=cclass)

    if with_timing:
        start = TimingSignal.objects.create(
            competition=competition, running_number=1, port=1, device_time=BASE.time(),
        )
        finish = TimingSignal.objects.create(
            competition=competition, running_number=1, port=2,
            device_time=(BASE + datetime.timedelta(seconds=42.5)).time(),
        )
        run = TimedRun.objects.create(
            competition=competition, start_signal=start, finish_signal=finish,
            bib_number=7, competition_class=cclass, run_type=TimedRun.RunType.COUNTED,
            run_number=1, manual_entry=True, pylon_count=2, pylon_adjust=-1,
            manual_run_time=Decimal("42.500"),
        )
        MarshalPenalty.objects.create(
            timed_run=run, marshal_post=post, pylon_count=2, submitted=True,
            detail={"tasks": {"5": {"pylons": 2}}, "stop_line": False},
        )

    ResultColumnSettings.objects.create(
        competition=competition, columns=["driver_name", "club"], show_overall=False,
    )
    ManualTieResolution.objects.create(
        competition=competition, scope=f"class:{cclass.pk}",
        members=[[entry.pk, 0, 1], [entry.pk, 1, 1]],
    )
    Competition.objects.filter(pk=competition.pk).update(
        auto_timing_order=[slot_key(entry.pk, cclass.pk, 0, "counted", 1)]
    )
    competition.refresh_from_db()
    return competition


def wipe():
    """Drop everything an export carries, so an import lands on a bare system —
    the point of the feature is moving to a *different* machine."""
    TimedRun.objects.all().delete()
    TimingSignal.objects.all().delete()
    Competition.objects.all().delete()
    Participant.objects.all().delete()
    CompetitionType.objects.all().delete()


def import_archive(payload, **kwargs):
    document, media = archive.read(payload)
    plan = importers.plan(document)
    return plan, importers.commit(plan, resolutions={}, media=media, **kwargs)


# --- archive format ----------------------------------------------------------


def test_archive_is_a_zip_holding_data_json():
    competition = make_event()
    payload = exporters.export(competition=competition)
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        assert "data.json" in bundle.namelist()


def test_reading_a_non_archive_is_reported_not_raised_raw():
    with pytest.raises(TransferError):
        archive.read(b"this is not a zip")


def test_reading_a_zip_that_is_not_ours_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("something.txt", "hello")
    with pytest.raises(TransferError):
        archive.read(buffer.getvalue())


def test_a_newer_document_version_is_refused():
    competition = make_event()
    document, _media = archive.read(exporters.export(competition=competition))
    document["version"] = 999
    with pytest.raises(TransferError) as error:
        archive.read(archive.write(document))
    assert "newer version" in str(error.value)


# --- an archive is a hand-picked file, so its size is not a promise ----


def _zip_of(name, content):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(name, content)
    return buffer.getvalue()


def test_an_over_sized_data_entry_is_refused_before_it_is_read(monkeypatch):
    """A zip declares how far each entry expands, so 60 KiB on disk can be as much
    memory as whoever wrote the file chose. The budget is checked first."""
    monkeypatch.setattr(archive, "MAX_DATA_BYTES", 4096)
    payload = _zip_of(archive.DATA_NAME, b"a" * 100_000)
    assert len(payload) < 4096          # a small file...
    with pytest.raises(TransferError) as error:
        archive.read(payload)           # ...that would have expanded far past it
    assert "too large" in str(error.value)


def test_an_over_sized_media_entry_is_refused(monkeypatch):
    monkeypatch.setattr(archive, "MAX_MEDIA_ENTRY_BYTES", 4096)
    document, _media = archive.read(exporters.export(competition=make_event()))
    payload = archive.write(document, media={"logo.png": b"\0" * 100_000})
    with pytest.raises(TransferError):
        archive.read(payload)


def test_a_lying_zip_header_is_reported_not_raised_raw():
    """file_size is the writer's claim, so it is not the only check — the data has
    to match it. An entry edited to declare a small size fails its CRC, and that has
    to reach the operator as a sentence rather than a 500."""
    payload = _zip_of(archive.DATA_NAME, b"a" * 100_000)
    # Rewrite every declared uncompressed size to something harmless.
    payload = payload.replace((100_000).to_bytes(4, "little"), (10).to_bytes(4, "little"))
    with pytest.raises(TransferError) as error:
        archive.read(payload)
    assert "damaged" in str(error.value)


def test_an_over_sized_upload_is_refused_at_the_import_page(client, monkeypatch):
    monkeypatch.setattr(archive, "MAX_UPLOAD_BYTES", 1024)
    upload = SimpleUploadedFile(
        "event.zip", exporters.export(competition=make_event()) + b"\0" * 2048,
        content_type="application/zip",
    )
    response = client.post(reverse("transfer:import"), {"archive": upload}, follow=True)
    assert "too large" in response.content.decode()
    assert staging.SESSION_KEY not in client.session


def test_an_over_sized_participant_csv_is_refused(client, monkeypatch):
    from . import views as transfer_views

    make_event()
    monkeypatch.setattr(transfer_views, "MAX_CSV_BYTES", 16)
    upload = SimpleUploadedFile("starters.csv", b"x" * 64, content_type="text/csv")
    response = client.post(reverse("transfer:import"), {"csv": upload}, follow=True)
    assert "too large" in response.content.decode()


# --- markup arriving in a file ----------------------------------------


def test_imported_pdf_header_is_sanitised(settings, tmp_path):
    """The header/footer is the one field in the document that holds markup, and the
    settings page renders it into an editor. An import used to store it verbatim, so
    another club's file could run script in the importer's session."""
    settings.MEDIA_ROOT = tmp_path
    competition = make_event()
    layout = ResultsPdfLayout.objects.create(competition=competition)
    # Written past the editor, the way an export from a tampered-with system would be.
    ResultsPdfLayout.objects.filter(pk=layout.pk).update(
        header_html='<img src=x onerror="alert(document.cookie)"><b>Cup</b>',
        footer_html='<script>alert(1)</script>Timed',
    )
    payload = exporters.export(competition=competition)
    wipe()

    _plan, result = import_archive(payload)

    imported = ResultsPdfLayout.objects.get(competition=result.competition)
    assert "onerror" not in imported.header_html
    assert "<img" not in imported.header_html
    assert imported.header_html == "<b>Cup</b>"
    assert "<script>" not in imported.footer_html
    assert imported.footer_html == "alert(1)Timed"


# --- event round trip --------------------------------------------------------


def test_event_round_trip_onto_a_bare_system():
    payload = exporters.export(competition=make_event())
    wipe()

    _plan, result = import_archive(payload)

    competition = result.competition
    assert competition.name == "Spring Race"
    assert competition.date == datetime.date(2026, 5, 1)
    assert competition.penalties_by_marshal_posts is True
    assert competition.start_pattern == [{"window": None, "chips": ["practice", "counted"]}]
    assert competition.competition_type.name == "Motorcycle"
    assert competition.competition_type.timing_precision == CompetitionType.Precision.THOUSANDTHS

    cclass = competition.classes.get()
    assert (cclass.name, cclass.practice_runs, cclass.counted_runs) == ("T1", 1, 2)
    assert cclass.is_running and cclass.run_position == 1

    entry = competition.entries.get()
    assert entry.bib_number == 7
    assert str(entry.participant) == "Ada Lovelace"
    assert entry.participant.vehicle == "Kart 1"
    assert ClassAssignment.objects.filter(competition_class=cclass).count() == 1


def test_event_round_trip_restores_timing():
    payload = exporters.export(competition=make_event())
    wipe()

    _plan, result = import_archive(payload)

    run = TimedRun.objects.get(competition=result.competition)
    assert run.bib_number == 7
    assert run.run_type == TimedRun.RunType.COUNTED
    assert run.manual_entry is True
    assert run.manual_run_time == Decimal("42.500")
    assert (run.pylon_count, run.pylon_adjust) == (2, -1)
    assert run.start_signal.device_time == BASE.time()
    assert run.finish_signal.port == 2
    assert run.competition_class == result.competition.classes.get()

    penalty = MarshalPenalty.objects.get(timed_run=run)
    assert penalty.submitted is True
    assert penalty.detail == {"tasks": {"5": {"pylons": 2}}, "stop_line": False}
    assert penalty.marshal_post.number == 1


def test_signal_arrival_order_survives_the_round_trip():
    """arrangement.py pairs runs by arrival, so received_at must come across —
    auto_now_add would otherwise restamp every signal at import time."""
    competition = make_event()
    original = list(
        TimingSignal.objects.filter(competition=competition)
        .order_by("pk").values_list("received_at", flat=True)
    )
    payload = exporters.export(competition=competition)
    wipe()

    _plan, result = import_archive(payload)

    restored = list(
        TimingSignal.objects.filter(competition=result.competition)
        .order_by("pk").values_list("received_at", flat=True)
    )
    assert restored == original


def test_timing_can_be_left_out_of_an_export():
    payload = exporters.export(competition=make_event(), include_timing=False)
    wipe()

    _plan, result = import_archive(payload)

    assert TimedRun.objects.filter(competition=result.competition).count() == 0
    assert result.competition.entries.count() == 1


def test_marshal_post_claims_do_not_travel():
    """A claim says which *device* is holding a post; importing it elsewhere
    would lock a post nobody is standing at."""
    payload = exporters.export(competition=make_event())
    wipe()

    _plan, result = import_archive(payload)

    post = MarshalPost.objects.get(competition=result.competition)
    assert post.tasks == "1, 5, 11-15"
    assert post.handles_stop_line is True
    assert post.claim_token == ""
    assert post.claim_seen is None


def test_import_never_steals_the_active_competition():
    payload = exporters.export(competition=make_event())
    keeper = make_event(ctype=make_type("Go-Cart"))
    Competition.objects.filter(pk=keeper.pk).update(is_active=True)

    _plan, result = import_archive(payload)

    assert result.competition.is_active is False
    keeper.refresh_from_db()
    assert keeper.is_active is True


def test_import_can_activate_the_event_on_request():
    payload = exporters.export(competition=make_event())
    wipe()

    _plan, result = import_archive(payload, activate=True)

    result.competition.refresh_from_db()
    assert result.competition.is_active is True


# --- pk remapping ------------------------------------------------------------


def test_auto_timing_order_is_remapped_onto_the_new_pks():
    payload = exporters.export(competition=make_event())
    wipe()
    # Burn some pks so the imported rows can't accidentally land on the old ones.
    filler = make_event(ctype=make_type("Filler"))
    filler.delete()

    _plan, result = import_archive(payload)

    competition = result.competition
    entry = competition.entries.get()
    cclass = competition.classes.get()
    assert competition.auto_timing_order == [slot_key(entry.pk, cclass.pk, 0, "counted", 1)]


def test_tie_resolution_scope_and_members_are_remapped():
    payload = exporters.export(competition=make_event())
    wipe()
    filler = make_event(ctype=make_type("Filler"))
    filler.delete()

    _plan, result = import_archive(payload)

    competition = result.competition
    tie = ManualTieResolution.objects.get(competition=competition)
    entry = competition.entries.get()
    assert tie.scope == f"class:{competition.classes.get().pk}"
    assert tie.members == [[entry.pk, 0, 1], [entry.pk, 1, 1]]


def test_result_column_settings_come_across():
    payload = exporters.export(competition=make_event())
    wipe()

    _plan, result = import_archive(payload)

    row = ResultColumnSettings.objects.get(competition=result.competition)
    assert row.columns == ["driver_name", "club"]
    assert row.show_overall is False


def test_pdf_logo_travels_inside_the_archive(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    competition = make_event()
    layout = ResultsPdfLayout.objects.create(competition=competition, header_html="<b>Hi</b>")
    # A 1x1 GIF — small, and a real image so ImageField accepts it.
    pixel = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
             b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
    layout.image_left.save("logo.gif", ContentFile(pixel), save=True)

    payload = exporters.export(competition=competition)
    wipe()

    _plan, result = import_archive(payload)

    imported = ResultsPdfLayout.objects.get(competition=result.competition)
    assert imported.header_html == "<b>Hi</b>"
    assert imported.image_left
    with imported.image_left.open("rb") as handle:
        assert handle.read() == pixel


# --- competition type scope --------------------------------------------------


def test_type_export_carries_its_participants_but_no_event():
    ctype = make_type()
    make_participant(ctype, first="Ada")
    make_participant(ctype, first="Grace", last="Hopper")
    document, _media = archive.read(exporters.export(competition_type=ctype))

    assert document["scope"] == SCOPE_TYPE
    assert document.get("competition") is None
    assert len(document["participants"]) == 2


def test_type_import_creates_the_type_and_its_participants():
    ctype = make_type()
    make_participant(ctype, first="Grace", last="Hopper")
    payload = exporters.export(competition_type=ctype)
    wipe()

    _plan, result = import_archive(payload)

    assert result.competition is None
    assert result.competition_type.name == "Motorcycle"
    assert result.competition_type.requires_vehicle is True
    assert result.competition_type.requires_phone is False
    participant = Participant.objects.get()
    assert (participant.first_name, participant.last_name) == ("Grace", "Hopper")
    assert participant.competition_type == result.competition_type


def test_existing_type_is_reused_rather_than_duplicated():
    ctype = make_type()
    make_participant(ctype, first="Grace", last="Hopper")
    payload = exporters.export(competition_type=ctype)

    _plan, result = import_archive(payload)

    assert CompetitionType.objects.filter(name="Motorcycle").count() == 1
    assert result.competition_type == ctype


def test_type_action_create_registers_a_separate_type():
    ctype = make_type()
    payload = exporters.export(competition_type=ctype)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    result = importers.commit(
        plan, resolutions={}, type_action=importers.TYPE_CREATE, media=media
    )

    assert result.competition_type != ctype
    assert CompetitionType.objects.filter(name__startswith="Motorcycle").count() == 2
    assert "imported" in result.competition_type.name


# --- competition-type settings (issue #10) -----------------------------------
#
# An export has always carried the competition type's parameters; what it did
# not do was *say* when they disagreed with the ones already on this system. The
# type is shared by every competition of the discipline, so importing one club's
# file used to be a choice between ignoring their rules entirely and overwriting
# this system's — including for the fourteen settings both agreed about.


def test_the_export_carries_every_competition_type_parameter():
    """The structural half: a setting the export forgets is one an import can
    never reconcile, and the failure is silent — the file simply says nothing
    about it and the type keeps whatever it had.

    Pinned against archiving.FROZEN_FIELDS (every concrete field of the model)
    rather than against a list written out here, so adding a parameter to
    CompetitionType fails this until the export carries it too.
    """
    assert set(schema.COMPETITION_TYPE_FIELDS) == set(archiving.FROZEN_FIELDS)


def test_a_type_that_agrees_asks_nothing():
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)

    document, _media = archive.read(payload)
    assert importers.plan(document).type_settings == []


def test_the_plan_lists_only_the_parameters_that_differ():
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99)

    document, _media = archive.read(payload)
    rows = importers.plan(document).type_settings

    assert [row["field"] for row in rows] == ["pylon_penalty"]
    assert rows[0]["incoming"] == 5 and rows[0]["current"] == 99


def test_the_type_name_is_never_offered_as_a_difference():
    """It is what matched the two types in the first place. Another club's
    capitalisation is not a rule this system should be asked to adopt."""
    ctype = make_type()
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(name="motorcycle")

    document, _media = archive.read(payload)
    assert importers.plan(document).type_settings == []


def test_an_undecided_parameter_keeps_this_systems_value():
    """The default, and the one that matters: an import is about the event in
    the file, and must not re-rank finished events by moving a shared rule."""
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    importers.commit(plan, resolutions={}, media=media)

    ctype.refresh_from_db()
    assert ctype.pylon_penalty == 99


def test_keeping_the_files_parameter_writes_it_to_the_shared_type():
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99, requires_phone=True)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    importers.commit(
        plan,
        resolutions={},
        type_settings={"pylon_penalty": "incoming", "requires_phone": "current"},
        media=media,
    )

    ctype.refresh_from_db()
    # Answered "from the file" — taken; answered "current" — left alone, even
    # though the file disagrees about it too.
    assert ctype.pylon_penalty == 5
    assert ctype.requires_phone is True


def test_a_parameter_nobody_was_asked_about_cannot_be_moved():
    """A stale page posting an answer for a setting the two types agree about
    must not write it: the plan is what decides which rows exist."""
    ctype = make_type(pylon_penalty=5, task_penalty=10)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    importers.commit(
        plan,
        resolutions={},
        type_settings={"task_penalty": "incoming", "pylon_penalty": "incoming"},
        media=media,
    )

    ctype.refresh_from_db()
    assert ctype.pylon_penalty == 5     # a real difference, answered
    assert ctype.task_penalty == 10     # never differed, so never touched


def test_an_event_import_reconciles_the_type_it_lands_in():
    """The event scope is the case the issue is really about: a club's event
    file arrives, and its discipline's settings have moved here since."""
    competition = make_event()
    ctype = competition.competition_type
    payload = exporters.export(competition=competition)
    CompetitionType.objects.filter(pk=ctype.pk).update(timing_precision=2)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    assert [row["field"] for row in plan.type_settings] == ["timing_precision"]

    importers.commit(
        plan, resolutions={},
        type_settings={"timing_precision": "incoming"}, media=media,
    )
    ctype.refresh_from_db()
    assert ctype.timing_precision == 3


def test_the_review_screen_shows_the_differences(client):
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99)

    upload(client, payload)
    page = client.get(reverse("transfer:review")).content.decode()

    assert 'name="setting-pylon_penalty"' in page
    assert 'value="incoming"' in page
    # This system's value is the one pre-selected.
    assert 'value="current"\n                   checked' in page or 'value="current" checked' in page


def test_the_review_screen_asks_nothing_when_the_type_agrees(client):
    ctype = make_type(pylon_penalty=5)
    upload(client, exporters.export(competition_type=ctype))

    page = client.get(reverse("transfer:review")).content.decode()
    assert "setting-pylon_penalty" not in page


def test_the_review_form_moves_the_setting_it_was_told_to(client):
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99)

    upload(client, payload)
    client.post(reverse("transfer:review"), {"setting-pylon_penalty": "incoming"})

    ctype.refresh_from_db()
    assert ctype.pylon_penalty == 5


# --- participant matching ----------------------------------------------------


def test_an_identical_participant_is_reused_not_duplicated():
    ctype = make_type()
    existing = make_participant(ctype)
    payload = exporters.export(competition_type=ctype)

    plan, result = import_archive(payload)

    assert [m.status for m in plan.matches] == [merge.IDENTICAL]
    assert Participant.objects.count() == 1
    assert result.reused_participants == 1
    assert Participant.objects.get().pk == existing.pk


def test_an_unknown_participant_is_new():
    ctype = make_type()
    make_participant(ctype, first="Grace", last="Hopper")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.all().delete()

    plan, result = import_archive(payload)

    assert [m.status for m in plan.matches] == [merge.NEW]
    assert result.created_participants == 1


def test_a_differing_record_is_a_conflict_not_an_overwrite():
    ctype = make_type()
    make_participant(ctype, club="New Club", email="ada@new.example")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(club="Old Club", email="ada@old.example")

    plan = importers.plan(archive.read(payload)[0])

    match = plan.matches[0]
    assert match.status == merge.CONFLICT
    assert {d.field for d in match.candidates[0].diffs} == {"club", "email"}


def test_a_conflict_left_untouched_takes_the_imported_values():
    ctype = make_type()
    make_participant(ctype, club="New Club")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(club="Old Club")

    _plan, result = import_archive(payload)

    assert Participant.objects.count() == 1
    assert Participant.objects.get().club == "New Club"
    assert result.merged_participants == 1


def test_a_conflict_can_be_resolved_field_by_field():
    ctype = make_type()
    make_participant(ctype, club="New Club", email="ada@new.example")
    payload = exporters.export(competition_type=ctype)
    existing = Participant.objects.get()
    Participant.objects.update(club="Old Club", email="ada@old.example")

    document, media = archive.read(payload)
    plan = importers.plan(document)
    match = plan.matches[0]
    importers.commit(
        plan,
        resolutions={
            match.ref: {
                "action": merge.ACTION_MERGE,
                "target": existing.pk,
                "fields": {"club": merge.KEEP_EXISTING, "email": merge.KEEP_IMPORTED},
            }
        },
        media=media,
    )

    existing.refresh_from_db()
    assert existing.club == "Old Club"
    assert existing.email == "ada@new.example"
    assert Participant.objects.count() == 1


def test_a_conflict_can_be_kept_as_a_separate_person():
    ctype = make_type()
    make_participant(ctype, club="New Club")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(club="Old Club")

    document, media = archive.read(payload)
    plan = importers.plan(document)
    match = plan.matches[0]
    importers.commit(
        plan,
        resolutions={match.ref: {"action": merge.ACTION_CREATE, "target": None, "fields": {}}},
        media=media,
    )

    assert Participant.objects.count() == 2
    assert set(Participant.objects.values_list("club", flat=True)) == {"Old Club", "New Club"}


def test_a_matching_licence_offers_a_candidate_despite_a_different_name():
    ctype = make_type()
    make_participant(ctype, first="Ada", last="Lovelace", license_number="L-42")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(last_name="Byron")

    plan = importers.plan(archive.read(payload)[0])

    match = plan.matches[0]
    assert match.status == merge.CONFLICT
    assert match.candidates[0].reason == "license"
    assert {d.field for d in match.candidates[0].diffs} == {"last_name"}


def test_matching_ignores_case_and_padding():
    ctype = make_type()
    make_participant(ctype, first="Ada", last="Lovelace", club="Club A")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(first_name="  ADA ", club="club a")

    plan = importers.plan(archive.read(payload)[0])

    assert plan.matches[0].status == merge.IDENTICAL


def test_participants_of_another_type_are_never_matched():
    ctype = make_type()
    make_participant(ctype)
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(competition_type=make_type("Go-Cart"))

    plan = importers.plan(archive.read(payload)[0])

    assert plan.matches[0].status == merge.NEW


def test_reimporting_an_event_reuses_people_and_adds_a_second_competition():
    """The everyday case: the same event file landing on a system that already
    knows the competitors. Nobody should be duplicated."""
    payload = exporters.export(competition=make_event())

    _plan, result = import_archive(payload)

    assert Participant.objects.count() == 1
    assert Competition.objects.count() == 2
    assert result.competition.entries.get().participant == Participant.objects.get()


def test_two_file_participants_merged_into_one_keep_a_single_entry():
    """Merging two incoming competitors onto the same existing person would
    break one-entry-per-participant; the import warns instead of failing."""
    ctype = make_type()
    competition = make_event(ctype=ctype, with_timing=False)
    twin = make_participant(ctype, first="Ada", last="Lovelace", club="Club B")
    EventEntry.objects.create(participant=twin, competition=competition, bib_number=8)
    payload = exporters.export(competition=competition)

    # The receiving system knows only one Ada, so both rows resolve onto her:
    # the first exactly, the second as a conflict that defaults to merging.
    competition.delete()
    twin.delete()

    document, media = archive.read(payload)
    plan = importers.plan(document)
    assert [m.status for m in plan.matches] == [merge.IDENTICAL, merge.CONFLICT]

    result = importers.commit(plan, resolutions={}, media=media)

    assert Participant.objects.count() == 1
    assert result.competition.entries.count() == 1
    assert result.warnings


# --- views -------------------------------------------------------------------


def test_export_page_lists_events_and_types(client):
    make_event()
    response = client.get(reverse("transfer:export"))
    assert response.status_code == 200
    assert "Spring Race" in response.content.decode()


def test_export_download_returns_an_archive(client):
    competition = make_event()
    response = client.post(
        reverse("transfer:export"),
        {"target": "competition", "pk": competition.pk, "include_timing": "on"},
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    assert ".zip" in response["Content-Disposition"]
    document, _media = archive.read(response.content)
    assert document["scope"] == SCOPE_EVENT


def test_type_export_download(client):
    ctype = make_type()
    response = client.post(reverse("transfer:export"), {"target": "type", "pk": ctype.pk})
    assert response.status_code == 200
    document, _media = archive.read(response.content)
    assert document["scope"] == SCOPE_TYPE


def upload(client, payload, name="event.zip"):
    return client.post(reverse("transfer:import"), {"archive": _as_upload(payload, name)})


def _as_upload(payload, name):
    return SimpleUploadedFile(name, payload, content_type="application/zip")


def test_pages_render_in_german(client, settings):
    """German is the shipped default, so these pages have to be translated —
    this guards the catalog, not the wording."""
    settings.LANGUAGE_CODE = "de"
    make_event()

    export_page = client.get(reverse("transfer:export")).content.decode()
    assert "Veranstaltungen" in export_page
    assert "Zeitmessdaten einschließen" in export_page

    import_page = client.get(reverse("transfer:import")).content.decode()
    assert "Datei einlesen" in import_page


def test_import_page_offers_an_upload(client):
    response = client.get(reverse("transfer:import"))
    assert response.status_code == 200
    assert 'name="archive"' in response.content.decode()


def test_import_wizard_uploads_reviews_and_commits(client):
    payload = exporters.export(competition=make_event())
    wipe()

    response = upload(client, payload)
    assert response.status_code == 302
    assert response["Location"] == reverse("transfer:review")

    review = client.get(reverse("transfer:review"))
    assert review.status_code == 200
    assert "Spring Race" in review.content.decode()
    # Still nothing written — the review step only looks.
    assert Competition.objects.count() == 0

    done = client.post(reverse("transfer:review"), {})
    assert done.status_code == 200
    assert Competition.objects.count() == 1
    assert Competition.objects.get().name == "Spring Race"


def test_review_form_resolves_a_conflict_by_field(client):
    ctype = make_type()
    make_participant(ctype, club="New Club", email="ada@new.example")
    payload = exporters.export(competition_type=ctype)
    existing = Participant.objects.get()
    Participant.objects.update(club="Old Club", email="ada@old.example")

    upload(client, payload)
    plan = importers.plan(archive.read(payload)[0])
    ref = plan.matches[0].ref

    client.post(reverse("transfer:review"), {
        f"choice-{ref}": f"merge:{existing.pk}",
        f"field-{ref}-{existing.pk}-club": "existing",
        f"field-{ref}-{existing.pk}-email": "imported",
    })

    existing.refresh_from_db()
    assert existing.club == "Old Club"
    assert existing.email == "ada@new.example"
    assert Participant.objects.count() == 1


def test_review_form_can_keep_a_conflict_separate(client):
    ctype = make_type()
    make_participant(ctype, club="New Club")
    payload = exporters.export(competition_type=ctype)
    Participant.objects.update(club="Old Club")

    upload(client, payload)
    ref = importers.plan(archive.read(payload)[0]).matches[0].ref

    client.post(reverse("transfer:review"), {f"choice-{ref}": "create"})

    assert Participant.objects.count() == 2


def test_uploading_a_bad_file_is_rejected_with_a_message(client):
    response = upload(client, b"not a zip at all", name="junk.zip")

    assert response.status_code == 302
    assert response["Location"] == reverse("transfer:import")
    messages = [str(m) for m in response.wsgi_request._messages]
    assert any("Slalom Timing export" in message for message in messages)


def test_review_without_an_upload_goes_back_to_the_upload_step(client):
    response = client.get(reverse("transfer:review"))
    assert response.status_code == 302
    assert response["Location"] == reverse("transfer:import")


def test_cancelling_discards_the_staged_upload(client):
    payload = exporters.export(competition=make_event())
    upload(client, payload)
    token = client.session[staging.SESSION_KEY]

    response = client.post(reverse("transfer:cancel"))

    assert response.status_code == 302
    assert staging.read(token) is None
    assert staging.SESSION_KEY not in client.session


def test_committing_discards_the_staged_upload(client):
    payload = exporters.export(competition=make_event())
    wipe()
    upload(client, payload)
    token = client.session[staging.SESSION_KEY]

    client.post(reverse("transfer:review"), {})

    assert staging.read(token) is None


# --- the review form is bigger than Django's default form ---------------------


def test_a_realistic_review_page_stays_under_the_field_limit(client, settings):
    """The review form is urlencoded, so DATA_UPLOAD_MAX_NUMBER_FIELDS applies to
    it — and Django's default of 1000 is fewer fields than the wizard's own main
    use case renders. Re-importing a club's roster into a system that already
    knows those people is the *normal* case: 200 participants differing in six
    fields each is 1400 radio groups, all of which submit. It came back as a bare
    browser 400 — no message, no partial save, and the staged upload gone.

    So this asks the two questions that keep it fixed: does a realistic page fit
    inside the configured limit, and does it still not fit inside Django's
    default — because a test that only checks the first would pass just as
    happily on a page that had quietly shrunk.
    """
    import re

    ctype = make_type()
    for n in range(200):
        make_participant(ctype, first=f"P{n}", last=f"Racer{n}")
    payload = exporters.export(competition_type=ctype)
    # Every one of them now disagrees with the file on six fields, which is what
    # turns each into a CONFLICT with a row per field to decide.
    Participant.objects.update(
        club="Old Club", email="old@example.org", phone_number="0700",
        vehicle="Old Kart", address_street="Old Street 1", address_city="Oldtown",
    )

    upload(client, payload)
    page = client.get(reverse("transfer:review")).content.decode()

    posted = set(re.findall(r'name="((?:choice|field)-[^"]+)"', page))
    assert len(posted) > 1000, (
        f"only {len(posted)} fields — this page no longer reproduces the case, "
        "so it no longer guards it"
    )
    assert len(posted) < settings.DATA_UPLOAD_MAX_NUMBER_FIELDS

    # And end to end: the same page's POST is accepted rather than refused
    # before any view sees it.
    response = client.post(reverse("transfer:review"), {
        name: "create" if name.startswith("choice-") else "imported"
        for name in posted
    })
    assert response.status_code == 200
    assert Participant.objects.count() == 400        # each kept separate


# --- staging -----------------------------------------------------------------


def test_staging_rejects_a_token_that_is_not_ours():
    assert staging.read("../../etc/passwd") is None
    assert staging.read("") is None


def test_staging_sweeps_abandoned_uploads():
    token = staging.store(_as_upload(b"payload", "x.zip"))
    assert staging.read(token) == b"payload"

    staging.sweep(now=999_999_999_999)

    assert staging.read(token) is None


# --- CSV participant import --------------------------------------------------
#
# The columns a file needs follow the active competition's type, so these vary
# it deliberately: `plain()` turns the optional details off to keep a fixture
# readable, and the tests that care about a detail switch that one back on.


def plain(**overrides):
    """An active competition whose type collects only the always-required
    identity — the smallest file these tests can post."""
    values = dict(
        requires_club=False, requires_address=False, requires_email=False,
        requires_license=False, requires_vehicle=False, requires_phone=False,
        requires_co_driver=False,
    )
    values.update(overrides)
    return csv_setup(**values)


def csv_setup(**type_overrides):
    ctype = make_type(**type_overrides)
    competition = Competition.objects.create(
        competition_type=ctype, name="CSV Race", date=datetime.date(2026, 6, 1),
        is_active=True,
    )
    return ctype, competition


def csv_text(rows, header=None, delimiter=";"):
    header = header or ["first_name", "last_name", "date_of_birth"]
    lines = [delimiter.join(header)]
    lines.extend(delimiter.join(str(cell) for cell in row) for row in rows)
    return "\n".join(lines)


def read_csv(text, competition, encoding="utf-8"):
    return csvimport.read(text.encode(encoding), competition)


def messages_of(report):
    return " | ".join(message for _line, message in report.errors)


def test_columns_follow_what_the_type_collects():
    _ctype, competition = csv_setup(requires_phone=False, requires_club=True)
    keys = [c.key for c in csvimport.columns_for(competition)]

    assert keys[:3] == ["first_name", "last_name", "date_of_birth"]
    assert "club" in keys
    assert "phone_number" not in keys      # this type doesn't collect it
    assert keys[-1] == "bib_number"


def test_only_the_types_mandatory_details_are_required():
    _ctype, competition = csv_setup(requires_club=True, requires_co_driver=True)
    required = {c.key for c in csvimport.columns_for(competition) if c.mandatory}

    assert {"first_name", "last_name", "date_of_birth", "club"} <= required
    # A co-driver is collected but never mandatory (PARTICIPANT_INFO says so).
    assert "co_driver_first_name" not in required
    assert "bib_number" not in required


def test_a_good_file_registers_everyone():
    _ctype, competition = plain()
    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", 5], ["Erika", "Musterfrau", "05.09.2011", 6]],
        header=["first_name", "last_name", "date_of_birth", "bib_number"],
    )

    report = read_csv(text, competition)
    assert report.ok, report.errors
    result = csvimport.commit(report, competition)

    assert (result.created, result.entries, result.auto_bibs) == (2, 2, 0)
    entry = EventEntry.objects.get(competition=competition, bib_number=5)
    assert str(entry.participant) == "Max Mustermann"
    assert entry.participant.date_of_birth == datetime.date(2010, 4, 23)
    assert entry.participant.competition_type == competition.competition_type
    other = EventEntry.objects.get(competition=competition, bib_number=6)
    assert other.participant.date_of_birth == datetime.date(2011, 9, 5)


def test_a_missing_mandatory_column_refuses_the_whole_file():
    _ctype, competition = plain(requires_club=True)
    text = csv_text([["Max", "Mustermann", "2010-04-23"]])

    report = read_csv(text, competition)

    assert not report.ok
    assert "club" in messages_of(report)
    assert Participant.objects.count() == 0


def test_a_blank_mandatory_value_refuses_the_whole_file():
    _ctype, competition = plain(requires_club=True)
    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", "MSC A"],
         ["Erika", "Musterfrau", "2011-09-05", ""]],
        header=["first_name", "last_name", "date_of_birth", "club"],
    )

    report = read_csv(text, competition)

    assert not report.ok
    assert report.errors[0][0] == 3          # the spreadsheet line, header counted
    assert Participant.objects.count() == 0


def test_an_unreadable_date_is_reported_with_its_line():
    _ctype, competition = plain()
    text = csv_text([["Max", "Mustermann", "not a date"]])

    report = read_csv(text, competition)

    assert not report.ok
    assert report.errors[0][0] == 2


def test_a_bib_already_taken_is_refused():
    ctype, competition = plain()
    existing = make_participant(ctype, first="Ada", last="Lovelace")
    EventEntry.objects.create(participant=existing, competition=competition, bib_number=5)

    text = csv_text([["Max", "Mustermann", "2010-04-23", 5]],
                    header=["first_name", "last_name", "date_of_birth", "bib_number"])
    report = read_csv(text, competition)

    assert not report.ok
    assert "5" in messages_of(report)


def test_a_bib_used_twice_in_the_file_is_refused():
    _ctype, competition = plain()
    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", 5], ["Erika", "Musterfrau", "2011-09-05", 5]],
        header=["first_name", "last_name", "date_of_birth", "bib_number"],
    )

    report = read_csv(text, competition)

    assert not report.ok
    assert report.errors[0][0] == 3


def test_missing_bibs_fill_the_free_numbers():
    ctype, competition = plain()
    existing = make_participant(ctype, first="Ada", last="Lovelace")
    EventEntry.objects.create(participant=existing, competition=competition, bib_number=2)

    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", ""], ["Erika", "Musterfrau", "2011-09-05", 7],
         ["Otto", "Normal", "2012-01-02", ""]],
        header=["first_name", "last_name", "date_of_birth", "bib_number"],
    )
    report = read_csv(text, competition)
    assert report.ok, report.errors
    result = csvimport.commit(report, competition)

    assert result.auto_bibs == 2
    bibs = set(
        EventEntry.objects.filter(competition=competition).values_list("bib_number", flat=True)
    )
    assert bibs == {1, 2, 3, 7}      # 2 was already taken, 7 came from the file


def test_a_known_participant_is_reused_not_duplicated():
    ctype, competition = plain()
    known = make_participant(ctype, first="Ada", last="Lovelace")

    text = csv_text([["  ada ", "LOVELACE", known.date_of_birth.isoformat()]])
    report = read_csv(text, competition)
    assert report.ok, report.errors
    result = csvimport.commit(report, competition)

    assert (result.created, result.reused) == (0, 1)
    assert Participant.objects.count() == 1
    assert EventEntry.objects.get(competition=competition).participant == known


def test_someone_already_entered_is_refused():
    ctype, competition = plain()
    known = make_participant(ctype, first="Ada", last="Lovelace")
    EventEntry.objects.create(participant=known, competition=competition, bib_number=1)

    text = csv_text([["Ada", "Lovelace", known.date_of_birth.isoformat()]])
    report = read_csv(text, competition)

    assert not report.ok
    assert "already entered" in messages_of(report)


def test_the_same_person_twice_in_one_file_is_refused():
    _ctype, competition = plain()
    text = csv_text([["Max", "Mustermann", "2010-04-23"], ["max", "mustermann", "2010-04-23"]])

    report = read_csv(text, competition)

    assert not report.ok
    assert report.errors[0][0] == 3


def test_headers_are_matched_loosely():
    _ctype, competition = plain()
    text = csv_text(
        [["Max", "Mustermann", "23.04.2010", 4]],
        header=["First Name", "SURNAME", "Date of Birth", "Bib"],
    )

    report = read_csv(text, competition)

    assert report.ok, report.errors
    assert report.rows[0].bib == 4
    assert report.rows[0].values["last_name"] == "Mustermann"


def test_comma_and_tab_separated_files_are_read_too():
    _ctype, competition = plain()
    for delimiter in (",", "\t"):
        text = csv_text([["Max", "Mustermann", "2010-04-23"]], delimiter=delimiter)
        assert read_csv(text, competition).ok


def test_a_file_saved_as_windows_latin1_still_reads():
    _ctype, competition = plain()
    text = csv_text([["Jürgen", "Müller", "2010-04-23"]])

    report = read_csv(text, competition, encoding="cp1252")

    assert report.ok, report.errors
    assert report.rows[0].values["first_name"] == "Jürgen"


def test_blank_lines_are_skipped_not_flagged():
    _ctype, competition = plain()
    text = csv_text([["Max", "Mustermann", "2010-04-23"]]) + "\n\n;;\n"

    report = read_csv(text, competition)

    assert report.ok, report.errors
    assert len(report.rows) == 1


def test_unknown_columns_are_ignored_not_fatal():
    _ctype, competition = plain()
    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", "whatever"]],
        header=["first_name", "last_name", "date_of_birth", "lieblingsfarbe"],
    )

    report = read_csv(text, competition)

    assert report.ok, report.errors
    assert report.ignored_columns == ["lieblingsfarbe"]


def test_a_header_with_no_rows_is_refused():
    _ctype, competition = plain()

    report = read_csv(csv_text([]), competition)

    assert not report.ok
    assert report.errors


def test_an_empty_file_is_refused():
    _ctype, competition = plain()

    report = read_csv("", competition)

    assert not report.ok


def test_a_bad_email_is_refused_when_the_type_collects_one():
    _ctype, competition = plain(requires_email=True)
    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", "not-an-address"]],
        header=["first_name", "last_name", "date_of_birth", "email"],
    )

    report = read_csv(text, competition)

    assert not report.ok
    assert report.errors[0][0] == 2


def test_optional_details_are_stored_when_given():
    _ctype, competition = plain(requires_club=True, requires_phone=True)
    text = csv_text(
        [["Max", "Mustermann", "2010-04-23", "MSC A", "+49 160 111"]],
        header=["first_name", "last_name", "date_of_birth", "club", "phone_number"],
    )
    report = read_csv(text, competition)
    assert report.ok, report.errors
    csvimport.commit(report, competition)

    participant = Participant.objects.get()
    assert participant.club == "MSC A"
    assert participant.phone_number == "+49 160 111"


# --- CSV on the import page --------------------------------------------------
#
# Both kinds of file are offered by the one Import page; which one was submitted
# is read off the file field's name.


def post_csv(client, text, name="participants.csv"):
    upload = SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")
    return client.post(reverse("transfer:import"), {"csv": upload})


def test_import_page_offers_both_kinds_of_file(client):
    csv_setup()

    body = client.get(reverse("transfer:import")).content.decode()

    assert 'name="archive"' in body      # the export archive
    assert 'name="csv"' in body          # the participant list


def test_import_page_lists_the_required_columns(client):
    csv_setup(requires_club=True, requires_phone=False)

    body = client.get(reverse("transfer:import")).content.decode()

    assert "first_name" in body and "club" in body
    assert "phone_number" not in body
    assert "Required" in body


def test_import_page_without_an_active_competition_still_takes_an_archive(client):
    """No competition selected only rules out the participant half — an export
    archive brings its own competition with it."""
    response = client.get(reverse("transfer:import"))
    body = response.content.decode()

    assert response.status_code == 200
    assert 'name="archive"' in body
    assert 'name="csv"' not in body
    assert "none is selected" in body


def test_posting_participants_without_a_competition_is_refused(client):
    response = post_csv(client, csv_text([["Max", "Mustermann", "2010-04-23"]]))

    assert response.status_code == 302
    assert Participant.objects.count() == 0


def test_csv_sample_downloads_the_matching_columns(client):
    csv_setup(requires_club=True, requires_phone=False)

    response = client.get(reverse("transfer:csv-sample"))

    assert response.status_code == 200
    assert "text/csv" in response["Content-Type"]
    body = response.content.decode("utf-8-sig")
    header = body.splitlines()[0].split(csvimport.SAMPLE_DELIMITER)
    assert header[:3] == ["first_name", "last_name", "date_of_birth"]
    assert "club" in header and "phone_number" not in header
    assert header[-1] == "bib_number"


def test_the_sample_file_imports_cleanly():
    """The example we hand out has to be a file the importer accepts."""
    _ctype, competition = csv_setup()

    report = csvimport.read(csvimport.sample_csv(competition).encode("utf-8"), competition)

    assert report.ok, report.errors
    assert len(report.rows) == 2


def test_csv_upload_registers_and_reports(client):
    _ctype, competition = plain()
    text = csv_text([["Max", "Mustermann", "2010-04-23", 3]],
                    header=["first_name", "last_name", "date_of_birth", "bib_number"])

    response = post_csv(client, text)

    assert response.status_code == 200
    assert EventEntry.objects.filter(competition=competition, bib_number=3).exists()


def test_csv_upload_with_a_bad_row_changes_nothing(client):
    _ctype, competition = plain()
    text = csv_text([["Max", "Mustermann", "2010-04-23"], ["Erika", "", "2011-09-05"]])

    response = post_csv(client, text)

    assert response.status_code == 200
    assert "was not imported" in response.content.decode()
    assert Participant.objects.count() == 0
    assert EventEntry.objects.count() == 0


def test_every_fault_in_a_file_is_reported_in_one_pass():
    """A bad date on one line must not hide a duplicate bib on the next — the
    operator should be able to fix the whole file from a single upload."""
    _ctype, competition = plain()
    text = csv_text(
        [["Test", "Eins", "kein datum", 77], ["Test", "Zwei", "2012-01-01", 77]],
        header=["first_name", "last_name", "date_of_birth", "bib_number"],
    )

    report = read_csv(text, competition)

    assert not report.ok
    assert [line for line, _message in report.errors] == [2, 3]
    assert "77" in messages_of(report)


# --- Automatic backup ---------------------------------------
# The event *is* the database, and the only backup used to be a line in the
# run-book asking the operator to run VACUUM INTO between runs and copy the
# result to a USB stick — a thing to remember while timing a race.

import sqlite3
import threading

from apps.transfer import backup
from apps.transfer.models import BackupSettings


def _write_source(path, rows=200):
    """A database that looks like one of ours: WAL, and something to lose."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE timing (id INTEGER PRIMARY KEY, t TEXT)")
    conn.executemany("INSERT INTO timing (t) VALUES (?)",
                     [(f"10:00:{i:02d}",) for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM timing").fetchone()[0]
    finally:
        conn.close()


class TestCopyingTheDatabase:
    def test_the_copy_holds_every_row(self, tmp_path):
        source = _write_source(tmp_path / "db.sqlite3")
        destination = tmp_path / "stick"
        destination.mkdir()
        written = backup.copy_database(source, destination, keep=5)
        assert written.exists()
        assert _rows(written) == 200

    def test_the_copy_is_readable_on_its_own(self, tmp_path):
        """A file copy of a WAL database leaves the recent writes in the -wal
        file beside it; carried off on a stick alone it is missing them. SQLite's
        backup API writes a self-contained database."""
        source = _write_source(tmp_path / "db.sqlite3")
        destination = tmp_path / "stick"
        destination.mkdir()
        written = backup.copy_database(source, destination, keep=5)
        carried = tmp_path / "elsewhere.sqlite3"
        carried.write_bytes(written.read_bytes())   # the file, and nothing else
        assert _rows(carried) == 200

    def test_a_copy_taken_while_the_database_is_written_finishes_and_is_consistent(
            self, tmp_path):
        """The rig records times through this database while the copy runs, so a
        torn copy is worse than none — it looks like a backup.

        This also pins the reason the copy is taken in *one* step: a batched
        backup gives up its read lock between batches and SQLite restarts it
        whenever another connection has written, so against a writer like the one
        below it restarts for ever and never produces a file. The first version of
        this code did exactly that, and this test is what found it — it hung."""
        source = _write_source(tmp_path / "db.sqlite3")
        destination = tmp_path / "stick"
        destination.mkdir()
        stop = threading.Event()

        def writer():
            conn = sqlite3.connect(source, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            n = 0
            while not stop.is_set():
                conn.execute("INSERT INTO timing (t) VALUES (?)", (f"x{n}",))
                conn.commit()
                n += 1
            conn.close()

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            written = backup.copy_database(source, destination, keep=5)
        finally:
            stop.set()
            thread.join(timeout=5)
        # Readable, integral, and holding at least what was there when it started.
        conn = sqlite3.connect(written)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM timing").fetchone()[0] >= 200
        finally:
            conn.close()

    def test_an_interrupted_copy_leaves_no_plausible_looking_file(self, tmp_path, monkeypatch):
        """Half a database sitting there under a backup's name is the worst
        outcome: it is the one you would reach for."""
        source = _write_source(tmp_path / "db.sqlite3")
        destination = tmp_path / "stick"
        destination.mkdir()

        # sqlite3.Connection is immutable, so the interruption goes in through
        # the one seam this module has: the rename that publishes the file.
        def explode(self, target):
            raise OSError("the stick was pulled out")

        monkeypatch.setattr(Path, "replace", explode)
        with pytest.raises(OSError):
            backup.copy_database(source, destination, keep=5)
        assert list(destination.glob("*.sqlite3")) == [], (
            "a half-written copy is sitting there under a backup's name"
        )
        assert list(destination.glob("*.partial")), "the partial should still be there"

        # …and the next good copy sweeps the leftover away.
        monkeypatch.undo()
        backup.copy_database(source, destination, keep=5)
        assert list(destination.glob("*.partial")) == []
        assert len(list(destination.glob("*.sqlite3"))) == 1

    def test_it_keeps_only_the_newest_copies(self, tmp_path):
        """One a minute over an eight-hour event is 480 copies of a growing
        database. A full stick means the newest copy is the one that failed."""
        source = _write_source(tmp_path / "db.sqlite3")
        destination = tmp_path / "stick"
        destination.mkdir()
        for minute in range(8):
            backup.copy_database(source, destination, keep=3,
                                 now=datetime.datetime(2026, 7, 1, 10, minute, 0))
        kept = sorted(p.name for p in destination.glob("*.sqlite3"))
        assert len(kept) == 3
        assert kept[-1].endswith("100700.sqlite3")     # the newest survived


class TestTheDestination:
    def test_a_missing_folder_is_named_as_such(self, tmp_path):
        settings = BackupSettings(destination=str(tmp_path / "not-plugged-in"))
        assert "does not exist" in settings.destination_problem()

    def test_a_file_is_not_a_folder(self, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x")
        assert "not a folder" in BackupSettings(destination=str(target)).destination_problem()

    def test_no_destination_at_all(self):
        assert BackupSettings(destination="").destination_problem()

    def test_a_usable_folder_has_no_problem(self, tmp_path):
        assert BackupSettings(destination=str(tmp_path)).destination_problem() == ""


class TestTheBackupPage:
    def test_saving_a_destination_turns_it_on(self, client, tmp_path, monkeypatch):
        # The runner would otherwise start a thread that reads the suite's own
        # in-memory database — see TestTheTimer for why that hangs.
        monkeypatch.setattr(backup.runner, "start", lambda: None)
        monkeypatch.setattr(backup.runner, "run_soon", lambda: None)
        response = client.post(reverse("transfer:backup"), {
            "enabled": "on", "destination": str(tmp_path),
            "interval_minutes": "5", "keep": "12",
        })
        assert response.status_code == 302
        settings = BackupSettings.load()
        assert settings.enabled and settings.destination == str(tmp_path)

    def test_a_destination_that_cannot_be_written_is_refused_on_save(self, client, tmp_path):
        """Finding out at the next tick, from a page they have navigated away
        from, is not telling the operator."""
        response = client.post(reverse("transfer:backup"), {
            "enabled": "on", "destination": str(tmp_path / "nowhere"),
            "interval_minutes": "5", "keep": "12",
        })
        assert response.status_code == 200
        assert "does not exist" in response.content.decode()
        assert BackupSettings.load().enabled is False

    def test_turning_it_off_needs_no_destination(self, client):
        response = client.post(reverse("transfer:backup"), {
            "destination": "", "interval_minutes": "5", "keep": "12",
        })
        assert response.status_code == 302
        assert BackupSettings.load().enabled is False

    def test_the_interval_is_bounded(self, client, tmp_path):
        for minutes in ("0", "11", "600"):
            response = client.post(reverse("transfer:backup"), {
                "enabled": "on", "destination": str(tmp_path),
                "interval_minutes": minutes, "keep": "12",
            })
            assert response.status_code == 200, f"{minutes} min was accepted"

    def test_the_status_endpoint_reports_the_last_attempt(self, client, tmp_path):
        settings = BackupSettings.load()
        settings.enabled = True
        settings.destination = str(tmp_path)
        settings.last_error = "the stick was pulled out"
        settings.save()
        data = client.get(reverse("transfer:backup-status")).json()
        assert data["enabled"] is True
        assert data["last_error"] == "the stick was pulled out"

    def test_there_is_no_back_up_now_button(self, client, tmp_path):
        """On purpose: the point is that the operator does not have to remember,
        and a button invites them to think they should. (The page *says* so in
        words, so this looks for a control rather than for the phrase.)"""
        import re as _re

        body = client.get(reverse("transfer:backup")).content.decode()
        # data-unsaved-guard, not just method="post": the app shell's own logout
        # form comes first in the document.
        form = _re.search(r"<form[^>]*data-unsaved-guard[^>]*>(.*?)</form>",
                          body, _re.S | _re.I)
        assert form, "the settings form is missing"
        controls = [c.strip() for c in
                    _re.findall(r"<button[^>]*>(.*?)</button>", form.group(1),
                                _re.S | _re.I)]
        # Submits, specifically. "Browse…" is a control on the form and is not one
        # of these; asserting on *every* button made adding it look like this
        # regression, which it wasn't.
        submits = [c.strip() for c in
                   _re.findall(r"<button[^>]*type=\"submit\"[^>]*>(.*?)</button>",
                               form.group(1), _re.S | _re.I)]
        assert submits == ["Save"], submits
        assert not any("now" in c.lower() for c in controls), controls


class TestTheTimer:
    """The timer reads the *live* database file.

    These point it at a real file rather than at the suite's own database: the
    test database is SQLite in shared-cache memory and pytest-django holds an
    uncommitted transaction on it for the length of each test, so a second
    connection reading it blocks for ever. That is an artefact of how the tests
    are run — a deployment's database is a file in WAL mode, where a reader never
    waits on the writer — but it would hang the suite, so it is avoided here and
    said out loud rather than left to be rediscovered.
    """

    @pytest.fixture
    def live_db(self, settings, tmp_path):
        path = tmp_path / "live.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE timing (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        # Only what backup.copy_database reads; the ORM keeps its own connection.
        settings.DATABASES = {**settings.DATABASES,
                              "default": {**settings.DATABASES["default"],
                                          "NAME": str(path)}}
        return path

    def test_a_broken_destination_is_recorded_rather_than_thrown(self, live_db, tmp_path):
        """A backup that has quietly been failing since lunchtime is worse than
        none, because nobody is looking for the fault."""
        settings = BackupSettings.load()
        settings.enabled = True
        settings.destination = str(tmp_path / "gone")
        settings.save()
        backup.runner._tick()
        settings.refresh_from_db()
        assert settings.last_error
        assert settings.last_ok_at is None

    def test_a_good_destination_records_the_file_it_wrote(self, live_db, tmp_path):
        destination = tmp_path / "stick"
        destination.mkdir()
        settings = BackupSettings.load()
        settings.enabled = True
        settings.destination = str(destination)
        settings.save()
        backup.runner._tick()
        settings.refresh_from_db()
        assert settings.last_error == ""
        assert settings.last_ok_at is not None
        assert settings.last_bytes and settings.last_bytes > 0
        assert Path(settings.last_file).exists()

    def test_it_does_nothing_until_the_interval_has_passed(self, live_db, tmp_path):
        destination = tmp_path / "stick"
        destination.mkdir()
        settings = BackupSettings.load()
        settings.enabled = True
        settings.destination = str(destination)
        settings.interval_minutes = 10
        settings.save()
        backup.runner._tick()
        first = list(destination.glob("*.sqlite3"))
        assert first, "the first tick should have written one"
        backup.runner._tick()
        assert list(destination.glob("*.sqlite3")) == first

    def test_it_does_nothing_at_all_when_switched_off(self, live_db, tmp_path):
        destination = tmp_path / "stick"
        destination.mkdir()          # a directory of its own: live_db is in tmp_path
        settings = BackupSettings.load()
        settings.enabled = False
        settings.destination = str(destination)
        settings.save()
        backup.runner._tick()
        assert list(destination.glob("*.sqlite3")) == []


# --- The destination picker --------------------------------------------------
# A file input is no use here: the browser would offer the folders of whichever
# machine is displaying the page, and over the venue network that is usually
# somebody else's phone. So the listing comes from the server.

class TestBrowsingTheHostsFolders:
    def test_no_path_lists_the_drives(self, client):
        data = client.get(reverse("transfer:backup-folders")).json()
        assert data["entries"], "no roots at all"
        assert data["at_root"] is True
        assert data["path"] == ""

    def test_it_lists_the_folders_in_a_folder(self, client, tmp_path):
        (tmp_path / "keep").mkdir()
        (tmp_path / "toss").mkdir()
        (tmp_path / "a-file.txt").write_text("x")
        data = client.get(reverse("transfer:backup-folders"),
                          {"path": str(tmp_path)}).json()
        assert [e["name"] for e in data["entries"]] == ["keep", "toss"]
        assert data["path"] == str(tmp_path)

    def test_it_never_lists_files(self, client, tmp_path):
        """Folders only. Somebody who can reach this may learn that a folder
        exists, and nothing about what is in it."""
        (tmp_path / "secret.sqlite3").write_text("x")
        (tmp_path / "addresses.csv").write_text("x")
        data = client.get(reverse("transfer:backup-folders"),
                          {"path": str(tmp_path)}).json()
        assert data["entries"] == []

    def test_a_path_that_is_not_a_folder_comes_back_as_a_sentence(self, client, tmp_path):
        target = tmp_path / "a-file.txt"
        target.write_text("x")
        for bad in (str(target), str(tmp_path / "nowhere"), "\x00nonsense"):
            data = client.get(reverse("transfer:backup-folders"), {"path": bad}).json()
            assert data["problem"], f"{bad!r} produced no message"
            # …and it lands somewhere usable rather than on an error page.
            assert data["entries"] or data["at_root"]

    def test_it_is_gated_like_the_rest_of_the_section(self):
        """The endpoint lists a filesystem. It must be exactly as reachable as
        the page it serves, and no more."""
        from django.test import Client
        from django.urls import resolve

        from apps.accounts import pages

        match = resolve(reverse("transfer:backup-folders"))
        assert (match.app_name, match.url_name) in pages.PAGE_URLS["import_export"]
        assert (match.app_name, match.url_name) not in pages.OPEN
        response = Client().get(reverse("transfer:backup-folders"))
        assert response.status_code == 302        # anonymous → login

    def test_a_long_folder_is_cut_off_and_says_so(self, client, tmp_path):
        """Thousands of entries is a list nobody can use and a payload nobody
        asked for."""
        from apps.transfer import folders

        for i in range(folders.MAX_ENTRIES + 5):
            (tmp_path / f"d{i:04d}").mkdir()
        data = client.get(reverse("transfer:backup-folders"),
                          {"path": str(tmp_path)}).json()
        assert len(data["entries"]) == folders.MAX_ENTRIES
        assert data["truncated"] == 5

    def test_browsing_writes_nothing(self, client, tmp_path):
        """destination_problem() probes by writing a file. Browsing must not
        leave a trail of those through every folder the operator clicks past."""
        (tmp_path / "sub").mkdir()
        before = sorted(p.name for p in tmp_path.iterdir())
        client.get(reverse("transfer:backup-folders"), {"path": str(tmp_path)})
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_the_parent_of_a_folder_is_offered(self, client, tmp_path):
        (tmp_path / "sub").mkdir()
        data = client.get(reverse("transfer:backup-folders"),
                          {"path": str(tmp_path / "sub")}).json()
        assert data["parent"] == str(tmp_path)
        assert data["at_root"] is False


class TestTheBackupTimestampsReadAsLocalTime:
    """The operator reads "last copy at 08:37" against the clock on the wall
    beside the laptop. TIME_ZONE used to be UTC, so all summer it was an hour or
    two out in a way that is easy to misread as right."""

    def test_the_setting_follows_the_machine(self):
        from config.settings import _local_time_zone

        assert _local_time_zone()      # never empty; UTC is the last resort

    def test_an_explicit_zone_wins(self, monkeypatch):
        from config.settings import _local_time_zone

        monkeypatch.setenv("DJANGO_TIME_ZONE", "Pacific/Auckland")
        assert _local_time_zone() == "Pacific/Auckland"

    def test_the_page_renders_the_zone_it_is_configured_for(self, client, settings):
        import datetime

        settings.TIME_ZONE = "Europe/Berlin"       # UTC+2 in July
        row = BackupSettings.load()
        row.enabled = True
        row.destination = str(Path(__file__).parent)
        row.last_run_at = row.last_ok_at = datetime.datetime(
            2026, 7, 30, 6, 37, tzinfo=datetime.UTC)
        row.save()
        body = client.get(reverse("transfer:backup")).content.decode()
        assert "08:37" in body, "the timestamp is still being rendered in UTC"


# --- an archive is a file a person picked, so it is hostile -----------
#
# The existing tests here cover the archive *budgets* (over-sized entries, a
# lying header) and the happy path. What they did not cover is the thing that
# actually went wrong: the import wrote whatever bytes the document
# carried under whatever name it asked for, so a crafted .zip could put an
# executable file on the app's own origin. That fix lives in apps/results/logos,
# and this is the test that it is still in the door.

def _crafted_archive(competition, name, content):
    """A real export with its logo replaced by something the archive chose.

    Both halves are what an attacker controls: the *bytes* under media/ and the
    *name* the document asks for them to be saved under.
    """
    document, _media = exporters._event_document(competition)
    document["results"]["pdf_layout"]["image_left"] = name
    return archive.write(document, {name: content})


def test_an_imported_logo_that_is_not_an_image_is_refused(settings, tmp_path):
    """Media is served from this app's own origin, so a file that is not an
    image is not a cosmetic problem: `evil.html` under /media/ is script running
    in the operator's session, from a file they merely *imported*."""
    settings.MEDIA_ROOT = tmp_path
    competition = make_event()
    ResultsPdfLayout.objects.create(competition=competition, header_html="<b>Hi</b>")
    payload = _crafted_archive(competition, "evil.html",
                               b"<script>alert(document.cookie)</script>")
    wipe()

    _plan, result = import_archive(payload)

    layout = ResultsPdfLayout.objects.get(competition=result.competition)
    assert not layout.image_left, "a non-image was accepted as a logo"
    assert not list(Path(tmp_path).rglob("*.html")), "an HTML file was written under MEDIA_ROOT"


def test_an_imported_logo_cannot_choose_its_own_path(settings, tmp_path):
    """The archive names the file. A name is not a thing to be trusted: `../`
    in one is a write outside MEDIA_ROOT, and Django answers that with a
    SuspiciousFileOperation — a 500 on the import page at best."""
    settings.MEDIA_ROOT = tmp_path / "media"
    (tmp_path / "media").mkdir()
    competition = make_event()
    ResultsPdfLayout.objects.create(competition=competition)
    pixel = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
             b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
    payload = _crafted_archive(competition, "../../escaped.gif", pixel)
    wipe()

    _plan, result = import_archive(payload)

    layout = ResultsPdfLayout.objects.get(competition=result.competition)
    # Either refused outright or saved under a name of *our* choosing — never
    # one that climbs out of MEDIA_ROOT.
    if layout.image_left:
        assert ".." not in layout.image_left.name
        assert Path(layout.image_left.path).resolve().is_relative_to(
            Path(settings.MEDIA_ROOT).resolve())
    assert not (tmp_path / "escaped.gif").exists()


def test_a_document_with_a_damaged_value_is_refused_as_a_sentence():
    """apps/transfer/schema runs each field's own validators on the way in, so a
    value SQLite would accept and every later read would choke on is refused
    here — as a TransferError the operator can read, not a traceback."""
    competition = make_event()
    document, media = exporters._event_document(competition)
    for row in document["participants"]:
        row["date_of_birth"] = "not-a-date"
    payload = archive.write(document, media)
    wipe()

    with pytest.raises(TransferError):
        import_archive(payload)


def test_a_document_missing_a_section_the_importer_needs_is_refused():
    """A hand-edited or truncated document, rather than a crafted one — the
    ordinary way a file arrives damaged."""
    competition = make_event()
    document, media = exporters._event_document(competition)
    del document["competition"]
    payload = archive.write(document, media)
    wipe()

    with pytest.raises((TransferError, KeyError)):
        import_archive(payload)


def test_a_competition_name_outside_latin_1_still_downloads(client):
    """The download header is latin-1 encoded, so a Cyrillic event name has to be
    RFC 5987 encoded rather than raising on the way out. Same lesson as the
    results PDF's Content-Disposition — one door per header, not one per page."""
    competition = make_event()
    competition.name = "Слалом"
    competition.save()

    response = client.post(
        reverse("transfer:export"), {"target": "competition", "pk": competition.pk},
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"


def test_a_document_with_two_general_column_rows_is_refused_cleanly(settings, tmp_path):
    """Only one ResultColumnSettings row may have competition_class=None. A
    document carrying two is damaged, and has to come back as a TransferError
    sentence rather than whatever the database raises three sections later."""
    settings.MEDIA_ROOT = tmp_path
    document = {
        "format": "slalomtiming-export", "version": 1, "scope": SCOPE_EVENT,
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
    read, _media = archive.read(archive.write(document, {}))
    plan = importers.plan(read)

    try:
        importers.commit(plan, resolutions={}, media={})
    except TransferError:
        pass  # a sentence the operator can read is the wanted outcome
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"a duplicate General row raised {type(exc).__name__}: {exc}")


# --- an archived event travels archived (issue #8) ---------------------------

def test_an_archived_event_arrives_archived_with_the_settings_it_was_run_under():
    """The document also carries a competition *type* — the current one on the
    source machine, which may already have moved on, and which the target may
    merge into a type of its own. A signed-off event must be re-ranked by
    neither, so its own snapshot travels with it."""
    from apps.competitions import archiving

    competition = make_event()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 99
    competition.competition_type.save()
    payload = exporters.export(competition=competition)
    wipe()

    _plan, result = import_archive(payload)

    imported = result.competition
    assert imported.is_archived
    assert imported.archived_at is not None
    assert imported.rules.pylon_penalty == 5      # the day's, not the 99 above
    assert imported.competition_type.pylon_penalty == 99
    # And the frozen field with it: an archived event renders from these rows and
    # not from the participant tables, so a document carrying the flag without
    # them would arrive as an event with results, settings and nobody in it.
    rows = imported.entry_rows()
    assert [e.bib_number for e in rows] == [7]
    assert rows[0].participant.last_name == "Lovelace"
    assert imported.starters_by_class()


def test_a_live_event_arrives_live():
    competition = make_event()
    payload = exporters.export(competition=competition)
    wipe()

    _plan, result = import_archive(payload)

    assert not result.competition.is_archived
    assert result.competition.archived_rules == {}


# --- drawn numbers travel with the event (issue #11) -------------------------
#
# An event exported mid-registration is the case these exist for: its bibs have
# not been handed out yet, so the *only* record of the draw is the DrawNumber
# rows and the per-class closed flag. A file that dropped them would arrive
# looking like an event nobody had registered for.


def _drawn_event():
    """An event still drawing: one class closed and drawn, one still open with
    a competitor holding a number and no bib."""
    from apps.participants.models import DrawNumber

    competition = make_event()
    competition.uses_draw_numbers = True
    competition.save(update_fields=["uses_draw_numbers"])

    waiting = make_participant(
        competition.competition_type, first="Wait", last="Ing")
    DrawNumber.objects.create(
        competition=competition, participant=waiting, number=12)

    closed = competition.classes.first()
    closed.registration_closed_at = timezone.now()
    closed.save(update_fields=["registration_closed_at"])
    return competition, waiting, closed


def test_an_export_carries_the_draw_setting_and_the_closed_classes():
    competition, _waiting, closed = _drawn_event()

    document, _media = archive.read(exporters.export(competition=competition))

    assert document["competition"]["uses_draw_numbers"] is True
    by_name = {row["name"]: row for row in document["classes"]}
    assert by_name[closed.name]["registration_closed_at"] is not None


def test_an_export_carries_the_numbers_people_drew():
    competition, waiting, _closed = _drawn_event()

    document, _media = archive.read(exporters.export(competition=competition))

    assert [row["number"] for row in document["draw_numbers"]] == [12]
    # The competitor holding it travels too, even though they have no bib and —
    # under age assignment — no class assignment either.
    carried = {row["ref"] for row in document["participants"]}
    assert document["draw_numbers"][0]["participant"] in carried
    assert waiting.pk in carried


def test_an_imported_event_arrives_still_drawing():
    from apps.participants.models import DrawNumber

    competition, _waiting, closed = _drawn_event()
    payload = exporters.export(competition=competition)
    wipe()

    _plan, result = import_archive(payload)
    imported = result.competition

    assert imported.uses_draw_numbers is True
    assert imported.classes.get(name=closed.name).registration_closed
    assert [row.number for row in DrawNumber.objects.filter(competition=imported)] == [12]


def test_a_setup_only_duplicate_starts_with_its_classes_open():
    """The trap in carrying the flag: next year's event copied from this one
    must not refuse every registration before anybody has drawn a number."""
    from apps.competitions import duplication

    competition, _waiting, closed = _drawn_event()

    copy = duplication.copy(competition, with_data=False)

    assert copy.uses_draw_numbers is True
    assert not copy.classes.get(name=closed.name).registration_closed


def test_a_full_duplicate_keeps_the_draw_it_was_run_with():
    from apps.competitions import archiving, duplication
    from apps.participants.models import DrawNumber

    competition, _waiting, closed = _drawn_event()
    # The full copy is the way back into a signed-off event.
    archiving.archive(competition)

    copy = duplication.copy(competition, with_data=True)

    assert copy.classes.get(name=closed.name).registration_closed
    # Only competitors the copy took over — the one still waiting for a bib was
    # never a starter, so the archived event does not carry them.
    assert DrawNumber.objects.filter(competition=copy).count() == 0
