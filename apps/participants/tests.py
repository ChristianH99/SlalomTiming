import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.competitions.models import Competition, CompetitionType

from .forms import ParticipantCreateForm, ParticipantUpdateForm
from .models import EventEntry, Participant

pytestmark = pytest.mark.django_db


def make_type(name="Motorcycle"):
    return CompetitionType.objects.create(name=name)


def make_competition(ctype=None, name="Spring Slalom", year=2026, active=True):
    ctype = ctype or make_type()
    return Competition.objects.create(
        competition_type=ctype,
        name=name,
        date=datetime.date(year, 5, 1),
        is_active=active,
    )


def participant_data(ctype, **overrides):
    data = {
        "competition_type": ctype.pk,
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "2010-06-15",
        "address_street": "1 Main St",
        "address_zip_code": "12345",
        "address_city": "Springfield",
        "club": "Speed Club",
        "license_number": "LIC-001",
        "email": "jane@example.com",
        "phone_number": "",
    }
    data.update(overrides)
    return data


# ----- create form -----

def test_create_form_valid_without_bib():
    ctype = make_type()
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype))
    assert form.is_valid(), form.errors


def test_create_form_assigns_bib_and_view_creates_entry(client):
    ctype = make_type()
    competition = make_competition(ctype)
    response = client.post(
        reverse("participants:add"), participant_data(ctype, bib_number="7")
    )
    assert response.status_code == 302
    participant = Participant.objects.get(last_name="Doe")
    entry = EventEntry.objects.get(participant=participant, competition=competition)
    assert entry.bib_number == 7
    assert entry.status == EventEntry.Status.REGISTERED


def test_create_form_rejects_duplicate_bib():
    ctype = make_type()
    competition = make_competition(ctype)
    existing = Participant.objects.create(
        **{k: v for k, v in participant_data(ctype).items() if k != "competition_type"},
        competition_type=ctype,
    )
    EventEntry.objects.create(participant=existing, competition=competition, bib_number=5)

    form = ParticipantCreateForm(data=participant_data(ctype, bib_number="5", email="x@y.com"))
    assert not form.is_valid()
    assert "bib_number" in form.errors


def test_create_form_registers_under_active_competition_type():
    running_type = make_type("Motorcycle")
    other_type = make_type("Go-Cart")
    make_competition(running_type)
    # A stray posted type is ignored — the participant takes the active type.
    form = ParticipantCreateForm(data=participant_data(other_type, bib_number="3"))
    assert form.is_valid(), form.errors
    participant = form.save()
    assert participant.competition_type == running_type


def test_create_form_disables_bib_without_active_competition():
    ctype = make_type()
    # no active competition
    form = ParticipantCreateForm(data=participant_data(ctype, bib_number="3"))
    assert form.fields["bib_number"].disabled is True
    assert form.is_valid(), form.errors
    # disabled field is ignored, so no bib comes through
    assert form.cleaned_data.get("bib_number") is None


@pytest.mark.parametrize("bad_bib", ["0", "-1", "abc"])
def test_create_form_rejects_invalid_bib_values(bad_bib):
    ctype = make_type()
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, bib_number=bad_bib))
    assert not form.is_valid()
    assert "bib_number" in form.errors


# ----- edge cases: special characters / empty / abuse -----

def test_create_form_accepts_unicode_and_special_characters(client):
    ctype = make_type()
    make_competition(ctype)
    data = participant_data(
        ctype,
        first_name="Renée-Élodie",
        last_name="O'Brien-Müller",
        club="Ski & Board «Zürich»",
        email="renee@example.com",
    )
    response = client.post(reverse("participants:add"), data)
    assert response.status_code == 302
    assert Participant.objects.filter(last_name="O'Brien-Müller").exists()


def test_create_form_does_not_execute_html_in_names():
    ctype = make_type()
    make_competition(ctype)
    payload = '<script>alert(1)</script>'
    form = ParticipantCreateForm(data=participant_data(ctype, first_name=payload))
    assert form.is_valid(), form.errors
    participant = form.save()
    # stored verbatim; escaping is the template layer's job (Django autoescape)
    assert participant.first_name == payload


@pytest.mark.parametrize("missing", [
    "first_name", "last_name", "date_of_birth", "address_street",
    "address_zip_code", "address_city", "club", "license_number", "email",
])
def test_create_form_requires_core_fields(missing):
    ctype = make_type()
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, **{missing: ""}))
    assert not form.is_valid()
    assert missing in form.errors


def test_phone_number_is_optional():
    ctype = make_type()
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, phone_number=""))
    assert form.is_valid(), form.errors


def test_create_form_rejects_invalid_email():
    ctype = make_type()
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, email="not-an-email"))
    assert not form.is_valid()
    assert "email" in form.errors


def test_create_form_rejects_overlong_first_name():
    ctype = make_type()
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, first_name="x" * 101))
    assert not form.is_valid()
    assert "first_name" in form.errors


# ----- update form -----

def _update_form(participant, competition, **data):
    base = participant_data(participant.competition_type)
    base.update(data)
    return ParticipantUpdateForm(data=base, instance=participant, competition=competition)


def test_update_form_can_add_and_remove_bib():
    ctype = make_type()
    competition = make_competition(ctype)
    participant = Participant.objects.create(
        **{k: v for k, v in participant_data(ctype).items() if k != "competition_type"},
        competition_type=ctype,
    )
    # add a bib
    form = _update_form(participant, competition, bib_number="9", status="registered")
    assert form.is_valid(), form.errors
    form.save()
    EventEntry.objects.update_or_create(
        participant=participant, competition=competition,
        defaults={"bib_number": 9, "status": "registered"},
    )
    assert EventEntry.objects.filter(participant=participant, bib_number=9).exists()


def test_update_form_detects_bib_conflict():
    ctype = make_type()
    competition = make_competition(ctype)
    p1 = Participant.objects.create(
        competition_type=ctype, first_name="A", last_name="A",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="1", email="a@a.com",
    )
    p2 = Participant.objects.create(
        competition_type=ctype, first_name="B", last_name="B",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="2", email="b@b.com",
    )
    EventEntry.objects.create(participant=p1, competition=competition, bib_number=4)
    form = ParticipantUpdateForm(
        data=participant_data(ctype, first_name="B", last_name="B",
                              license_number="2", email="b@b.com", bib_number="4"),
        instance=p2, competition=competition,
    )
    assert not form.is_valid()
    assert "bib_number" in form.errors


# ----- list view -----

def test_list_view_defaults_to_showing_all_and_active_only_filters(client):
    ctype = make_type()
    competition = make_competition(ctype)
    active = Participant.objects.create(
        competition_type=ctype, first_name="Active", last_name="Racer",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="1", email="a@a.com",
    )
    Participant.objects.create(
        competition_type=ctype, first_name="Idle", last_name="Bystander",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="2", email="b@b.com",
    )
    EventEntry.objects.create(participant=active, competition=competition, bib_number=1)

    # default now shows everyone
    response = client.get(reverse("participants:list"))
    names = {p.last_name for p in response.context["participants"]}
    assert {"Racer", "Bystander"} <= names

    # active-only is the opt-in
    response = client.get(reverse("participants:list"), {"active": "1"})
    names = {p.last_name for p in response.context["participants"]}
    assert "Racer" in names
    assert "Bystander" not in names


def test_list_view_sorts_by_bib_then_last_name(client):
    ctype = make_type()
    competition = make_competition(ctype)

    def mk(first, last, licence):
        return Participant.objects.create(
            competition_type=ctype, first_name=first, last_name=last,
            date_of_birth=datetime.date(2010, 1, 1), address_street="s",
            address_zip_code="1", address_city="c", club="c",
            license_number=licence, email=f"{licence}@x.com",
        )

    unassigned_z = mk("Zoe", "Zulu", "1")
    unassigned_a = mk("Al", "Alpha", "2")
    bib5 = mk("Bee", "Five", "3")
    bib2 = mk("Cee", "Two", "4")
    EventEntry.objects.create(participant=bib5, competition=competition, bib_number=5)
    EventEntry.objects.create(participant=bib2, competition=competition, bib_number=2)

    response = client.get(reverse("participants:list"))
    order = [p.last_name for p in response.context["participants"]]
    # bibs first (2 then 5), then the unassigned by last name (Alpha, Zulu)
    assert order == ["Two", "Five", "Alpha", "Zulu"]


def test_list_view_search_by_name(client):
    ctype = make_type()
    make_competition(ctype)
    Participant.objects.create(
        competition_type=ctype, first_name="Findme", last_name="Unique",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="1", email="a@a.com",
    )
    response = client.get(reverse("participants:list"), {"q": "Findme"})
    assert len(response.context["participants"]) == 1

    response = client.get(reverse("participants:list"), {"q": "nomatch"})
    assert len(response.context["participants"]) == 0


# ----- duplicate check endpoint -----

def test_participant_check_flags_matching_license_and_name(client):
    # Scoped to the active competition's type, so there has to be one.
    ctype = make_competition().competition_type
    existing = Participant.objects.create(
        competition_type=ctype, first_name="John", last_name="Smith",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="Speed",
        license_number="LIC-9", email="j@x.com",
    )
    # licence match (case-insensitive)
    response = client.get(reverse("participants:check"), {"license_number": "lic-9"})
    data = response.json()
    assert len(data["matches"]) == 1
    assert data["matches"][0]["id"] == existing.pk
    assert "licence" in data["matches"][0]["reason"]

    # name match
    response = client.get(reverse("participants:check"), {"first_name": "john", "last_name": "SMITH"})
    assert response.json()["matches"][0]["id"] == existing.pk

    # excludes self when editing
    response = client.get(
        reverse("participants:check"), {"license_number": "LIC-9", "exclude": str(existing.pk)}
    )
    assert response.json()["matches"] == []

    # no criteria -> no matches
    response = client.get(reverse("participants:check"))
    assert response.json()["matches"] == []


# ----- update view preserves run status -----

def test_update_view_preserves_existing_entry_status(client):
    ctype = make_type()
    competition = make_competition(ctype)
    participant = Participant.objects.create(
        competition_type=ctype, first_name="Keep", last_name="Status",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="1", email="k@x.com",
    )
    EventEntry.objects.create(
        participant=participant, competition=competition,
        bib_number=3, status=EventEntry.Status.DNF,
    )
    data = participant_data(ctype, first_name="Keep", last_name="Status",
                            license_number="1", email="k@x.com", bib_number="8")
    response = client.post(reverse("participants:edit", kwargs={"pk": participant.pk}), data)
    assert response.status_code == 302
    entry = EventEntry.objects.get(participant=participant, competition=competition)
    assert entry.bib_number == 8
    # status must not be reset by the participant form
    assert entry.status == EventEntry.Status.DNF


def test_delete_view_removes_participant_and_entries(client):
    ctype = make_type()
    competition = make_competition(ctype)
    participant = Participant.objects.create(
        competition_type=ctype, first_name="Gone", last_name="Soon",
        date_of_birth=datetime.date(2010, 1, 1), address_street="s",
        address_zip_code="1", address_city="c", club="c",
        license_number="1", email="a@a.com",
    )
    EventEntry.objects.create(participant=participant, competition=competition, bib_number=2)
    client.post(reverse("participants:delete", kwargs={"pk": participant.pk}))
    assert not Participant.objects.filter(pk=participant.pk).exists()
    assert not EventEntry.objects.filter(participant=participant).exists()


# ----- class assignment -----

def make_participant(ctype, license_number="LIC-XYZ"):
    return Participant.objects.create(
        competition_type=ctype,
        first_name="Al",
        last_name="Ice",
        date_of_birth=datetime.date(2010, 6, 15),
        address_street="1 St",
        address_zip_code="1",
        address_city="Town",
        club="Club",
        license_number=license_number,
        email="al@example.com",
    )


def test_manual_assignment_persists_single_class(client):
    from .models import ClassAssignment
    ctype = make_type()
    comp = make_competition(ctype)  # manual, single by default
    comp.classes.filter(name="1").update(is_running=True)
    c1 = comp.classes.get(name="1")
    data = participant_data(ctype, classes=[c1.pk])
    response = client.post(reverse("participants:add"), data)
    assert response.status_code == 302
    p = Participant.objects.get(license_number="LIC-001")
    assert [a.competition_class_id for a in p.class_assignments.all()] == [c1.pk]
    assert comp.classes_for_participant(p) == [c1]


def test_manual_multiple_distinct_classes_when_allowed(client):
    ctype = make_type()
    comp = make_competition(ctype)
    comp.allow_multiple_classes = True
    comp.save()
    comp.classes.filter(name__in=["1", "2"]).update(is_running=True)
    c1, c2 = comp.classes.get(name="1"), comp.classes.get(name="2")
    response = client.post(reverse("participants:add"), participant_data(ctype, classes=[c1.pk, c2.pk]))
    assert response.status_code == 302
    p = Participant.objects.get(license_number="LIC-001")
    assert {a.competition_class_id for a in p.class_assignments.all()} == {c1.pk, c2.pk}


def test_manual_rejects_second_class_when_single(client):
    ctype = make_type()
    comp = make_competition(ctype)  # allow_multiple_classes False
    comp.classes.filter(name__in=["1", "2"]).update(is_running=True)
    c1, c2 = comp.classes.get(name="1"), comp.classes.get(name="2")
    response = client.post(reverse("participants:add"), participant_data(ctype, classes=[c1.pk, c2.pk]))
    assert response.status_code == 200  # re-rendered with error
    assert not Participant.objects.filter(license_number="LIC-001").exists()


def test_manual_same_class_twice_when_repeat_allowed(client):
    ctype = make_type()
    comp = make_competition(ctype)
    comp.classes.filter(name="1").update(is_running=True, allow_multiple_entries=True)
    c1 = comp.classes.get(name="1")
    response = client.post(reverse("participants:add"), participant_data(ctype, classes=[c1.pk, c1.pk]))
    assert response.status_code == 302
    p = Participant.objects.get(license_number="LIC-001")
    assert p.class_assignments.count() == 2
    assert comp.classes_for_participant(p) == [c1, c1]


def test_manual_rejects_repeat_when_not_allowed(client):
    ctype = make_type()
    comp = make_competition(ctype)
    comp.classes.filter(name="1").update(is_running=True)  # repeat off
    c1 = comp.classes.get(name="1")
    response = client.post(reverse("participants:add"), participant_data(ctype, classes=[c1.pk, c1.pk]))
    assert response.status_code == 200
    assert not Participant.objects.filter(license_number="LIC-001").exists()


def test_age_method_stores_no_assignments_but_derives_class(client):
    ctype = make_type()
    comp = make_competition(ctype)
    comp.assignment_method = "age"
    comp.save()
    comp.classes.filter(name="1").update(is_running=True, age_from=6, age_to=99)
    c1 = comp.classes.get(name="1")
    # a stray posted class is ignored in age mode
    response = client.post(reverse("participants:add"), participant_data(ctype, classes=[c1.pk]))
    assert response.status_code == 302
    p = Participant.objects.get(license_number="LIC-001")
    assert p.class_assignments.count() == 0
    assert comp.classes_for_participant(p) == [c1]  # derived from DOB


def test_duplicate_copies_class_assignments_not_bibs(client):
    from .models import ClassAssignment
    ctype = make_type()
    comp = make_competition(ctype)
    comp.classes.filter(name="1").update(is_running=True, allow_multiple_entries=True)
    c1 = comp.classes.get(name="1")
    p = make_participant(ctype)
    ClassAssignment.objects.create(participant=p, competition_class=c1)
    ClassAssignment.objects.create(participant=p, competition_class=c1)  # repeat
    EventEntry.objects.create(participant=p, competition=comp, bib_number=5)

    client.post(reverse("competitions:duplicate", kwargs={"pk": comp.pk}))
    copy = Competition.objects.get(name=f"{comp.name} (Copy)")
    copy_c1 = copy.classes.get(name="1")
    assert ClassAssignment.objects.filter(competition_class=copy_c1, participant=p).count() == 2
    assert not EventEntry.objects.filter(competition=copy).exists()  # bib never copied


# ----- unsaved-changes guard: ?next redirect -----

def test_add_redirects_to_safe_next(client):
    ctype = make_type()
    make_competition(ctype)
    data = participant_data(ctype, next=reverse("competitions:classes"))
    response = client.post(reverse("participants:add"), data)
    assert response.status_code == 302
    assert response.url == reverse("competitions:classes")


def test_add_ignores_unsafe_next(client):
    ctype = make_type()
    make_competition(ctype)
    data = participant_data(ctype, next="https://evil.example.com/steal")
    response = client.post(reverse("participants:add"), data)
    assert response.status_code == 302
    assert response.url == reverse("participants:list")


def test_edit_redirects_to_safe_next(client):
    ctype = make_type()
    make_competition(ctype)
    participant = make_participant(ctype)
    data = participant_data(ctype, next=reverse("competitions:general"), license_number="LIC-XYZ")
    response = client.post(
        reverse("participants:edit", kwargs={"pk": participant.pk}), data
    )
    assert response.status_code == 302
    assert response.url == reverse("competitions:general")


# ----- type-driven participant fields -----

def test_fields_the_type_does_not_collect_are_not_required():
    # A type that collects none of the optional details: name/dob/licence only.
    ctype = CompetitionType.objects.create(
        name="Minimal", requires_address=False, requires_club=False,
        requires_email=False, requires_phone=False,
    )
    make_competition(ctype)
    form = ParticipantCreateForm(data={
        "competition_type": ctype.pk,
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "2010-06-15",
        "license_number": "LIC-001",
    })
    assert form.is_valid(), form.errors


def test_fields_the_type_collects_are_required():
    ctype = CompetitionType.objects.create(name="Rally", requires_vehicle=True)
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, vehicle=""))
    assert not form.is_valid()
    assert "vehicle" in form.errors


def test_optional_collected_field_may_be_blank():
    # Co-driver is collected but not mandatory.
    ctype = CompetitionType.objects.create(name="Rally", requires_co_driver=True)
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, co_driver_first_name=""))
    assert form.is_valid(), form.errors


def test_values_for_uncollected_fields_are_not_saved(client):
    """A value posted for a detail the type doesn't collect is dropped rather
    than stored through a field the form never showed."""
    ctype = CompetitionType.objects.create(name="NoClub", requires_club=False)
    make_competition(ctype)
    response = client.post(
        reverse("participants:add"), participant_data(ctype, club="Sneaky Club"),
    )
    assert response.status_code == 302
    assert Participant.objects.get(license_number="LIC-001").club == ""


def test_requirements_follow_the_active_competition_type():
    """A new participant is validated against the active competition's type,
    regardless of any type posted with the form."""
    active_type = CompetitionType.objects.create(name="Active", requires_vehicle=True)
    make_competition(active_type)
    other_type = CompetitionType.objects.create(name="Other", requires_vehicle=False)
    # Posting a type that doesn't require a vehicle can't relax the active type.
    form = ParticipantCreateForm(data=participant_data(other_type, vehicle=""))
    assert not form.is_valid()
    assert "vehicle" in form.errors


def test_edit_form_uses_the_participants_own_type():
    ctype = CompetitionType.objects.create(name="Rally", requires_vehicle=True)
    competition = make_competition(ctype)
    participant = Participant.objects.create(
        competition_type=ctype, first_name="Jane", last_name="Doe",
        date_of_birth=datetime.date(2010, 6, 15), license_number="LIC-001",
        vehicle="Kart 5",
    )
    form = ParticipantUpdateForm(instance=participant, competition=competition)
    assert form.fields["vehicle"].required is True
    assert form.fields["phone_number"].required is False


def test_licence_not_required_when_type_does_not_collect_it():
    ctype = CompetitionType.objects.create(name="NoLicence", requires_license=False)
    make_competition(ctype)
    form = ParticipantCreateForm(data=participant_data(ctype, license_number=""))
    assert form.is_valid(), form.errors


def test_licence_value_dropped_when_type_does_not_collect_it(client):
    ctype = CompetitionType.objects.create(name="NoLicence", requires_license=False)
    make_competition(ctype)
    response = client.post(
        reverse("participants:add"), participant_data(ctype, license_number="SNEAK-1"),
    )
    assert response.status_code == 302
    assert Participant.objects.get(last_name="Doe").license_number == ""


# ----- no active competition -----

def test_list_view_without_competition_shows_no_participants(client):
    ctype = make_type()
    Participant.objects.create(
        competition_type=ctype, first_name="Ghost", last_name="Racer",
        date_of_birth=datetime.date(2010, 1, 1), license_number="1",
    )
    # No active competition.
    response = client.get(reverse("participants:list"))
    assert response.context["competition"] is None
    assert list(response.context["participants"]) == []


def test_add_view_redirects_without_active_competition(client):
    response = client.get(reverse("participants:add"))
    assert response.status_code == 302
    assert response.url == reverse("participants:list")


def test_list_columns_follow_type_settings(client):
    ctype = make_type("NoLicNoClub")
    ctype.requires_license = False
    ctype.requires_club = False
    ctype.save()
    make_competition(ctype)
    response = client.get(reverse("participants:list"))
    body = response.content.decode()
    assert "requires_license" not in response.context["collected_info"]
    assert "<th class=\"col-licence\">Licence</th>" not in body
    assert "<th class=\"col-club\">Club</th>" not in body
    # A collected detail keeps its column.
    ctype.requires_club = True
    ctype.save()
    body = client.get(reverse("participants:list")).content.decode()
    assert "<th class=\"col-club\">Club</th>" in body


# --- A bib carries its recorded times --------------------------------
# TimedRun.bib_number is a loose integer, so a run belongs to whoever wears the
# number. Swapping two bibs to fix a registration mistake used to hand one
# competitor's times to another with nothing said anywhere.

def _make_person(ctype, first="Alice", last="Ahead", licence="A-1", email="a@x.com"):
    return Participant.objects.create(
        competition_type=ctype, first_name=first, last_name=last,
        date_of_birth=datetime.date(2010, 6, 15), address_street="1 Main St",
        address_zip_code="12345", address_city="Springfield", club="Speed Club",
        license_number=licence, email=email,
    )


def _record_a_run(competition, bib):
    """A run with a real time on it, recorded against a bib number."""
    from apps.timing.models import TimedRun, TimingSignal

    start = TimingSignal.objects.create(
        competition=competition, running_number=bib, port=1,
        device_time=datetime.time(10, 0, 0),
    )
    return TimedRun.objects.create(competition=competition, bib_number=bib, start_signal=start)


def _edit(client, participant, competition, **overrides):
    data = participant_data(participant.competition_type, first_name=participant.first_name,
                            last_name=participant.last_name,
                            license_number=participant.license_number,
                            email=participant.email)
    data.update(overrides)
    return client.post(reverse("participants:edit", kwargs={"pk": participant.pk}), data)


def test_a_bib_with_no_times_on_it_changes_freely(client):
    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)

    assert _edit(client, person, competition, bib_number="8").status_code == 302
    assert EventEntry.objects.get(participant=person).bib_number == 8


def test_changing_a_bib_that_has_times_asks_first(client):
    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    _record_a_run(competition, 7)

    response = _edit(client, person, competition, bib_number="8")

    assert response.status_code == 200                       # the form came back
    effect = response.context["confirm_bib_change"]
    assert (effect["leaving"], effect["arriving"]) == (1, 0)
    assert EventEntry.objects.get(participant=person).bib_number == 7   # nothing moved


def test_taking_over_a_bib_that_already_has_times_asks_too(client):
    """The other direction: whatever was recorded under the new number would
    become this competitor's."""
    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    _record_a_run(competition, 9)

    response = _edit(client, person, competition, bib_number="9")

    effect = response.context["confirm_bib_change"]
    assert (effect["leaving"], effect["arriving"]) == (0, 1)


def test_the_bib_change_goes_through_once_confirmed(client):
    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    _record_a_run(competition, 7)

    response = _edit(client, person, competition, bib_number="8", confirm_bib_change="1")

    assert response.status_code == 302
    assert EventEntry.objects.get(participant=person).bib_number == 8


def test_a_placeholder_row_is_not_a_recorded_time(client):
    """An empty row the operator pre-entered for an upcoming starter has nothing
    on it to lose, so it must not turn an ordinary correction into a warning."""
    from apps.timing.models import TimedRun

    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    TimedRun.objects.create(competition=competition, bib_number=7)

    assert _edit(client, person, competition, bib_number="8").status_code == 302


def _set_bib(client, participant, bib, confirm=False):
    return client.post(
        reverse("participants:set-bib"),
        data={"participant": participant.pk, "bib": bib, "confirm": confirm},
        content_type="application/json",
    ).json()


def _set_dsq(client, participant, dsq):
    return client.post(
        reverse("participants:set-dsq"),
        data={"participant": participant.pk, "dsq": dsq},
        content_type="application/json",
    )


def test_event_disqualification_is_scoped_to_the_active_competition(client):
    """The switch on the participant detail ends their event here and nowhere
    else — the flag lives on the entry, so the same person is untouched at another
    competition."""
    ctype = make_type()
    competition = make_competition(ctype)
    other = Competition.objects.create(
        competition_type=ctype, name="Other", date=datetime.date(2026, 6, 1),
    )
    person = _make_person(ctype)
    here = EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    there = EventEntry.objects.create(participant=person, competition=other, bib_number=7)

    assert _set_dsq(client, person, True).json()["ok"]
    here.refresh_from_db(); there.refresh_from_db()
    assert here.status == EventEntry.Status.DSQ
    assert there.status == EventEntry.Status.REGISTERED

    assert _set_dsq(client, person, False).json()["ok"]
    here.refresh_from_db()
    assert here.status == EventEntry.Status.REGISTERED


def test_event_disqualification_needs_a_registration(client):
    ctype = make_type()
    make_competition(ctype)
    person = _make_person(ctype)   # no bib: not in this event at all

    response = _set_dsq(client, person, True)
    assert response.status_code == 404 and response.json()["ok"] is False


def test_the_inline_bib_field_asks_the_same_question(client):
    """The list's inline field changes exactly the same thing as the edit form,
    so it cannot be the way around the guard."""
    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    _record_a_run(competition, 7)

    response = _set_bib(client, person, "8")

    assert response["ok"] is False
    assert "bib 7" in response["confirm"]
    assert EventEntry.objects.get(participant=person).bib_number == 7

    assert _set_bib(client, person, "8", confirm=True)["ok"] is True
    assert EventEntry.objects.get(participant=person).bib_number == 8


def test_clearing_a_bib_with_times_on_it_asks_too(client):
    """Clearing removes the registration but leaves the times behind under a
    number that can be reissued — the same question."""
    ctype = make_type()
    competition = make_competition(ctype)
    person = _make_person(ctype)
    EventEntry.objects.create(participant=person, competition=competition, bib_number=7)
    _record_a_run(competition, 7)

    assert _set_bib(client, person, "")["ok"] is False
    assert EventEntry.objects.filter(participant=person).exists()

    assert _set_bib(client, person, "", confirm=True)["ok"] is True
    assert not EventEntry.objects.filter(participant=person).exists()


# --- a date of birth has to be a plausible one ----------------------

class TestBirthDateBounds:
    """A slipped keystroke put a competitor in the year 3000 or the year 1200,
    and every age-based class then computed a nonsense age from it."""

    def test_the_form_refuses_a_future_date(self, client):
        competition = make_competition()
        data = participant_data(competition.competition_type)
        data["date_of_birth"] = "3000-01-01"
        client.post(reverse("participants:add"), data)
        assert not Participant.objects.filter(first_name=data["first_name"]).exists()

    def test_the_form_refuses_an_ancient_date(self, client):
        competition = make_competition()
        data = participant_data(competition.competition_type)
        data["date_of_birth"] = "1200-06-06"
        client.post(reverse("participants:add"), data)
        assert not Participant.objects.filter(first_name=data["first_name"]).exists()

    def test_an_ordinary_date_still_saves(self, client):
        competition = make_competition()
        data = participant_data(competition.competition_type)
        data["date_of_birth"] = "2010-06-06"
        client.post(reverse("participants:add"), data)
        assert Participant.objects.filter(first_name=data["first_name"]).exists()

    def test_an_import_document_is_refused_too(self):
        """The validator lives on the model so apps/transfer/schema.py runs it —
        which is what turns a damaged file into a sentence instead of a row every
        later read chokes on."""
        from apps.transfer import schema
        from apps.transfer.schema import TransferError

        with pytest.raises(TransferError):
            schema.load(Participant, {"date_of_birth": "3000-01-01"}, ["date_of_birth"])


# --- two registration desks, one bib ---------------------------------

def test_a_bib_taken_between_the_check_and_the_write_is_reported(client):
    """The uniqueness check is a read and the insert is a write, so two desks can
    both pass it. The database refuses the loser; that must read as "already
    taken", not as a 500."""
    import json as _json

    competition = make_competition()
    first = Participant.objects.create(
        competition_type=competition.competition_type, first_name="A", last_name="One",
        date_of_birth=datetime.date(2010, 1, 1))
    second = Participant.objects.create(
        competition_type=competition.competition_type, first_name="B", last_name="Two",
        date_of_birth=datetime.date(2010, 1, 1))
    EventEntry.objects.create(participant=first, competition=competition, bib_number=7)

    response = client.post(
        reverse("participants:set-bib"),
        data=_json.dumps({"participant": second.pk, "bib": "7", "confirm": True}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "7" in response.json()["error"]


# --- when a participant record was last actually used -----------------
# A personal record kept because it might be needed again stops being kept for
# that reason once it stops being used. updated_at can't answer that question: a
# competitor who has raced every year since 2019 and never changed their address
# has an updated_at of 2019 and is not stale at all.

class TestLastUsed:
    def _participant(self, ctype=None, **kw):
        return Participant.objects.create(
            competition_type=ctype or make_type(),
            first_name=kw.get("first_name", "Ida"),
            last_name=kw.get("last_name", "Nine"),
            date_of_birth=datetime.date(2000, 1, 1),
        )

    def test_creating_one_stamps_it(self):
        assert self._participant().last_used_at is not None

    def test_editing_one_moves_it(self):
        participant = self._participant()
        Participant.objects.filter(pk=participant.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))
        participant.refresh_from_db()
        was = participant.last_used_at

        participant.club = "RRR"
        participant.save()
        participant.refresh_from_db()
        assert participant.last_used_at > was

    def test_a_partial_save_moves_it_too(self):
        """update_fields is how half the app saves. A save that names its fields
        is still an edit, and the stamp has to be added to the list or the write
        silently doesn't include it."""
        participant = self._participant()
        Participant.objects.filter(pk=participant.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))
        participant.refresh_from_db()
        was = participant.last_used_at

        participant.club = "RRR"
        participant.save(update_fields=["club"])
        participant.refresh_from_db()
        assert participant.last_used_at > was
        assert participant.club == "RRR"      # …and the named field still saved

    def test_being_given_a_bib_moves_it(self):
        """The half updated_at cannot see: an entry is a different row, written
        without touching the participant at all."""
        competition = make_competition()
        participant = self._participant(competition.competition_type)
        Participant.objects.filter(pk=participant.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))
        participant.refresh_from_db()
        was, edited = participant.last_used_at, participant.updated_at

        EventEntry.objects.create(participant=participant, competition=competition,
                                  bib_number=4)
        participant.refresh_from_db()
        assert participant.last_used_at > was
        # …and it is not an edit of the participant, so updated_at stays put.
        assert participant.updated_at == edited

    def test_changing_a_bib_moves_it(self):
        competition = make_competition()
        participant = self._participant(competition.competition_type)
        entry = EventEntry.objects.create(participant=participant,
                                          competition=competition, bib_number=4)
        Participant.objects.filter(pk=participant.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))

        entry.bib_number = 5
        entry.save()
        participant.refresh_from_db()
        assert participant.last_used_at.year > 2019

    def test_the_csv_import_counts_as_use(self):
        """A participant already on file who turns up in this year's list is
        reused rather than duplicated (csvimport) — which is precisely a use, and
        the one most likely to be the only sign of it."""
        from apps.transfer import csvimport

        competition = make_competition()
        participant = self._participant(competition.competition_type,
                                        first_name="Ida", last_name="Nine")
        Participant.objects.filter(pk=participant.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))

        # The app's own sample file with our competitor's details written into
        # it, so the test follows whatever columns this type collects rather
        # than pinning a header of its own.
        columns = csvimport.columns_for(competition)
        header = csvimport.sample_csv(competition).lstrip("﻿").splitlines()[0]
        values = {"first_name": "Ida", "last_name": "Nine",
                  "date_of_birth": "2000-01-01", "email": "ida@example.de"}
        row = ";".join(values.get(c.key, c.example or "x") for c in columns)
        csv = f"{header}\n{row}\n".encode("utf-8")
        report = csvimport.read(csv, competition)
        assert report.ok, report.errors
        csvimport.commit(report, competition)
        assert Participant.objects.count() == 1      # reused, not duplicated
        participant.refresh_from_db()
        assert participant.last_used_at.year > 2019

    def test_reading_a_participant_is_not_a_use(self):
        """Rendering the list, a start order or a results table must not refresh
        the stamp, or nothing would ever age at all."""
        competition = make_competition()
        participant = self._participant(competition.competition_type)
        EventEntry.objects.create(participant=participant, competition=competition,
                                  bib_number=4)
        Participant.objects.filter(pk=participant.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))

        from django.test import Client
        client = Client()
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_superuser("looker", "l@x.de", "pw")
        client.force_login(user)
        client.get(reverse("participants:list"))
        client.get(reverse("results:index"))

        participant.refresh_from_db()
        assert participant.last_used_at.year == 2019

    def test_the_stale_ones_can_be_found_in_one_query(self, django_assert_num_queries):
        """What the field is for: a bulk delete by age asks the whole table, so
        the column is indexed and the question is one query."""
        ctype = make_type()
        old = self._participant(ctype, last_name="Old")
        fresh = self._participant(ctype, last_name="Fresh")
        Participant.objects.filter(pk=old.pk).update(
            last_used_at=datetime.datetime(2019, 1, 1, tzinfo=datetime.UTC))

        cutoff = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
        with django_assert_num_queries(1):
            stale = list(Participant.objects.filter(last_used_at__lt=cutoff))
        assert [p.pk for p in stale] == [old.pk]
        assert fresh.pk not in [p.pk for p in stale]


def test_the_duplicate_check_does_not_reach_into_another_discipline(client):
    """participant_check used to search Participant.objects.all(), so the
    response carried the name, club and licence number of people registered under
    a discipline the caller has nothing to do with — an unthrottled licence-number
    oracle for anyone holding the Participants page."""
    other = make_type("Go-Cart")
    Participant.objects.create(
        competition_type=other, first_name="Erika", last_name="Mustermann",
        date_of_birth=datetime.date(1990, 5, 5),
        license_number="SECRET-1", club="Geheimclub",
    )
    make_competition()  # active type is Motorcycle, not Go-Cart

    by_license = client.get(reverse("participants:check"), {"license_number": "SECRET-1"})
    by_name = client.get(
        reverse("participants:check"), {"first_name": "Erika", "last_name": "Mustermann"},
    )

    assert by_license.json()["matches"] == []
    assert by_name.json()["matches"] == []


# ----- a date must survive the round trip to the screen (issue #1) -----
# The suite pins the UI to English (conftest), and English's first
# DATE_INPUT_FORMATS entry happens to be ISO — which is exactly the format
# `<input type="date">` requires. So every test rendering an edit form passed
# while the shipped default language, German, rendered "15.06.2010" into an
# input that silently ignores anything but ISO, and the operator saw an empty
# birthday. These two run in German on purpose.

def test_an_existing_birthday_is_still_in_the_edit_form_in_german(client, settings):
    settings.LANGUAGE_CODE = "de"
    ctype = make_type()
    make_competition(ctype)
    participant = make_participant(ctype)

    page = client.get(reverse("participants:edit", args=[participant.pk])).content.decode()

    assert 'value="2010-06-15"' in page, (
        "the date input needs an ISO value; anything else and the browser shows "
        "an empty field, which is what the operator reads as a deleted birthday"
    )


def test_a_german_edit_screen_can_save_the_date_it_was_given(client, settings):
    """The other half: `<input type="date">` always posts ISO, whatever the page
    language, and German's DATE_INPUT_FORMATS does not contain it — so the form
    used to reject the browser's own value with "Enter a valid date"."""
    settings.LANGUAGE_CODE = "de"
    ctype = make_type()
    make_competition(ctype)
    participant = make_participant(ctype)

    response = client.post(
        reverse("participants:edit", args=[participant.pk]),
        participant_data(ctype, date_of_birth="2011-07-16", license_number="LIC-XYZ"),
    )

    assert response.status_code == 302, getattr(response, "context_data", {}).get("form")
    participant.refresh_from_db()
    assert participant.date_of_birth == datetime.date(2011, 7, 16)


# --- drawn numbers and automatic bib assignment (issue #11) ------------------
#
# The registration desk stops deciding what number somebody wears. It hands out
# a drawn number instead, and the bibs come from it later, per class. Three
# things carry the whole feature and each has its own trap:
#
#   * a drawn number is *not* an EventEntry (an entry is a bib — see
#     participants/models.DrawNumber for why that invariant is worth keeping),
#   * a bib somebody already has is never moved by the draw,
#   * closing a class ends its draw, and after that a bib has to be typed.


def _drawing_competition(ctype=None):
    """An event that draws numbers, with class 1 running."""
    ctype = ctype or make_type()
    comp = make_competition(ctype)
    comp.uses_draw_numbers = True
    comp.save(update_fields=["uses_draw_numbers"])
    comp.classes.filter(name="1").update(is_running=True)
    return comp


def _entered(comp, ctype, first, draw_number=None, bib=None, cclass=None):
    from .models import ClassAssignment, DrawNumber

    person = Participant.objects.create(
        competition_type=ctype, first_name=first, last_name="Racer",
        date_of_birth=datetime.date(2010, 6, 15),
    )
    if cclass is not None:
        ClassAssignment.objects.create(participant=person, competition_class=cclass)
    if draw_number is not None:
        DrawNumber.objects.create(
            competition=comp, participant=person, number=draw_number)
    if bib is not None:
        EventEntry.objects.create(competition=comp, participant=person, bib_number=bib)
    return person


def test_a_drawn_number_is_not_a_starter():
    """The invariant the whole design rests on: somebody who has drawn a number
    but has no bib is registered and is *not* yet in the event."""
    from .models import DrawNumber

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=3, cclass=cclass)

    assert DrawNumber.objects.filter(competition=comp).count() == 1
    assert EventEntry.objects.filter(competition=comp).count() == 0


def test_bibs_are_handed_out_in_draw_order():
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=9, cclass=cclass)
    _entered(comp, ctype, "Bea", draw_number=2, cclass=cclass)
    _entered(comp, ctype, "Cy", draw_number=5, cclass=cclass)

    plan = draw.plan(comp, cclass)
    assert [(row.draw_number, row.bib_number) for row in plan.assignments] == [
        (2, 1), (5, 2), (9, 3)
    ]


def test_the_order_can_be_reversed():
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=9, cclass=cclass)
    _entered(comp, ctype, "Bea", draw_number=2, cclass=cclass)

    plan = draw.plan(comp, cclass, order=draw.DESCENDING)
    assert [(row.draw_number, row.bib_number) for row in plan.assignments] == [
        (9, 1), (2, 2)
    ]


def test_assignment_can_start_at_a_chosen_bib():
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)
    _entered(comp, ctype, "Bea", draw_number=2, cclass=cclass)

    plan = draw.plan(comp, cclass, start=100)
    assert [row.bib_number for row in plan.assignments] == [100, 101]


def test_a_hand_typed_bib_is_kept_and_its_number_skipped():
    """The rule that makes the manual bib field still mean something: a bib
    somebody typed is a decision, so the draw allocates *around* it."""
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Champ", bib=2, cclass=cclass)
    _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)
    _entered(comp, ctype, "Bea", draw_number=2, cclass=cclass)

    plan = draw.plan(comp, cclass, start=1)
    assert [(row.participant.first_name, row.bib_number, row.kept)
            for row in plan.assignments] == [
        ("Ada", 1, False), ("Champ", 2, True), ("Bea", 3, False),
    ]


def test_a_bib_taken_by_another_class_is_stepped_over():
    """A bib is unique to the *event*, so drawing a second class continues past
    the first one's numbers rather than colliding with them."""
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    comp.classes.filter(name="2").update(is_running=True)
    one, two = comp.classes.get(name="1"), comp.classes.get(name="2")
    _entered(comp, ctype, "Ada", bib=1, cclass=one)
    _entered(comp, ctype, "Bea", draw_number=4, cclass=two)

    plan = draw.plan(comp, two)
    assert [row.bib_number for row in plan.assignments] == [2]


def test_somebody_with_neither_number_is_passed_over():
    """Registered under the discipline but not part of this event: no drawn
    number, no bib, so nothing to hand out. They are skipped rather than given
    a number — and deliberately not counted anywhere either, because the class's
    membership includes every participant of every past season."""
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    ada = _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)
    forgotten = _entered(comp, ctype, "Nobody", cclass=cclass)

    plan = draw.plan(comp, cclass)

    assigned = [row.participant.pk for row in plan.assignments]
    assert assigned == [ada.pk]
    assert forgotten.pk not in assigned


def test_committing_writes_the_bibs_and_closes_the_class():
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=2, cclass=cclass)
    _entered(comp, ctype, "Bea", draw_number=1, cclass=cclass)

    created = draw.commit(comp, cclass, draw.plan(comp, cclass))
    cclass.refresh_from_db()

    assert created == 2
    assert cclass.registration_closed
    assert sorted(
        EventEntry.objects.filter(competition=comp)
        .values_list("bib_number", flat=True)
    ) == [1, 2]


def test_age_based_assignment_is_drawn_too():
    """The draw asks the *competition* which class somebody is in, so an event
    that resolves classes by age is drawn like any other."""
    from . import draw

    ctype = make_type()
    comp = _drawing_competition(ctype)
    comp.assignment_method = "age"
    comp.save(update_fields=["assignment_method"])
    cclass = comp.classes.get(name="1")
    cclass.age_from, cclass.age_to = 10, 20
    cclass.save()
    _entered(comp, ctype, "Ada", draw_number=1)

    plan = draw.plan(comp, cclass)
    assert [row.bib_number for row in plan.assignments] == [1]


# --- the page ----------------------------------------------------------------


def test_the_page_is_hidden_when_the_event_does_not_draw_numbers(client):
    ctype = make_type()
    comp = make_competition(ctype)  # uses_draw_numbers is off by default

    assert comp.uses_draw_numbers is False
    assert client.get(reverse("participants:bib-assignment")).status_code == 302
    assert reverse("participants:bib-assignment") not in \
        client.get(reverse("participants:list")).content.decode()


def test_the_page_is_offered_when_the_event_draws_numbers(client):
    _drawing_competition()

    assert client.get(reverse("participants:bib-assignment")).status_code == 200
    assert reverse("participants:bib-assignment") in \
        client.get(reverse("participants:list")).content.decode()


def test_the_page_previews_before_it_writes(client):
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)

    page = client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "order": "asc", "start": "1",
    })

    assert page.status_code == 200
    assert "Ada" in page.content.decode()
    # Nothing written until the second post carries `confirm`.
    assert EventEntry.objects.filter(competition=comp).count() == 0
    cclass.refresh_from_db()
    assert not cclass.registration_closed


def test_the_page_writes_when_confirmed(client):
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    person = _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)

    client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "order": "asc", "start": "1", "confirm": "1",
    })

    cclass.refresh_from_db()
    assert cclass.registration_closed
    assert EventEntry.objects.get(competition=comp, participant=person).bib_number == 1


def test_a_closed_class_is_not_drawn_twice(client):
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)
    client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "confirm": "1"})
    _entered(comp, ctype, "Late", draw_number=2, cclass=cclass)

    client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "confirm": "1"})

    assert EventEntry.objects.filter(competition=comp).count() == 1


# --- the participant form ----------------------------------------------------


def test_the_draw_field_is_absent_when_the_event_does_not_draw_numbers():
    ctype = make_type()
    make_competition(ctype)
    assert "draw_number" not in ParticipantCreateForm().fields


def test_the_draw_field_is_offered_and_saved(client):
    from .models import DrawNumber

    ctype = make_type()
    comp = _drawing_competition(ctype)
    assert "draw_number" in ParticipantCreateForm().fields

    client.post(reverse("participants:add"),
                participant_data(ctype, draw_number=7))

    person = Participant.objects.get(license_number="LIC-001")
    assert DrawNumber.objects.get(competition=comp, participant=person).number == 7
    # A drawn number alone does not make them a starter.
    assert not EventEntry.objects.filter(competition=comp, participant=person).exists()


def test_two_people_cannot_hold_one_drawn_number():
    ctype = make_type()
    comp = _drawing_competition(ctype)
    _entered(comp, ctype, "Ada", draw_number=7)

    form = ParticipantCreateForm(data=participant_data(ctype, draw_number=7))
    assert not form.is_valid()
    assert "draw_number" in form.errors


def test_joining_a_closed_class_needs_a_bib_by_hand(client):
    """Posted through the client rather than built by hand: the class selection
    is read with ``getlist``, so a plain dict silently selects no class at all
    and the rule under test never gets a class to look at."""
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    cclass.registration_closed_at = timezone.now()
    cclass.save(update_fields=["registration_closed_at"])

    data = participant_data(ctype, classes=[cclass.pk], draw_number=9)
    refused = client.post(reverse("participants:add"), data)
    assert refused.status_code == 200          # re-rendered, not saved
    assert "bib_number" in refused.context_data["form"].errors
    assert not Participant.objects.filter(license_number="LIC-001").exists()

    data["bib_number"] = 50
    accepted = client.post(reverse("participants:add"), data)
    assert accepted.status_code == 302, accepted.context_data["form"].errors
    person = Participant.objects.get(license_number="LIC-001")
    assert EventEntry.objects.get(competition=comp, participant=person).bib_number == 50


def test_editing_somebody_already_in_a_closed_class_is_not_blocked(client):
    """The rule is about *joining* a closed class. Refusing to save a corrected
    phone number for somebody who was in the draw would make the participant
    screen unusable for the rest of the event."""
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    person = _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)
    cclass.registration_closed_at = timezone.now()
    cclass.save(update_fields=["registration_closed_at"])

    response = client.post(
        reverse("participants:edit", args=[person.pk]),
        participant_data(ctype, first_name="Ada", last_name="Racer",
                         classes=[cclass.pk], phone_number="0123"),
    )

    assert response.status_code == 302, response.context_data["form"].errors
    person.refresh_from_db()
    assert person.phone_number == "0123"


def _age_drawing_competition(ctype=None):
    """A drawing event that resolves classes by age, with class 1 covering
    10-to-20-year-olds and its registration closed."""
    ctype = ctype or make_type()
    comp = _drawing_competition(ctype)
    comp.assignment_method = "age"
    comp.save(update_fields=["assignment_method"])
    cclass = comp.classes.get(name="1")
    cclass.age_from, cclass.age_to = 10, 20
    cclass.registration_closed_at = timezone.now()
    cclass.save()
    return comp, cclass


def test_age_assignment_also_needs_a_bib_for_a_closed_class(client):
    ctype = make_type()
    _age_drawing_competition(ctype)

    refused = client.post(reverse("participants:add"), participant_data(ctype))
    assert refused.status_code == 200
    assert "bib_number" in refused.context_data["form"].errors


def test_age_assignment_does_not_block_editing_an_existing_competitor(client):
    """The mirror of the manual case, and the one that is easy to get wrong:
    age assignment keeps no assignment rows, so "were they already in this
    class?" has to be asked of their stored date of birth rather than of an
    `initial_classes` list that is only ever filled for manual assignment."""
    ctype = make_type()
    comp, _cclass = _age_drawing_competition(ctype)
    person = _entered(comp, ctype, "Ada", draw_number=1)

    response = client.post(
        reverse("participants:edit", args=[person.pk]),
        participant_data(ctype, first_name="Ada", last_name="Racer",
                         date_of_birth="2010-06-15", phone_number="0456"),
    )

    assert response.status_code == 302, response.context_data["form"].errors
    person.refresh_from_db()
    assert person.phone_number == "0456"


# --- the drawn number, typed straight into the participant list --------------
#
# The desk's actual job. It used to need the full edit form per competitor,
# which is the slow path on the one screen that is busy exactly when a queue is
# forming in front of it.


def _set_draw(client, participant, value):
    return client.post(
        reverse("participants:set-draw"),
        data={"participant": participant.pk, "draw": value},
        content_type="application/json",
    ).json()


def test_a_drawn_number_can_be_entered_from_the_list(client):
    from .models import DrawNumber

    ctype = make_type()
    comp = _drawing_competition(ctype)
    person = _entered(comp, ctype, "Ada", cclass=comp.classes.get(name="1"))

    assert _set_draw(client, person, "14") == {"ok": True, "draw": 14}
    assert DrawNumber.objects.get(competition=comp, participant=person).number == 14


def test_entering_a_drawn_number_creates_no_starter(client):
    """The invariant, asked of the new door: a drawn number is not a bib, so
    typing one must not quietly enter somebody in the event."""
    ctype = make_type()
    comp = _drawing_competition(ctype)
    person = _entered(comp, ctype, "Ada", cclass=comp.classes.get(name="1"))

    _set_draw(client, person, "14")

    assert not EventEntry.objects.filter(competition=comp, participant=person).exists()


def test_a_drawn_number_can_be_changed_and_cleared(client):
    from .models import DrawNumber

    ctype = make_type()
    comp = _drawing_competition(ctype)
    person = _entered(comp, ctype, "Ada", draw_number=3)

    assert _set_draw(client, person, "9")["draw"] == 9
    assert DrawNumber.objects.get(competition=comp, participant=person).number == 9

    assert _set_draw(client, person, "") == {"ok": True, "draw": None}
    assert not DrawNumber.objects.filter(competition=comp, participant=person).exists()


def test_a_drawn_number_somebody_else_holds_is_refused_by_name(client):
    """Two people on one ticket is the argument the draw exists to prevent, so
    the answer names who has it — the desk can then go and ask them."""
    ctype = make_type()
    comp = _drawing_competition(ctype)
    _entered(comp, ctype, "Ada", draw_number=14)
    bea = _entered(comp, ctype, "Bea")

    result = _set_draw(client, bea, "14")

    assert result["ok"] is False
    assert "Ada" in result["error"]


def test_keeping_your_own_drawn_number_is_not_a_clash(client):
    ctype = make_type()
    comp = _drawing_competition(ctype)
    ada = _entered(comp, ctype, "Ada", draw_number=14)

    assert _set_draw(client, ada, "14")["ok"] is True


@pytest.mark.parametrize("value", ["0", "-3", "abc", "1e5", "10000"])
def test_a_drawn_number_the_column_cannot_hold_is_refused(client, value):
    """SQLite stores an out-of-range integer rather than refusing it, so the
    bound is here — the same reason the timing views bound theirs."""
    from .models import DrawNumber

    ctype = make_type()
    comp = _drawing_competition(ctype)
    person = _entered(comp, ctype, "Ada")

    assert _set_draw(client, person, value)["ok"] is False
    assert not DrawNumber.objects.filter(competition=comp, participant=person).exists()


def test_the_endpoint_refuses_when_the_event_does_not_draw_numbers(client):
    """Hiding the field is not the same as closing the door."""
    from .models import DrawNumber

    ctype = make_type()
    comp = make_competition(ctype)          # uses_draw_numbers stays False
    person = _entered(comp, ctype, "Ada")

    response = client.post(
        reverse("participants:set-draw"),
        data={"participant": person.pk, "draw": "5"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert not DrawNumber.objects.filter(competition=comp).exists()


def test_the_list_offers_a_draw_field_only_while_drawing(client):
    ctype = make_type()
    comp = _drawing_competition(ctype)
    _entered(comp, ctype, "Ada", cclass=comp.classes.get(name="1"))

    drawing = client.get(reverse("participants:list")).content.decode()
    assert "data-draw-input" in drawing

    comp.uses_draw_numbers = False
    comp.save(update_fields=["uses_draw_numbers"])

    plain = client.get(reverse("participants:list")).content.decode()
    assert "data-draw-input" not in plain


# --- drawing a class again ---------------------------------------------------


def _closed_class_with_draws(ctype=None):
    """A class already drawn once: Ada drew 1 and wears bib 1, Bea drew 2 and
    wears bib 2, and Cid was typed bib 50 by hand without drawing."""
    from . import draw as draw_mod

    ctype = ctype or make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)
    _entered(comp, ctype, "Bea", draw_number=2, cclass=cclass)
    _entered(comp, ctype, "Cid", bib=50, cclass=cclass)
    draw_mod.commit(comp, cclass, draw_mod.plan(comp, cclass))
    cclass.refresh_from_db()
    return comp, ctype, cclass


def test_a_class_is_drawn_once_and_then_closed():
    comp, _ctype, cclass = _closed_class_with_draws()

    assert cclass.registration_closed
    assert dict(
        EventEntry.objects.filter(competition=comp)
        .values_list("participant__first_name", "bib_number")
    ) == {"Ada": 1, "Bea": 2, "Cid": 50}


def test_redrawing_reverses_the_order_of_the_drawn_competitors():
    """The point of the button: the same people, drawn the other way round."""
    from . import draw as draw_mod

    comp, _ctype, cclass = _closed_class_with_draws()

    plan = draw_mod.plan(comp, cclass, order=draw_mod.DESCENDING, start=1,
                         reassign=True)
    draw_mod.commit(comp, cclass, plan)

    assert dict(
        EventEntry.objects.filter(competition=comp)
        .values_list("participant__first_name", "bib_number")
    ) == {"Bea": 1, "Ada": 2, "Cid": 50}


def test_a_redraw_never_moves_a_bib_typed_in_by_hand():
    """Cid never drew a number, so his 50 is a decision rather than an
    allocation — the re-draw allocates around it exactly as the first one did."""
    from . import draw as draw_mod

    comp, _ctype, cclass = _closed_class_with_draws()

    plan = draw_mod.plan(comp, cclass, order=draw_mod.DESCENDING, start=1,
                         reassign=True)

    kept = [row for row in plan.assignments if row.kept]
    assert [(row.participant.first_name, row.bib_number) for row in kept] == [("Cid", 50)]
    assert [person.first_name for person, _bib in plan.released] == ["Ada", "Bea"]


def test_a_redraw_can_reuse_the_numbers_it_hands_back():
    """The bibs being released are freed *before* the allocation, so a class
    re-drawn into its own block keeps that block instead of being pushed past
    it — which is what would happen if they still counted as taken."""
    from . import draw as draw_mod

    comp, _ctype, cclass = _closed_class_with_draws()

    plan = draw_mod.plan(comp, cclass, order=draw_mod.DESCENDING, start=1,
                         reassign=True)

    assert sorted(row.bib_number for row in plan.assignments) == [1, 2, 50]


def test_a_redraw_says_which_numbers_move():
    from . import draw as draw_mod

    comp, _ctype, cclass = _closed_class_with_draws()

    plan = draw_mod.plan(comp, cclass, order=draw_mod.DESCENDING, start=1,
                         reassign=True)

    moved = {
        row.participant.first_name: (row.previous_bib, row.bib_number)
        for row in plan.assignments if row.previous_bib
    }
    assert moved == {"Ada": (1, 2), "Bea": (2, 1)}


def test_a_redraw_keeps_a_whole_event_disqualification():
    """The entry row is deleted and remade, but a DSQ is about the competitor,
    not about the number they were wearing."""
    from . import draw as draw_mod

    comp, _ctype, cclass = _closed_class_with_draws()
    EventEntry.objects.filter(
        competition=comp, participant__first_name="Ada"
    ).update(status=EventEntry.Status.DSQ)

    draw_mod.commit(comp, cclass, draw_mod.plan(
        comp, cclass, order=draw_mod.DESCENDING, start=1, reassign=True))

    ada = EventEntry.objects.get(competition=comp, participant__first_name="Ada")
    assert ada.status == EventEntry.Status.DSQ
    assert ada.bib_number == 2


def test_a_plain_draw_of_a_closed_class_is_still_refused(client):
    """Only the re-draw door opens a closed class; the ordinary one stays shut."""
    comp, _ctype, cclass = _closed_class_with_draws()

    response = client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "order": "asc", "confirm": "1",
    })

    assert response.status_code == 302
    assert EventEntry.objects.get(
        competition=comp, participant__first_name="Ada").bib_number == 1


def test_the_page_offers_a_redraw_only_once_a_class_is_closed(client):
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Ada", draw_number=1, cclass=cclass)

    open_page = client.get(reverse("participants:bib-assignment")).content.decode()
    assert 'name="reassign"' not in open_page

    from . import draw as draw_mod
    draw_mod.commit(comp, cclass, draw_mod.plan(comp, cclass))

    closed_page = client.get(reverse("participants:bib-assignment")).content.decode()
    assert 'name="reassign"' in closed_page


def test_the_redraw_confirmation_counts_the_times_it_would_move(client):
    """The consequence that is easy to miss: a time is recorded against the
    number, so re-drawing a class that has already run re-attaches its times."""
    comp, _ctype, cclass = _closed_class_with_draws()
    _record_a_run(comp, bib=1)
    _record_a_run(comp, bib=2)

    response = client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "order": "desc", "start": "1",
        "reassign": "1",
    })

    assert response.status_code == 200
    assert response.context["released_count"] == 2
    assert response.context["released_times"] == 2


def test_redrawing_through_the_page_writes_the_new_numbers(client):
    comp, _ctype, cclass = _closed_class_with_draws()

    response = client.post(reverse("participants:bib-assignment"), {
        "competition_class": cclass.pk, "order": "desc", "start": "1",
        "reassign": "1", "confirm": "1",
    })

    assert response.status_code == 302
    assert EventEntry.objects.get(
        competition=comp, participant__first_name="Bea").bib_number == 1


# --- the list puts the event's own competitors first --------------------------


def test_the_list_lifts_bibs_then_drawn_numbers_above_the_register(client):
    """A busy desk wants the people who are actually here at the top: bibs in
    bib order, then whoever has drawn and is waiting, then the rest of the
    discipline's register by name."""
    ctype = make_type()
    comp = _drawing_competition(ctype)
    cclass = comp.classes.get(name="1")
    _entered(comp, ctype, "Zoe", bib=2, cclass=cclass)
    _entered(comp, ctype, "Yara", bib=1, cclass=cclass)
    _entered(comp, ctype, "Xena", draw_number=9, cclass=cclass)
    _entered(comp, ctype, "Wilma", draw_number=4, cclass=cclass)
    _entered(comp, ctype, "Alice", cclass=cclass)      # neither
    _entered(comp, ctype, "Bob", cclass=cclass)        # neither

    response = client.get(reverse("participants:list"))
    order = [p.first_name for p in response.context["participants"]]

    assert order == ["Yara", "Zoe", "Wilma", "Xena", "Alice", "Bob"]
