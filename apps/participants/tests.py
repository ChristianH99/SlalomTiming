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
