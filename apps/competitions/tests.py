import datetime
import json

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

def test_saving_competition_seeds_default_classes():
    competition = make_competition()
    assert competition.classes.count() == len(CompetitionClass.DEFAULT_NAMES)
    assert set(competition.classes.values_list("name", flat=True)) == set(
        CompetitionClass.DEFAULT_NAMES
    )
    # seeded in order with incremental positions
    assert list(competition.classes.order_by("position").values_list("name", flat=True)) == (
        CompetitionClass.DEFAULT_NAMES
    )


def test_competition_type_name_is_unique():
    CompetitionType.objects.create(name="Go-Cart")
    with pytest.raises(IntegrityError):
        CompetitionType.objects.create(name="Go-Cart")


def test_active_classes_lists_only_running_names_in_order():
    competition = make_competition()
    competition.classes.filter(name__in=["3", "1"]).update(is_running=True)
    # unplaced running classes fall back to list position order (1 before 3)
    assert competition.active_classes() == ["1", "3"]


def test_active_classes_follows_run_position_then_position():
    competition = make_competition()
    competition.classes.filter(name__in=["1", "2", "3"]).update(is_running=True)
    # place 3 in run 0, 1 and 2 in run 1 -> run order beats list order
    competition.classes.filter(name="3").update(run_position=0)
    competition.classes.filter(name__in=["1", "2"]).update(run_position=1)
    assert competition.active_classes() == ["3", "1", "2"]


def test_run_groups_groups_by_run_position_and_appends_unplaced():
    competition = make_competition()
    competition.classes.filter(name__in=["1", "2", "3", "4"]).update(is_running=True)
    competition.classes.filter(name__in=["1", "3"]).update(run_position=0)
    competition.classes.filter(name="2").update(run_position=1)
    # 4 stays unplaced (run_position=None) -> its own run at the end
    groups = [[cc.name for cc in run] for run in competition.run_groups()]
    assert groups == [["1", "3"], ["2"], ["4"]]


def test_class_for_birth_year_matches_configured_range():
    competition = make_competition(year=2026)
    cc = competition.classes.get(name="1")
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
    cc = competition.classes.get(name="1")
    cc.is_running = False
    cc.age_from = 6
    cc.age_to = 7
    cc.save()
    assert competition.class_for_birth_year(2019) is None


def test_birth_year_range_orders_by_field_not_magnitude():
    competition = make_competition(year=2026)
    cc = competition.classes.get(name="2")
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

def make_active_competition(**kwargs):
    competition = make_competition(**kwargs)
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    return competition


def test_create_view_sets_active_and_redirects_to_general(client):
    ctype = CompetitionType.objects.create(name="Motorcycle")
    response = client.post(
        reverse("competitions:add"),
        {"competition_type": ctype.pk, "name": "Autumn Cup", "date": "2026-09-01"},
    )
    competition = Competition.objects.get(name="Autumn Cup")
    assert response.status_code == 302
    assert response.url == reverse("competitions:general")
    assert competition.is_active is True


def test_general_view_saves_active_competition(client):
    competition = make_active_competition(name="Old name")
    response = client.post(reverse("competitions:general"), {
        "competition_type": competition.competition_type_id,
        "name": "New name",
        "date": "2027-03-04",
    })
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.name == "New name"
    assert competition.date == datetime.date(2027, 3, 4)


def classes_post_data(competition, overrides=None, extra_rows=None):
    """Build POST payload for the Classes page from the current classes.
    `overrides` maps a class name to a dict of field values for that row;
    `extra_rows` is a list of dicts for brand-new (unsaved) class rows."""
    overrides = overrides or {}
    extra_rows = extra_rows or []
    classes = list(competition.classes.order_by("position", "name"))
    data = {
        "form-INITIAL_FORMS": str(len(classes)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
    }
    for i, cc in enumerate(classes):
        ov = overrides.get(cc.name, {})
        data[f"form-{i}-id"] = cc.pk
        data[f"form-{i}-name"] = ov.get("name", cc.name)
        data[f"form-{i}-is_running"] = "on" if ov.get("is_running") else ""
        data[f"form-{i}-age_from"] = ov.get("age_from", "")
        data[f"form-{i}-age_to"] = ov.get("age_to", "")
        data[f"form-{i}-practice_runs"] = ov.get("practice_runs", "1")
        data[f"form-{i}-counted_runs"] = ov.get("counted_runs", "2")
        if ov.get("DELETE"):
            data[f"form-{i}-DELETE"] = "on"
    base = len(classes)
    for j, row in enumerate(extra_rows):
        i = base + j
        data[f"form-{i}-id"] = ""
        data[f"form-{i}-name"] = row.get("name", f"New{j}")
        data[f"form-{i}-is_running"] = "on" if row.get("is_running") else ""
        data[f"form-{i}-age_from"] = row.get("age_from", "")
        data[f"form-{i}-age_to"] = row.get("age_to", "")
        data[f"form-{i}-practice_runs"] = row.get("practice_runs", "1")
        data[f"form-{i}-counted_runs"] = row.get("counted_runs", "2")
    data["form-TOTAL_FORMS"] = str(base + len(extra_rows))
    return data


def test_classes_view_saves_formset(client):
    competition = make_active_competition()
    data = classes_post_data(competition, overrides={
        "1": {"is_running": True, "age_from": "6", "age_to": "7"},
    })
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    cc = competition.classes.get(name="1")
    assert cc.is_running is True
    assert (cc.age_from, cc.age_to) == (6, 7)


def test_classes_view_saves_per_class_run_counts(client):
    competition = make_active_competition()
    data = classes_post_data(competition, overrides={
        "1": {"practice_runs": "3", "counted_runs": "4"},
    })
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    cc = competition.classes.get(name="1")
    assert (cc.practice_runs, cc.counted_runs) == (3, 4)


def test_classes_view_renames_class(client):
    competition = make_active_competition()
    data = classes_post_data(competition, overrides={"1": {"name": "Rookies"}})
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    assert competition.classes.filter(name="Rookies").exists()
    assert not competition.classes.filter(name="1").exists()


def test_classes_view_adds_new_class_at_end(client):
    competition = make_active_competition()
    before = competition.classes.count()
    data = classes_post_data(competition, extra_rows=[{"name": "Juniors", "is_running": True}])
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    assert competition.classes.count() == before + 1
    juniors = competition.classes.get(name="Juniors")
    assert juniors.is_running is True
    # a brand-new class sorts after the existing ones
    assert juniors.position >= before


def test_classes_view_deletes_class(client):
    competition = make_active_competition()
    before = competition.classes.count()
    data = classes_post_data(competition, overrides={"E": {"DELETE": True}})
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    assert competition.classes.count() == before - 1
    assert not competition.classes.filter(name="E").exists()


def test_save_redirects_to_safe_next(client):
    competition = make_active_competition()
    data = classes_post_data(competition)
    data["next"] = reverse("competitions:runorder")
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    assert response.url == reverse("competitions:runorder")


def test_save_ignores_unsafe_next(client):
    competition = make_active_competition()
    data = classes_post_data(competition)
    data["next"] = "https://evil.example.com/steal"
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    assert response.url == reverse("competitions:classes")


def test_sub_pages_show_empty_state_without_active_competition(client):
    make_competition()  # exists, but not active
    for name in ("general", "classes", "runorder"):
        response = client.get(reverse(f"competitions:{name}"))
        assert response.status_code == 200
        assert b"No competition is selected" in response.content


def test_runorder_view_applies_grouping(client):
    competition = make_active_competition()
    competition.classes.filter(name__in=["1", "2", "3"]).update(is_running=True)
    c1 = competition.classes.get(name="1")
    c2 = competition.classes.get(name="2")
    c3 = competition.classes.get(name="3")
    # Run 0 = classes 1 & 3 together; Run 1 = class 2
    run_order = json.dumps([[c1.pk, c3.pk], [c2.pk]])
    response = client.post(reverse("competitions:runorder"), {"run_order": run_order})
    assert response.status_code == 302
    groups = [[cc.name for cc in run] for run in competition.run_groups()]
    assert groups == [["1", "3"], ["2"]]


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
    original.classes.filter(name="1").update(
        is_running=True, age_from=6, age_to=7, practice_runs=3, counted_runs=4, run_position=0
    )

    client.post(reverse("competitions:duplicate", kwargs={"pk": original.pk}))
    copy = Competition.objects.get(name="Original (Copy)")
    assert copy.pk != original.pk
    assert copy.is_active is False
    assert copy.classes.count() == original.classes.count()
    copied = copy.classes.get(name="1")
    assert (copied.is_running, copied.age_from, copied.age_to) == (True, 6, 7)
    assert (copied.practice_runs, copied.counted_runs, copied.run_position) == (3, 4, 0)


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
