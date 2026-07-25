import datetime
import io
import zipfile
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.competitions.models import Competition, CompetitionClass, CompetitionType, MarshalPost
from apps.participants.models import ClassAssignment, EventEntry, Participant
from apps.results.models import ManualTieResolution, ResultColumnSettings, ResultsPdfLayout
from apps.timing.autotiming import slot_key
from apps.timing.models import MarshalPenalty, TimedRun, TimingSignal

from . import archive, csvimport, exporters, importers, merge, staging
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


# --- SEC-5: an archive is a hand-picked file, so its size is not a promise ----


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


# --- SEC-1: markup arriving in a file ----------------------------------------


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


def test_type_action_update_overwrites_the_local_settings():
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99, requires_phone=True)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    importers.commit(plan, resolutions={}, type_action=importers.TYPE_UPDATE, media=media)

    ctype.refresh_from_db()
    assert ctype.pylon_penalty == 5
    assert ctype.requires_phone is False


def test_type_action_reuse_keeps_the_local_settings():
    ctype = make_type(pylon_penalty=5)
    payload = exporters.export(competition_type=ctype)
    CompetitionType.objects.filter(pk=ctype.pk).update(pylon_penalty=99)

    document, media = archive.read(payload)
    plan = importers.plan(document)
    importers.commit(plan, resolutions={}, type_action=importers.TYPE_REUSE, media=media)

    ctype.refresh_from_db()
    assert ctype.pylon_penalty == 99


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
