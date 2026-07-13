import datetime

import pytest
from django.db import IntegrityError
from django.urls import reverse

from .models import Competition, CompetitionClass, CompetitionType

pytestmark = pytest.mark.django_db


def make_competition(name="Spring Slalom", year=2026, type_name="Motorcycle"):
    ctype = CompetitionType.objects.create(name=type_name)
    return Competition.objects.create(
        competition_type=ctype, name=name, date=datetime.date(year, 5, 1)
    )


# ----- models -----

def test_saving_competition_creates_one_class_per_code():
    competition = make_competition()
    assert competition.classes.count() == len(CompetitionClass.Code.choices)
    assert set(competition.classes.values_list("code", flat=True)) == {
        code for code, _ in CompetitionClass.Code.choices
    }


def test_competition_type_name_is_unique():
    CompetitionType.objects.create(name="Go-Cart")
    with pytest.raises(IntegrityError):
        CompetitionType.objects.create(name="Go-Cart")


def test_active_classes_lists_only_running_codes_sorted():
    competition = make_competition()
    competition.classes.filter(code__in=["3", "1"]).update(is_running=True)
    assert competition.active_classes() == ["1", "3"]


def test_class_for_birth_year_matches_configured_range():
    competition = make_competition(year=2026)
    cc = competition.classes.get(code="1")
    cc.is_running = True
    cc.age_from = 6
    cc.age_to = 7
    cc.save()
    # 2026 - 2019 = 7, inside 6..7
    assert competition.class_for_birth_year(2019) == cc
    # 2026 - 2010 = 16, outside the range
    assert competition.class_for_birth_year(2010) is None
    assert competition.class_for_birth_year(None) is None


def test_class_for_birth_year_ignores_non_running_classes():
    competition = make_competition(year=2026)
    cc = competition.classes.get(code="1")
    cc.is_running = False
    cc.age_from = 6
    cc.age_to = 7
    cc.save()
    assert competition.class_for_birth_year(2019) is None


def test_birth_year_range_orders_by_field_not_magnitude():
    competition = make_competition(year=2026)
    cc = competition.classes.get(code="2")
    cc.age_from = 6
    cc.age_to = 7
    cc.save()
    # age_to drives the first value, age_from the second
    assert cc.birth_year_range() == (2019, 2020)
    cc.age_from = None
    cc.save()
    assert cc.birth_year_range() is None


def test_get_current_returns_the_active_competition():
    make_competition(name="A", type_name="A")
    second = make_competition(name="B", type_name="B")
    assert Competition.get_current() is None
    second.is_active = True
    second.save()
    assert Competition.get_current() == second


# ----- views -----

def test_create_view_redirects_to_edit(client):
    ctype = CompetitionType.objects.create(name="Motorcycle")
    response = client.post(
        reverse("competitions:add"),
        {"competition_type": ctype.pk, "name": "Autumn Cup", "date": "2026-09-01"},
    )
    competition = Competition.objects.get(name="Autumn Cup")
    assert response.status_code == 302
    assert response.url == reverse("competitions:edit", kwargs={"pk": competition.pk})


def test_edit_view_saves_class_formset(client):
    competition = make_competition()
    classes = list(competition.classes.order_by("code"))
    data = {
        "form-TOTAL_FORMS": str(len(classes)),
        "form-INITIAL_FORMS": str(len(classes)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "competition_type": competition.competition_type_id,
        "name": competition.name,
        "date": competition.date.isoformat(),
    }
    for i, cc in enumerate(classes):
        data[f"form-{i}-id"] = cc.pk
        data[f"form-{i}-is_running"] = "on" if cc.code == "1" else ""
        data[f"form-{i}-age_from"] = "6" if cc.code == "1" else ""
        data[f"form-{i}-age_to"] = "7" if cc.code == "1" else ""

    response = client.post(reverse("competitions:edit", kwargs={"pk": competition.pk}), data)
    assert response.status_code == 302
    cc = competition.classes.get(code="1")
    assert cc.is_running is True
    assert (cc.age_from, cc.age_to) == (6, 7)


def test_select_competition_makes_exactly_one_active(client):
    a = make_competition(name="A", type_name="A")
    b = make_competition(name="B", type_name="B")
    client.post(reverse("competitions:select", kwargs={"pk": a.pk}))
    client.post(reverse("competitions:select", kwargs={"pk": b.pk}))
    a.refresh_from_db()
    b.refresh_from_db()
    assert (a.is_active, b.is_active) == (False, True)
    assert Competition.objects.filter(is_active=True).count() == 1


def test_select_competition_rejects_get(client):
    a = make_competition()
    response = client.get(reverse("competitions:select", kwargs={"pk": a.pk}))
    assert response.status_code == 405


def test_duplicate_competition_copies_class_configuration(client):
    original = make_competition(name="Original")
    cc = original.classes.get(code="1")
    cc.is_running = True
    cc.age_from = 6
    cc.age_to = 7
    cc.save()

    client.post(reverse("competitions:duplicate", kwargs={"pk": original.pk}))
    copy = Competition.objects.get(name="Original (Copy)")
    assert copy.pk != original.pk
    assert copy.is_active is False
    copied = copy.classes.get(code="1")
    assert (copied.is_running, copied.age_from, copied.age_to) == (True, 6, 7)


def test_create_competition_type_via_view(client):
    response = client.post(reverse("competitions:type-add"), {"name": "Skiing"})
    assert response.status_code == 302
    assert CompetitionType.objects.filter(name="Skiing").exists()


def test_delete_unused_competition_type(client):
    ctype = CompetitionType.objects.create(name="Unused")
    response = client.post(reverse("competitions:type-delete", kwargs={"pk": ctype.pk}))
    assert response.status_code == 302
    assert not CompetitionType.objects.filter(pk=ctype.pk).exists()


def test_cannot_delete_competition_type_in_use(client):
    competition = make_competition(type_name="InUse")
    ctype = competition.competition_type
    response = client.post(reverse("competitions:type-delete", kwargs={"pk": ctype.pk}))
    assert response.status_code == 302
    # still there — it's referenced by a competition
    assert CompetitionType.objects.filter(pk=ctype.pk).exists()


def test_delete_competition_type_rejects_get(client):
    ctype = CompetitionType.objects.create(name="ViaGet")
    response = client.get(reverse("competitions:type-delete", kwargs={"pk": ctype.pk}))
    assert response.status_code == 405
    assert CompetitionType.objects.filter(pk=ctype.pk).exists()


def test_type_list_annotates_usage_counts(client):
    competition = make_competition(type_name="Counted")
    ctype = competition.competition_type
    response = client.get(reverse("competitions:type-list"))
    types = {t.pk: t for t in response.context["competition_types"]}
    assert types[ctype.pk].competition_count == 1
    assert types[ctype.pk].participant_count == 0
