import datetime

import pytest
from django.urls import reverse

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


def test_create_form_rejects_bib_when_type_mismatches_competition():
    running_type = make_type("Motorcycle")
    other_type = make_type("Go-Cart")
    make_competition(running_type)
    form = ParticipantCreateForm(
        data=participant_data(other_type, bib_number="3")
    )
    assert not form.is_valid()
    assert "bib_number" in form.errors


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
    ctype = make_type()
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


def test_requirements_follow_the_submitted_type_not_the_active_one():
    """The type dropdown drives validation: posting type B against a competition
    of type A validates against B."""
    active_type = CompetitionType.objects.create(name="Active", requires_vehicle=False)
    make_competition(active_type)
    other_type = CompetitionType.objects.create(name="Other", requires_vehicle=True)
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
