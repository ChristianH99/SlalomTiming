import datetime
import json

import pytest
from django.db import IntegrityError
from django.urls import reverse

from . import startpattern
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


def classes_post_data(competition, overrides=None, extra_rows=None,
                       assignment_method=None, allow_multiple_classes=False):
    """Build POST payload for the Classes page (assignment settings + class formset).
    `overrides` maps a class name to a dict of field values for that row;
    `extra_rows` is a list of dicts for brand-new (unsaved) class rows."""
    overrides = overrides or {}
    extra_rows = extra_rows or []
    classes = list(competition.classes.order_by("position", "name"))
    data = {
        "assignment_method": assignment_method or competition.assignment_method,
        "allow_multiple_classes": "on" if allow_multiple_classes else "",
        "form-INITIAL_FORMS": str(len(classes)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
    }

    def fill(i, name, ov):
        data[f"form-{i}-name"] = ov.get("name", name)
        data[f"form-{i}-is_running"] = "on" if ov.get("is_running") else ""
        data[f"form-{i}-age_from"] = ov.get("age_from", "")
        data[f"form-{i}-age_to"] = ov.get("age_to", "")
        data[f"form-{i}-practice_runs"] = ov.get("practice_runs", "1")
        data[f"form-{i}-counted_runs"] = ov.get("counted_runs", "2")
        data[f"form-{i}-scoring_method"] = ov.get(
            "scoring_method", CompetitionClass.Scoring.AGGREGATE
        )
        data[f"form-{i}-allow_multiple_entries"] = "on" if ov.get("allow_multiple_entries") else ""

    for i, cc in enumerate(classes):
        ov = overrides.get(cc.name, {})
        data[f"form-{i}-id"] = cc.pk
        fill(i, cc.name, ov)
        if ov.get("DELETE"):
            data[f"form-{i}-DELETE"] = "on"
    base = len(classes)
    for j, row in enumerate(extra_rows):
        i = base + j
        data[f"form-{i}-id"] = ""
        fill(i, f"New{j}", row)
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


def test_new_class_scores_on_aggregate_times_by_default():
    competition = make_competition()
    assert competition.classes.get(name="1").scoring_method == (
        CompetitionClass.Scoring.AGGREGATE
    )


def test_classes_view_saves_per_class_scoring_method(client):
    competition = make_active_competition()
    data = classes_post_data(competition, overrides={
        "1": {"scoring_method": CompetitionClass.Scoring.REGULARITY},
    })
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    # Set per class: only the edited one changes.
    assert competition.classes.get(name="1").scoring_method == (
        CompetitionClass.Scoring.REGULARITY
    )
    assert competition.classes.get(name="2").scoring_method == (
        CompetitionClass.Scoring.AGGREGATE
    )


def test_classes_view_rejects_an_unknown_scoring_method(client):
    competition = make_active_competition()
    data = classes_post_data(competition, overrides={"1": {"scoring_method": "bogus"}})
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 200  # redisplayed with errors, nothing saved
    assert competition.classes.get(name="1").scoring_method == (
        CompetitionClass.Scoring.AGGREGATE
    )


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
        is_running=True, age_from=6, age_to=7, practice_runs=3, counted_runs=4, run_position=0,
        scoring_method=CompetitionClass.Scoring.REGULARITY,
    )

    client.post(reverse("competitions:duplicate", kwargs={"pk": original.pk}))
    copy = Competition.objects.get(name="Original (Copy)")
    assert copy.pk != original.pk
    assert copy.is_active is False
    assert copy.classes.count() == original.classes.count()
    copied = copy.classes.get(name="1")
    assert (copied.is_running, copied.age_from, copied.age_to) == (True, 6, 7)
    assert (copied.practice_runs, copied.counted_runs, copied.run_position) == (3, 4, 0)
    assert copied.scoring_method == CompetitionClass.Scoring.REGULARITY


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


# ----- competition type settings -----

def settings_post(**overrides):
    data = {
        "name": "Motorcycle",
        "penalties_enabled": "on",
        "pylon_penalty": "5",
        "task_penalty": "10",
        "stop_line_penalty": "3",
        "max_penalty_per_task": "20",
        "tie_break": CompetitionType.TieBreak.MANUAL,
        "timing_precision": CompetitionType.Precision.THOUSANDTHS,
        "requires_vehicle": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def test_type_settings_defaults():
    ctype = CompetitionType.objects.create(name="Fresh")
    assert ctype.penalties_enabled is True
    assert ctype.tie_break == CompetitionType.TieBreak.FASTEST_RUN
    assert ctype.timing_precision == CompetitionType.Precision.HUNDREDTHS
    # Defaults mirror what the participant form already collects today.
    assert (ctype.requires_address, ctype.requires_club) == (True, True)
    assert (ctype.requires_email, ctype.requires_phone) == (True, True)
    assert (ctype.requires_co_driver, ctype.requires_vehicle) == (False, False)
    assert ctype.requires_license is True


def test_save_type_settings_via_view(client):
    ctype = CompetitionType.objects.create(name="Motorcycle")
    response = client.post(
        reverse("competitions:type-settings", kwargs={"pk": ctype.pk}), settings_post()
    )
    assert response.status_code == 302
    ctype.refresh_from_db()
    assert ctype.pylon_penalty == 5
    assert ctype.max_penalty_per_task == 20
    assert ctype.tie_break == CompetitionType.TieBreak.MANUAL
    assert ctype.timing_precision == CompetitionType.Precision.THOUSANDTHS
    assert ctype.requires_vehicle is True
    assert ctype.requires_club is False  # unchecked box → off


def test_penalty_amounts_are_required_when_penalties_are_on(client):
    ctype = CompetitionType.objects.create(name="Motorcycle")
    response = client.post(
        reverse("competitions:type-settings", kwargs={"pk": ctype.pk}),
        settings_post(task_penalty=""),
    )
    assert response.status_code == 200
    assert "task_penalty" in response.context["form"].errors


def test_penalty_amounts_are_cleared_when_penalties_are_off(client):
    ctype = CompetitionType.objects.create(name="Motorcycle", pylon_penalty=5)
    response = client.post(
        reverse("competitions:type-settings", kwargs={"pk": ctype.pk}),
        settings_post(penalties_enabled=None, task_penalty="", stop_line_penalty="",
                      max_penalty_per_task=""),
    )
    assert response.status_code == 302
    ctype.refresh_from_db()
    assert ctype.penalties_enabled is False
    assert ctype.pylon_penalty is None


def test_fractional_penalty_amounts_are_rejected(client):
    ctype = CompetitionType.objects.create(name="Motorcycle")
    response = client.post(
        reverse("competitions:type-settings", kwargs={"pk": ctype.pk}),
        settings_post(pylon_penalty="5.5"),
    )
    assert response.status_code == 200
    assert "pylon_penalty" in response.context["form"].errors
    ctype.refresh_from_db()
    assert ctype.pylon_penalty is None


def test_negative_penalty_amounts_are_rejected(client):
    ctype = CompetitionType.objects.create(name="Motorcycle")
    response = client.post(
        reverse("competitions:type-settings", kwargs={"pk": ctype.pk}),
        settings_post(pylon_penalty="-5"),
    )
    assert response.status_code == 200
    assert "pylon_penalty" in response.context["form"].errors
    ctype.refresh_from_db()
    assert ctype.pylon_penalty is None


def test_format_time_follows_device_precision():
    ctype = CompetitionType.objects.create(
        name="Tenths", timing_precision=CompetitionType.Precision.TENTHS
    )
    assert ctype.format_time(12.345) == "12.3"
    ctype.timing_precision = CompetitionType.Precision.THOUSANDTHS
    assert ctype.format_time(12.345) == "12.345"


def test_participant_field_requirements_follow_the_settings():
    ctype = CompetitionType.objects.create(
        name="Rally", requires_co_driver=True, requires_vehicle=True,
        requires_address=False, requires_phone=False,
    )
    requirements = ctype.participant_field_requirements()
    # Collected and mandatory, collected and optional, and not collected at all.
    assert requirements["vehicle"] is True
    assert requirements["club"] is True
    assert requirements["co_driver_first_name"] is False
    assert "address_street" not in requirements
    assert "phone_number" not in requirements


# ----- start pattern -----

def make_starters(count, practice_runs=1, counted_runs=2, class_name="1"):
    return [
        startpattern.Starter(
            key=(bib,), bib=bib, name=f"Rider {bib}", class_name=class_name,
            practice_runs=practice_runs, counted_runs=counted_runs,
        )
        for bib in range(1, count + 1)
    ]


def slot_labels(slots):
    return [slot.label() for slot in slots]


def test_expand_plays_chips_for_the_whole_window_before_the_next_chip():
    # The worked example: bibs 1-5, two at a time, practice then counted run 1,
    # then a second block giving everyone their second counted run.
    blocks = [
        startpattern.Block(window=2, chips=("practice", "counted")),
        startpattern.Block(window=None, chips=("counted",)),
    ]
    assert slot_labels(startpattern.expand(blocks, make_starters(5))) == [
        "#1 P1", "#2 P1", "#1 C1", "#2 C1",   # first window
        "#3 P1", "#4 P1", "#3 C1", "#4 C1",   # second window
        "#5 P1", "#5 C1",                     # short final window
        "#1 C2", "#2 C2", "#3 C2", "#4 C2", "#5 C2",  # block 2, everyone at once
    ]


def test_expand_numbers_each_starters_runs_in_chip_order():
    blocks = [startpattern.Block(window=None, chips=("counted", "counted"))]
    slots = startpattern.expand(blocks, make_starters(2, counted_runs=2))
    assert slot_labels(slots) == ["#1 C1", "#2 C1", "#1 C2", "#2 C2"]


def test_expand_skips_starters_who_have_no_run_of_that_type_left():
    # One pattern, two classes: the pattern offers two counted runs but this
    # starter's class only grants one, so the second chip passes them over.
    blocks = [startpattern.Block(window=None, chips=("counted", "counted"))]
    starters = make_starters(1, counted_runs=1) + [
        startpattern.Starter(key=(2,), bib=2, name="Rider 2", class_name="2",
                             practice_runs=0, counted_runs=2)
    ]
    assert slot_labels(startpattern.expand(blocks, starters)) == ["#1 C1", "#2 C1", "#2 C2"]


def test_expand_skips_a_run_type_a_class_does_not_grant():
    blocks = [startpattern.Block(window=None, chips=("practice", "counted"))]
    slots = startpattern.expand(blocks, make_starters(2, practice_runs=0, counted_runs=1))
    assert slot_labels(slots) == ["#1 C1", "#2 C1"]


def test_expand_of_empty_pattern_or_empty_field_is_empty():
    assert startpattern.expand([], make_starters(3)) == []
    assert startpattern.expand([startpattern.Block(2, ("counted",))], []) == []


def test_passes_groups_slots_per_block_and_window():
    blocks = [startpattern.Block(window=2, chips=("counted",))]
    grouped = startpattern.passes(blocks, make_starters(5, counted_runs=1))
    assert [[slot_labels(p) for p in block] for block in grouped] == [
        [["#1 C1", "#2 C1"], ["#3 C1", "#4 C1"], ["#5 C1"]]
    ]


def test_shortfalls_reports_runs_the_pattern_never_plays():
    blocks = [startpattern.Block(window=None, chips=("practice",))]
    starters = make_starters(2, practice_runs=1, counted_runs=2)
    assert [(s.bib, owed) for s, owed in startpattern.shortfalls(blocks, starters)] == [
        (1, {"counted": 2}), (2, {"counted": 2}),
    ]


def test_shortfalls_is_empty_when_the_pattern_covers_every_run():
    blocks = [
        startpattern.Block(window=2, chips=("practice", "counted")),
        startpattern.Block(window=None, chips=("counted",)),
    ]
    assert startpattern.shortfalls(blocks, make_starters(5)) == []


def test_parse_drops_malformed_blocks_and_unknown_run_types():
    parsed = startpattern.parse([
        {"window": 2, "chips": ["practice", "bogus", "counted"]},
        {"window": 3, "chips": []},        # no runs -> schedules nothing
        {"window": 1, "chips": "counted"},  # chips must be a list
        "not a block",
        {"chips": ["counted"]},            # missing window -> all at once
    ])
    assert parsed == [
        startpattern.Block(window=2, chips=("practice", "counted")),
        startpattern.Block(window=None, chips=("counted",)),
    ]


def test_parse_of_junk_is_an_empty_pattern():
    assert startpattern.parse(None) == []
    assert startpattern.parse({"window": 1}) == []


@pytest.mark.parametrize("value,expected", [
    (0, None), (-3, None), ("2", 2), ("x", None), (None, None),
    (startpattern.MAX_WINDOW + 5, startpattern.MAX_WINDOW),
])
def test_parse_window_normalises_to_a_sane_size(value, expected):
    assert startpattern.parse([{"window": value, "chips": ["counted"]}])[0].window == expected


def test_serialize_round_trips_through_parse():
    blocks = [
        startpattern.Block(window=2, chips=("practice", "counted")),
        startpattern.Block(window=None, chips=("counted",)),
    ]
    assert startpattern.parse(startpattern.serialize(blocks)) == blocks


def test_runorder_view_sends_class_run_counts_for_the_dummy_preview(client):
    # The preview builds dummy starters from these, and there are no real
    # starters to fall back on before anyone is registered.
    competition = make_active_competition()
    competition.classes.filter(name="1").update(
        is_running=True, run_position=0, practice_runs=3, counted_runs=4
    )
    response = client.get(reverse("competitions:runorder"))
    run_groups_data = response.context["run_groups_data"]
    assert run_groups_data[0][0]["practice"] == 3
    assert run_groups_data[0][0]["counted"] == 4
    assert response.context["max_dummy"] == startpattern.MAX_DUMMY_STARTERS


def test_runorder_view_saves_start_pattern(client):
    competition = make_active_competition()
    posted = json.dumps([
        {"window": 2, "chips": ["practice", "counted"]},
        {"window": None, "chips": ["counted"]},
    ])
    response = client.post(
        reverse("competitions:runorder"), {"run_order": "[]", "start_pattern": posted}
    )
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.start_pattern_blocks() == [
        startpattern.Block(window=2, chips=("practice", "counted")),
        startpattern.Block(window=None, chips=("counted",)),
    ]


def test_runorder_view_stores_only_well_formed_blocks(client):
    competition = make_active_competition()
    client.post(reverse("competitions:runorder"), {
        "run_order": "[]",
        "start_pattern": json.dumps([{"window": 0, "chips": ["counted", "junk"]}, {"x": 1}]),
    })
    competition.refresh_from_db()
    assert competition.start_pattern == [{"window": None, "chips": ["counted"]}]


def test_runorder_view_survives_an_unparseable_start_pattern(client):
    competition = make_active_competition()
    response = client.post(
        reverse("competitions:runorder"), {"run_order": "[]", "start_pattern": "{not json"}
    )
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.start_pattern == []


def make_entered_participant(competition, bib, competition_class, first_name="Rider"):
    from apps.participants.models import ClassAssignment, EventEntry, Participant

    participant = Participant.objects.create(
        competition_type=competition.competition_type,
        first_name=first_name, last_name=f"No{bib}",
        date_of_birth=datetime.date(2000, 1, 1),
        address_street="Main St 1", address_zip_code="1000", address_city="Town",
        club="Club", license_number=f"L{bib}", email=f"r{bib}@example.com",
    )
    EventEntry.objects.create(participant=participant, competition=competition, bib_number=bib)
    ClassAssignment.objects.create(participant=participant, competition_class=competition_class)
    return participant


def test_starters_by_run_merges_a_runs_classes_in_bib_order():
    competition = make_active_competition()
    competition.classes.filter(name__in=["1", "2"]).update(is_running=True)
    c1 = competition.classes.get(name="1")
    c2 = competition.classes.get(name="2")
    # Both classes share run 0, so their participants interleave by bib.
    competition.classes.filter(name__in=["1", "2"]).update(run_position=0)
    make_entered_participant(competition, 1, c1)
    make_entered_participant(competition, 2, c2)
    make_entered_participant(competition, 3, c1)

    runs = competition.starters_by_run()
    assert len(runs) == 1
    run, starters = runs[0]
    assert [(s.bib, s.class_name) for s in starters] == [(1, "1"), (2, "2"), (3, "1")]


def test_starters_carry_their_own_classs_run_counts():
    competition = make_active_competition()
    competition.classes.filter(name="1").update(
        is_running=True, run_position=0, practice_runs=1, counted_runs=2
    )
    c1 = competition.classes.get(name="1")
    make_entered_participant(competition, 1, c1)
    _, starters = competition.starters_by_run()[0]
    assert (starters[0].practice_runs, starters[0].counted_runs) == (1, 2)


def test_a_participant_entered_twice_in_a_class_is_two_starters():
    competition = make_active_competition()
    competition.classes.filter(name="1").update(
        is_running=True, run_position=0, allow_multiple_entries=True
    )
    c1 = competition.classes.get(name="1")
    participant = make_entered_participant(competition, 1, c1)
    from apps.participants.models import ClassAssignment

    ClassAssignment.objects.create(participant=participant, competition_class=c1)

    _, starters = competition.starters_by_run()[0]
    assert len(starters) == 2
    # Distinct keys, so the pattern schedules each entry its own runs.
    assert starters[0].key != starters[1].key


def test_start_lists_plays_the_pattern_over_each_runs_participants():
    competition = make_active_competition()
    competition.classes.filter(name="1").update(
        is_running=True, run_position=0, practice_runs=1, counted_runs=1
    )
    c1 = competition.classes.get(name="1")
    make_entered_participant(competition, 1, c1)
    make_entered_participant(competition, 2, c1)
    competition.start_pattern = [{"window": None, "chips": ["practice", "counted"]}]
    competition.save(update_fields=["start_pattern"])

    (run, slots), = competition.start_lists()
    assert slot_labels(slots) == ["#1 P1", "#2 P1", "#1 C1", "#2 C1"]


def test_participants_without_a_bib_are_not_starters():
    from apps.participants.models import Participant

    competition = make_active_competition()
    competition.classes.filter(name="1").update(is_running=True, run_position=0)
    c1 = competition.classes.get(name="1")
    make_entered_participant(competition, 1, c1)
    unentered = Participant.objects.create(
        competition_type=competition.competition_type,
        first_name="No", last_name="Bib", date_of_birth=datetime.date(2000, 1, 1),
        address_street="S", address_zip_code="1", address_city="T",
        club="C", license_number="L9", email="n@example.com",
    )
    from apps.participants.models import ClassAssignment

    ClassAssignment.objects.create(participant=unentered, competition_class=c1)

    _, starters = competition.starters_by_run()[0]
    assert [s.bib for s in starters] == [1]


def test_duplicate_competition_copies_the_start_pattern(client):
    original = make_competition(name="Original")
    original.start_pattern = [{"window": 2, "chips": ["practice", "counted"]}]
    original.save(update_fields=["start_pattern"])
    client.post(reverse("competitions:duplicate", kwargs={"pk": original.pk}))
    copy = Competition.objects.get(name="Original (Copy)")
    assert copy.start_pattern == original.start_pattern


# ----- assignment methods -----

def test_get_assignment_method_falls_back_to_default():
    from .assignment import get_assignment_method
    assert get_assignment_method("age").key == "age"
    assert get_assignment_method("nonsense").key == "manual"  # default fallback


def test_classes_view_saves_assignment_settings(client):
    competition = make_active_competition()
    data = classes_post_data(
        competition,
        assignment_method="manual",
        allow_multiple_classes=True,
        overrides={"1": {"is_running": True, "allow_multiple_entries": True}},
    )
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.assignment_method == "manual"
    assert competition.allow_multiple_classes is True
    assert competition.classes.get(name="1").allow_multiple_entries is True


def test_classes_view_age_method_forces_multiple_off(client):
    competition = make_active_competition()
    data = classes_post_data(
        competition, assignment_method="age", allow_multiple_classes=True
    )
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.assignment_method == "age"
    # age-based can't support multiple distinct classes → forced off
    assert competition.allow_multiple_classes is False


# ----- changing a competition's type drops registrations that no longer fit -----

def test_changing_competition_type_clears_foreign_registrations(client):
    from apps.participants.models import EventEntry, Participant
    type_a = CompetitionType.objects.create(name="Kart")
    type_b = CompetitionType.objects.create(name="Moto")
    comp = Competition.objects.create(
        competition_type=type_a, name="C", date=datetime.date(2026, 5, 1), is_active=True
    )
    p = Participant.objects.create(
        competition_type=type_a, first_name="A", last_name="A",
        date_of_birth=datetime.date(2010, 1, 1), license_number="1",
    )
    EventEntry.objects.create(participant=p, competition=comp, bib_number=1)

    resp = client.post(reverse("competitions:general"), {
        "competition_type": type_b.pk, "name": comp.name, "date": "2026-05-01",
    })
    assert resp.status_code == 302
    comp.refresh_from_db()
    assert comp.competition_type == type_b
    assert not EventEntry.objects.filter(competition=comp).exists()  # foreign entry cleared


def test_reverting_type_keeps_now_matching_registrations(client):
    from apps.participants.models import EventEntry, Participant
    type_a = CompetitionType.objects.create(name="Kart")
    type_b = CompetitionType.objects.create(name="Moto")
    # Competition currently type B but holding a type-A entry (a prior bad switch).
    comp = Competition.objects.create(
        competition_type=type_b, name="C", date=datetime.date(2026, 5, 1), is_active=True
    )
    p = Participant.objects.create(
        competition_type=type_a, first_name="A", last_name="A",
        date_of_birth=datetime.date(2010, 1, 1), license_number="1",
    )
    EventEntry.objects.create(participant=p, competition=comp, bib_number=1)

    client.post(reverse("competitions:general"), {
        "competition_type": type_a.pk, "name": comp.name, "date": "2026-05-01",
    })
    comp.refresh_from_db()
    assert comp.competition_type == type_a
    assert EventEntry.objects.filter(competition=comp).exists()  # now matches → kept


# ----- task specs -----

from . import taskspec  # noqa: E402
from .models import MarshalPost  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("", []),
    ("   ", []),
    ("5", [5]),
    ("1, 5, 9", [1, 5, 9]),
    ("11-15", [11, 12, 13, 14, 15]),
    ("1, 5, 9, 11-15, 20, 18, 25-27", [1, 5, 9, 11, 12, 13, 14, 15, 18, 20, 25, 26, 27]),
    ("3-1", [1, 2, 3]),          # reversed range is accepted
    ("5, 5, 3-5", [3, 4, 5]),    # duplicates collapse
])
def test_taskspec_parse(text, expected):
    assert taskspec.parse(text) == expected


@pytest.mark.parametrize("text", ["0", "a", "1-", "1--3", "1,,x", "-4"])
def test_taskspec_parse_rejects_malformed(text):
    with pytest.raises(taskspec.TaskSpecError):
        taskspec.parse(text)


def test_taskspec_format_and_summary():
    assert taskspec.format_ranges([1, 2, 3, 5, 9, 10, 11]) == "1-3, 5, 9-11"
    assert taskspec.summary([]) == "No tasks assigned yet"
    assert taskspec.summary(range(1, 36)) == "Tasks 1-35 assigned"
    assert taskspec.summary([1, 2, 4]) == "Tasks 1-2, 4 assigned"


# ----- penalties setup -----

def test_penalties_view_saves_posts_and_stop_line(client):
    competition = make_active_competition()
    response = client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on",
        "post_count": "2",
        "post-1-tasks": "1-10",
        "post-2-tasks": "11, 12, 15-18",
        "stop_line_post": "2",
    })
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.penalties_by_marshal_posts is True
    posts = list(competition.marshal_posts.all())
    assert [p.number for p in posts] == [1, 2]
    assert posts[0].tasks == "1-10"
    assert [p.handles_stop_line for p in posts] == [False, True]
    assert competition.assigned_task_numbers()[:3] == [1, 2, 3]


def test_penalties_view_combines_tasks_into_summary(client):
    competition = make_active_competition()
    client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on", "post_count": "2",
        "post-1-tasks": "1-20", "post-2-tasks": "21-35",
    })
    response = client.get(reverse("competitions:penalties"))
    assert response.context["tasks_summary"] == "Tasks 1-35 assigned"


def test_penalties_view_rejects_malformed_tasks(client):
    competition = make_active_competition()
    response = client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on", "post_count": "1", "post-1-tasks": "1, oops",
    })
    assert response.status_code == 200          # re-rendered with the error
    assert competition.marshal_posts.count() == 0


def test_penalties_view_rejects_a_task_on_two_posts(client):
    competition = make_active_competition()
    response = client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on", "post_count": "2",
        "post-1-tasks": "1-10", "post-2-tasks": "8-15",   # 8, 9, 10 overlap
    })
    assert response.status_code == 200          # re-rendered, not saved
    assert competition.marshal_posts.count() == 0
    rows = response.context["rows"]
    assert "another post" in rows[0]["error"]
    assert "another post" in rows[1]["error"]


def test_penalties_view_off_removes_posts(client):
    competition = make_active_competition()
    MarshalPost.objects.create(competition=competition, number=1, tasks="1-5")
    response = client.post(reverse("competitions:penalties"), {"post_count": "1"})
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.penalties_by_marshal_posts is False
    assert competition.marshal_posts.count() == 0


def test_penalties_view_shrinking_count_drops_extra_posts(client):
    competition = make_active_competition()
    MarshalPost.objects.create(competition=competition, number=1, tasks="1-5")
    MarshalPost.objects.create(competition=competition, number=2, tasks="6-10",
                               handles_stop_line=True)
    client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on", "post_count": "1", "post-1-tasks": "1-8",
    })
    competition.refresh_from_db()
    assert [p.number for p in competition.marshal_posts.all()] == [1]
    assert competition.marshal_posts.get(number=1).tasks == "1-8"


def _post_with_a_recorded_penalty(competition, number=1):
    """A marshal post that has already judged a run — deleting it destroys work
    nothing else in the app can bring back."""
    from apps.timing.models import MarshalPenalty, TimedRun

    competition.penalties_by_marshal_posts = True
    competition.save(update_fields=["penalties_by_marshal_posts"])
    post = MarshalPost.objects.create(competition=competition, number=number, tasks="1-5")
    run = TimedRun.objects.create(competition=competition)
    MarshalPenalty.objects.create(timed_run=run, marshal_post=post, pylon_count=2)
    return post


def test_turning_penalties_off_asks_before_destroying_recorded_ones(client):
    from apps.timing.models import MarshalPenalty

    competition = make_active_competition()
    _post_with_a_recorded_penalty(competition)
    response = client.post(reverse("competitions:penalties"), {"post_count": "1"})
    assert response.status_code == 200                  # re-rendered, not saved
    assert response.context["confirm_loss"] == 1
    competition.refresh_from_db()
    assert competition.penalties_by_marshal_posts is True
    assert competition.marshal_posts.count() == 1
    assert MarshalPenalty.objects.count() == 1


def test_turning_penalties_off_goes_through_once_confirmed(client):
    from apps.timing.models import MarshalPenalty

    competition = make_active_competition()
    _post_with_a_recorded_penalty(competition)
    response = client.post(reverse("competitions:penalties"), {
        "post_count": "1", "confirm_penalty_loss": "1",
    })
    assert response.status_code == 302
    competition.refresh_from_db()
    assert competition.penalties_by_marshal_posts is False
    assert competition.marshal_posts.count() == 0
    assert MarshalPenalty.objects.count() == 0


def test_shrinking_the_post_count_asks_before_dropping_a_judged_post(client):
    competition = make_active_competition()
    MarshalPost.objects.create(competition=competition, number=1, tasks="1-5")
    _post_with_a_recorded_penalty(competition, number=2)
    response = client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on", "post_count": "1", "post-1-tasks": "1-8",
    })
    assert response.status_code == 200
    assert response.context["confirm_loss"] == 1
    assert competition.marshal_posts.count() == 2       # nothing dropped yet


def test_shrinking_past_an_empty_post_needs_no_confirmation(client):
    """Only recorded penalties are worth stopping for — an unjudged post isn't."""
    competition = make_active_competition()
    MarshalPost.objects.create(competition=competition, number=1, tasks="1-5")
    MarshalPost.objects.create(competition=competition, number=2, tasks="6-10")
    response = client.post(reverse("competitions:penalties"), {
        "penalties_by_marshal_posts": "on", "post_count": "1", "post-1-tasks": "1-8",
    })
    assert response.status_code == 302
    assert [p.number for p in competition.marshal_posts.all()] == [1]


def test_penalties_page_shows_what_each_post_has_recorded(client):
    competition = make_active_competition()
    _post_with_a_recorded_penalty(competition, number=3)
    response = client.get(reverse("competitions:penalties"))
    assert response.context["penalty_counts"] == {"3": 1}


def test_penalties_page_needs_an_active_competition(client):
    response = client.get(reverse("competitions:penalties"))
    assert "competitions/no_active_competition.html" in [t.name for t in response.templates]


# ----- marshal posts operator page -----

def test_marshal_posts_page_prompts_when_not_configured(client):
    make_active_competition()
    response = client.get(reverse("competitions:marshal-posts"))
    assert response.context["has_posts"] is False


def test_marshal_posts_page_serves_config(client):
    competition = make_active_competition()
    ctype = competition.competition_type
    CompetitionType.objects.filter(pk=ctype.pk).update(
        pylon_penalty=2, task_penalty=10, stop_line_penalty=5, max_penalty_per_task=10
    )
    competition.penalties_by_marshal_posts = True
    competition.save(update_fields=["penalties_by_marshal_posts"])
    MarshalPost.objects.create(competition=competition, number=1, tasks="1-3",
                               handles_stop_line=True)
    response = client.get(reverse("competitions:marshal-posts"))
    config = response.context["config"]
    assert config["maxPylons"] == 5          # max_penalty_per_task // pylon_penalty
    assert config["posts"][0]["tasks"] == [1, 2, 3]
    assert config["posts"][0]["stop_line"] is True


def test_duplicate_competition_copies_marshal_posts(client):
    original = make_competition(name="Original")
    original.penalties_by_marshal_posts = True
    original.save(update_fields=["penalties_by_marshal_posts"])
    MarshalPost.objects.create(competition=original, number=1, tasks="1-5",
                               handles_stop_line=True)
    client.post(reverse("competitions:duplicate", kwargs={"pk": original.pk}))
    copy = Competition.objects.get(name="Original (Copy)")
    assert copy.penalties_by_marshal_posts is True
    assert copy.marshal_posts.get(number=1).tasks == "1-5"


# --- DAT-5: the active competition is everybody's ---------------------------
# One global flag decides what every timing screen, results table and marshal
# post is showing. Switching it used to be a single unconfirmed click that other
# people found out about from their own data changing under them.

def _sign_in_someone_else(django_user_model, username="marshal-mia"):
    """A second live session, as a second person's browser would leave."""
    from django.test import Client

    other = django_user_model.objects.create_user(username=username, password="pw")
    Client().force_login(other)
    return other


def test_switching_alone_needs_no_confirmation(client):
    a = make_competition(name="A", type_name="A")
    response = client.post(reverse("competitions:select", kwargs={"pk": a.pk}))
    assert response.status_code == 302
    a.refresh_from_db()
    assert a.is_active is True


def test_switching_asks_first_when_others_are_signed_in(client, django_user_model):
    other = _sign_in_someone_else(django_user_model)
    a = make_competition(name="A", type_name="A")

    response = client.post(reverse("competitions:select", kwargs={"pk": a.pk}))

    assert response.status_code == 200          # the confirmation page, not a redirect
    assert other.get_username() in response.content.decode()
    a.refresh_from_db()
    assert a.is_active is False                 # and nothing moved


def test_switching_goes_through_once_confirmed(client, django_user_model):
    _sign_in_someone_else(django_user_model)
    a = make_competition(name="A", type_name="A")

    response = client.post(reverse("competitions:select", kwargs={"pk": a.pk}),
                           {"confirm_switch": "1"})

    assert response.status_code == 302
    a.refresh_from_db()
    assert a.is_active is True


def test_an_expired_session_is_not_somebody_watching(client, django_user_model, settings):
    """Sessions outlive the people in them; only live ones are a reason to ask."""
    import datetime

    from django.contrib.sessions.models import Session
    from django.utils import timezone

    _sign_in_someone_else(django_user_model)
    Session.objects.all().exclude(session_key=client.session.session_key).update(
        expire_date=timezone.now() - datetime.timedelta(hours=1))
    a = make_competition(name="A", type_name="A")

    assert client.post(reverse("competitions:select", kwargs={"pk": a.pk})).status_code == 302


def test_re_selecting_the_current_event_asks_nobody(client, django_user_model):
    """It changes nothing, so there is nothing to warn about."""
    _sign_in_someone_else(django_user_model)
    a = make_active_competition()
    assert client.post(reverse("competitions:select", kwargs={"pk": a.pk})).status_code == 302


def test_switching_tells_every_open_live_view_which_event_it_now_shows(client, monkeypatch):
    """A bare refresh nudge would make each open view quietly re-render as a
    different event; the name is what lets the page say what happened."""
    from apps.timing import services

    sent = []
    monkeypatch.setattr(services, "_send", sent.append)
    a = make_competition(name="Spring Slalom", type_name="A")

    client.post(reverse("competitions:select", kwargs={"pk": a.pk}))

    assert sent == [{"type": "timing.competition", "name": "Spring Slalom"}]
