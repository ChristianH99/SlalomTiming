import datetime
import json
from html.parser import HTMLParser

import pytest
from django.db import IntegrityError
from django.urls import reverse

from . import archiving, duplication, startpattern, urls
from apps.participants.models import Participant
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
        if row.get("DELETE"):
            data[f"form-{i}-DELETE"] = "on"
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


_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "source", "track", "wbr"}


class _ClassTileFields(HTMLParser):
    """The input names inside each ``data-class-tile`` element, and the ones
    that belong to a class form but sit outside every tile."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tiles = []
        self.outside = set()
        self._depth = 0  # open tags since the current tile's own; 0 = outside

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._depth == 0 and "data-class-tile" in attrs:
            self.tiles.append(set())
            self._depth = 1
        elif self._depth and tag not in _VOID_TAGS:
            self._depth += 1
        name = attrs.get("name")
        if tag in ("input", "select", "textarea") and name:
            (self.tiles[-1] if self._depth else self.outside).add(name)

    def handle_startendtag(self, tag, attrs):
        # An explicitly closed tag brings no end tag of its own to count.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1


def test_a_class_tile_carries_its_whole_form(client):
    """The page operates on a class by operating on its tile — removing one
    ticks the DELETE box inside ``[data-class-tile]`` and hides the element — so
    every field of a class form has to live in that element or the operation
    leaves it behind. The pk field once didn't: it was rendered beside the tile,
    and removing a class (which then dropped the tile) left its id in the POST
    with the rest of the form gone. The formset read that as an existing class
    submitted with an empty name, so the deleted class came back with "This
    field is required" on it (issue #6)."""
    competition = make_active_competition()
    response = client.get(reverse("competitions:classes"))
    assert response.status_code == 200
    parser = _ClassTileFields()
    parser.feed(response.content.decode())

    # One tile per class, plus the empty_form in the "add class" <template>.
    assert len(parser.tiles) == competition.classes.count() + 1
    for names in parser.tiles:
        ids = [n for n in names if n.endswith("-id")]
        assert len(ids) == 1, f"tile does not carry exactly one pk field: {names}"
        prefix = ids[0][: -len("-id")]
        for field in ("name", "DELETE", "is_running", "practice_runs",
                      "counted_runs", "scoring_method"):
            assert f"{prefix}-{field}" in names, f"{field} rendered outside its tile"

    management = {f"form-{k}" for k in
                  ("TOTAL_FORMS", "INITIAL_FORMS", "MIN_NUM_FORMS", "MAX_NUM_FORMS")}
    assert {n for n in parser.outside if n.startswith("form-")} == management


def test_classes_view_drops_a_class_removed_before_it_was_ever_saved(client):
    """"+ Add class", then × before pressing Save. The page keeps the tile and
    ticks its DELETE box rather than taking it out of the DOM, because a formset
    is an index range: a form left out of the POST is a hole, and Django reads
    the absent fields against that form's own defaults (practice_runs=1,
    counted_runs=2), decides it changed, and validates it — so the abandoned
    class came back as an empty tile refusing to save without a name."""
    competition = make_active_competition()
    before = competition.classes.count()
    data = classes_post_data(
        competition,
        extra_rows=[{"name": "", "practice_runs": "", "counted_runs": "",
                     "scoring_method": "", "DELETE": True}],
    )
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302  # not redisplayed with "name is required"
    assert competition.classes.count() == before


def test_classes_view_deletes_the_last_classes_of_a_competition(client):
    """Nothing fixes the class count at the seven that are seeded — an event
    running five deletes the other two and keeps them deleted (issue #6)."""
    competition = make_active_competition()
    keep = {"1", "2", "3", "4", "5"}
    doomed = [cc.name for cc in competition.classes.all() if cc.name not in keep]
    assert doomed, "the seeded set is expected to be larger than the kept five"
    data = classes_post_data(
        competition, overrides={name: {"DELETE": True} for name in doomed}
    )
    response = client.post(reverse("competitions:classes"), data)
    assert response.status_code == 302
    assert set(competition.classes.values_list("name", flat=True)) == keep


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

    # Dropping registrations is destructive, so it is refused until confirmed:
    # the first post re-renders the page with the dialog and changes nothing.
    resp = client.post(reverse("competitions:general"), {
        "competition_type": type_b.pk, "name": comp.name, "date": "2026-05-01",
    })
    assert resp.status_code == 200
    assert resp.context["confirm_type_change"] == 1
    comp.refresh_from_db()
    assert comp.competition_type == type_a
    assert EventEntry.objects.filter(competition=comp).exists()

    resp = client.post(reverse("competitions:general"), {
        "competition_type": type_b.pk, "name": comp.name, "date": "2026-05-01",
        "confirm_type_change": "1",
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


# --- The active competition is everybody's ---------------------------
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


# --- ...and deleting it is the same act, plus the data --------------
# Deleting the running event is strictly more destructive than switching away
# from it and used to be the quieter of the two: no other-people warning, no
# confirmation beyond the page itself, and no word to the screens that were
# following it — which simply emptied, mid-event, saying nothing.

def test_deleting_the_current_event_names_who_else_is_signed_in(client, django_user_model):
    other = _sign_in_someone_else(django_user_model)
    competition = make_active_competition()

    page = client.get(reverse("competitions:delete", kwargs={"pk": competition.pk}))

    body = page.content.decode()
    assert other.get_username() in body
    assert "current event" in body


def test_deleting_an_event_nobody_is_on_says_nothing_about_screens(client,
                                                                   django_user_model):
    """A competition that isn't the current one moves no screen at all, so the
    warning would be false — and a warning that is sometimes false is read past."""
    _sign_in_someone_else(django_user_model)
    other_event = make_competition(name="Next month", type_name="A")

    body = client.get(
        reverse("competitions:delete", kwargs={"pk": other_event.pk})).content.decode()

    assert "current event" not in body


def test_the_current_event_is_not_deleted_by_an_unconfirmed_post(client):
    """The confirmation page carries the flag; a POST without it never saw the
    page — a stale tab, a re-submitted form — and the running event is not
    something to take down on one of those."""
    competition = make_active_competition()

    response = client.post(reverse("competitions:delete", kwargs={"pk": competition.pk}))

    assert response.status_code == 200          # the confirmation page, not a redirect
    assert Competition.objects.filter(pk=competition.pk).exists()


def test_deleting_the_current_event_goes_through_once_confirmed(client):
    competition = make_active_competition()

    response = client.post(reverse("competitions:delete", kwargs={"pk": competition.pk}),
                           {"confirm_active": "1"})

    assert response.status_code == 302
    assert not Competition.objects.filter(pk=competition.pk).exists()


def test_deleting_a_competition_that_is_not_current_needs_no_extra_flag(client):
    """Only the running event is everybody's; deleting next month's is one click."""
    make_active_competition()
    other_event = make_competition(name="Next month", type_name="A")

    response = client.post(reverse("competitions:delete", kwargs={"pk": other_event.pk}))

    assert response.status_code == 302
    assert not Competition.objects.filter(pk=other_event.pk).exists()


def test_deleting_the_current_event_tells_every_open_live_view_it_is_gone(client,
                                                                         monkeypatch):
    """Not the switch nudge: nobody is being shown another event, they are being
    shown none, and a page cannot word that as a change of event."""
    from apps.timing import services

    sent = []
    monkeypatch.setattr(services, "_send", sent.append)
    competition = make_active_competition(name="Spring Slalom")

    client.post(reverse("competitions:delete", kwargs={"pk": competition.pk}),
                {"confirm_active": "1"})

    assert sent == [{"type": "timing.competition", "name": "Spring Slalom",
                     "deleted": True}]


def test_deleting_a_competition_that_is_not_current_nudges_nobody(client, monkeypatch):
    from apps.timing import services

    sent = []
    monkeypatch.setattr(services, "_send", sent.append)
    make_active_competition()
    other_event = make_competition(name="Next month", type_name="A")

    client.post(reverse("competitions:delete", kwargs={"pk": other_event.pk}))

    assert sent == []


# ----- A competition that can actually be timed -----

def test_a_new_competition_has_no_start_pattern():
    """A pattern is what *Auto* timing needs, and Auto timing is a choice: plenty
    of events are run on the Manual view with competitors turning up at the line
    in any order. Nothing else reads the pattern — the dashboard and the results
    derive from the entries and their classes — so a competition starts without
    one and the Auto page asks for it."""
    competition = make_competition()
    assert competition.start_pattern == []
    assert competition.start_pattern_blocks() == []


def test_an_explicit_pattern_is_never_overwritten():
    ctype = CompetitionType.objects.create(name="Go-Cart")
    pattern = [{"window": 2, "chips": ["counted"]}]
    competition = Competition.objects.create(
        competition_type=ctype, name="R", date=datetime.date(2026, 5, 1),
        start_pattern=pattern,
    )
    assert competition.start_pattern == pattern


# ----- Class configurations that can never rank -----

def test_a_class_with_no_counted_runs_is_flagged():
    competition = make_competition()
    cclass = competition.classes.create(name="X", is_running=True, counted_runs=0)
    assert "counted runs" in str(cclass.scoring_warning())


def test_regularity_over_one_run_is_flagged():
    competition = make_competition()
    cclass = competition.classes.create(
        name="X", is_running=True, counted_runs=1,
        scoring_method=CompetitionClass.Scoring.REGULARITY,
    )
    assert "regularity" in str(cclass.scoring_warning()).lower()
    cclass.counted_runs = 2
    assert cclass.scoring_warning() == ""


def test_only_a_running_class_is_flagged():
    competition = make_competition()
    cclass = competition.classes.create(name="X", is_running=False, counted_runs=0)
    assert cclass.scoring_warning() == ""


# ----- How a class is titled -----

def test_the_word_is_always_added():
    """The prefix used to be conditional — a name already opening with the
    word (in any shipped language) was shown alone — so the heading depended on
    how somebody had typed a name. The organiser owns the name, the app owns the
    word; a name that repeats it is answered by name_hint(), on the page where
    it can be changed."""
    competition = make_competition()
    plain = competition.classes.create(name="7", is_running=True)
    already = competition.classes.create(name="Klasse 7", is_running=True)
    assert plain.display_name() == "Class 7"
    assert already.display_name() == "Class Klasse 7"


def test_a_name_that_repeats_the_word_is_pointed_out_where_it_can_be_fixed():
    competition = make_competition()
    plain = competition.classes.create(name="7", is_running=True)
    assert plain.name_hint() == ""
    for name, suggested in (("Klasse 7", "7"), ("Class 8", "8"),
                            ("klasse-9", "9"), ("Klasse: Bobbycar", "Bobbycar")):
        cclass = competition.classes.create(name=name, is_running=True)
        hint = str(cclass.name_hint())
        assert f"“{suggested}”" in hint, (name, hint)
        # It shows what the class will actually read as, so the advice is
        # checkable rather than abstract.
        assert cclass.display_name() in hint


def test_a_class_named_only_the_word_keeps_its_name_as_the_suggestion():
    """Stripping the word off "Klasse" leaves nothing to suggest, and a hint
    telling somebody to name a class "" is worse than none."""
    competition = make_competition()
    cclass = competition.classes.create(name="Klasse", is_running=True)
    assert "“Klasse”" in str(cclass.name_hint())


# ----- The German page says the same thing the English one does -----

def test_run_order_palette_and_chips_use_the_same_words(client, settings):
    # The palette is rendered by Django from RUN_TYPE_LABELS and the chips dropped
    # from it by the page's own JS — untranslated labels meant dragging "Counted"
    # and getting "Wertung".
    settings.LANGUAGE_CODE = "de"
    competition = make_competition()
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    competition.classes.filter(name="1").update(is_running=True, run_position=0)
    body = client.get(reverse("competitions:runorder")).content.decode()
    palette = body.split('class="pattern-palette"', 1)[1].split("</div>", 1)[0]
    assert "Training" in palette and "Wertung" in palette
    assert "Practice" not in palette and "Counted" not in palette


def test_the_german_heading_uses_the_german_word(settings):
    settings.LANGUAGE_CODE = "de"
    competition = make_competition()
    plain = competition.classes.create(name="7", is_running=True)
    assert plain.display_name() == "Klasse 7"


def test_the_sidebar_names_a_class_as_a_class(client, settings):
    """The Results sub-list read "1", "2", "Bobbycar Mini" while every one
    of those links opens a page titled "Result Class 7"."""
    settings.LANGUAGE_CODE = "en"
    competition = make_competition()
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    competition.classes.filter(name="1").update(is_running=True, run_position=0)
    body = client.get(reverse("results:index")).content.decode()
    sidebar = body[body.index('<nav class="shell-nav">'):body.index("</nav>")]
    assert ">Class 1</a>" in sidebar


# --- exactly one active competition ----------------------------------

def test_two_active_competitions_are_refused_by_the_database():
    """`get_current()` is `filter(is_active=True).first()`, so with two active
    rows the app silently serves whichever sorts first and nobody can see the
    conflict. Every code path clears the others first — but that is a claim about
    code, and this is a claim about the data."""
    from django.db import IntegrityError, transaction

    ctype = CompetitionType.objects.create(name="OnlyOne")
    Competition.objects.create(competition_type=ctype, name="A",
                               date=datetime.date(2026, 5, 1), is_active=True)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Competition.objects.create(competition_type=ctype, name="B",
                                       date=datetime.date(2026, 6, 1), is_active=True)


def test_any_number_of_inactive_competitions_is_fine():
    ctype = CompetitionType.objects.create(name="Archive")
    for i in range(4):
        Competition.objects.create(competition_type=ctype, name=f"Past {i}",
                                   date=datetime.date(2025, i + 1, 1))
    assert Competition.objects.filter(is_active=False).count() == 4


def test_switching_the_active_competition_still_works(client):
    """The constraint must not get in the way of the normal switch, which clears
    the old flag and sets the new one."""
    ctype = CompetitionType.objects.create(name="Switcher")
    first = Competition.objects.create(competition_type=ctype, name="A",
                                       date=datetime.date(2026, 5, 1), is_active=True)
    second = Competition.objects.create(competition_type=ctype, name="B",
                                        date=datetime.date(2026, 6, 1))
    client.post(reverse("competitions:select", args=[second.pk]), {"confirm_switch": "1"})
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.is_active, second.is_active) == (False, True)


# --- what an age range covers, and what "age" means --------

class TestAgeRanges:
    """Under age-based assignment these decide which class a competitor lands in,
    and both failure modes are silent: an overlap picks whichever class sorts
    first, a gap leaves them in no class and absent from every start list."""

    def _competition(self):
        ctype = CompetitionType.objects.create(name="Aged")
        competition = Competition.objects.create(
            competition_type=ctype, name="R", date=datetime.date(2026, 5, 1),
            assignment_method="age",
        )
        competition.classes.all().delete()
        return competition

    def _cls(self, competition, name, age_from, age_to, position):
        return CompetitionClass.objects.create(
            competition=competition, name=name, is_running=True,
            age_from=age_from, age_to=age_to, position=position)

    def test_an_overlap_is_named_with_what_it_costs(self):
        competition = self._competition()
        self._cls(competition, "Mini", 6, 10, 0)
        self._cls(competition, "Maxi", 9, 14, 1)
        problems = competition.age_range_problems()
        assert any("9" in p and "Mini" in p and "Maxi" in p for p in problems), problems

    def test_a_gap_is_named(self):
        competition = self._competition()
        self._cls(competition, "Mini", 6, 10, 0)
        self._cls(competition, "Maxi", 14, 18, 1)
        assert any("11" in p for p in competition.age_range_problems())

    def test_a_backwards_range_is_named(self):
        competition = self._competition()
        self._cls(competition, "Odd", 14, 6, 0)
        assert any("backwards" in p for p in competition.age_range_problems())

    def test_ranges_that_meet_exactly_are_not_a_problem(self):
        competition = self._competition()
        self._cls(competition, "Mini", 6, 10, 0)
        self._cls(competition, "Maxi", 11, 14, 1)
        assert competition.age_range_problems() == []

    def test_manual_assignment_is_never_asked(self):
        """The ranges are display-only under manual assignment."""
        competition = self._competition()
        competition.assignment_method = "manual"
        competition.save(update_fields=["assignment_method"])
        self._cls(competition, "Mini", 6, 10, 0)
        self._cls(competition, "Maxi", 9, 14, 1)
        assert competition.age_range_problems() == []

    def test_age_is_the_competition_year_minus_the_birth_year(self):
        """Not age on the day: slalom classes are written by Jahrgang, so a
        December birthday is in the same class all year as a January one."""
        competition = self._competition()
        mini = self._cls(competition, "Mini", 6, 10, 0)
        assert competition.class_for_birth_year(2026 - 6) == mini
        assert competition.class_for_birth_year(2026 - 10) == mini
        assert competition.class_for_birth_year(2026 - 11) is None


def test_the_database_refuses_a_second_active_competition():
    """`get_current()` is `filter(is_active=True).first()`, so two active
    rows — from the admin, a bad import, an interrupted transaction — meant the
    app silently served one of them and the operator had no way to see the
    conflict. The invariant belongs in the database, not in whichever code path
    happened to set the flag; this asserts it is still there."""
    ctype = CompetitionType.objects.create(name="Motorcycle")
    Competition.objects.create(
        competition_type=ctype, name="A", date=datetime.date(2026, 5, 1), is_active=True,
    )

    with pytest.raises(IntegrityError):
        Competition.objects.create(
            competition_type=ctype, name="B", date=datetime.date(2026, 6, 1),
            is_active=True,
        )


# ----- the event's own date must survive the round trip (issue #1) -----

def test_the_general_page_still_shows_the_events_date_in_german(client, settings):
    """Same bug as the participant birthday, same cause: `<input type="date">`
    reads ISO and nothing else, while Django renders a date through the active
    locale — so on the shipped default language the General page offered the
    operator an empty date, and the suite's English pin hid it."""
    settings.LANGUAGE_CODE = "de"
    make_active_competition()

    page = client.get(reverse("competitions:general")).content.decode()

    assert 'value="2026-05-01"' in page


# ----- archiving: a finished event stops following its type (issue #8) -----
#
# A CompetitionType is shared by every competition of its discipline and its
# settings are read live, so editing a penalty amount to set up next month's
# event re-ranked last month's. Archiving is the answer: the settings are copied
# onto the competition and it is evaluated from the copy for ever after. These
# tests are about the copy; config/archived_tests.py is about the other half —
# that a signed-off event then takes no writes.

def test_archiving_freezes_the_settings_the_event_was_run_under():
    competition = make_competition()
    ctype = competition.competition_type
    ctype.pylon_penalty = 5
    ctype.timing_precision = CompetitionType.Precision.HUNDREDTHS
    ctype.save()

    archiving.archive(competition)
    # …and now somebody sets up next season.
    ctype.pylon_penalty = 10
    ctype.timing_precision = CompetitionType.Precision.THOUSANDTHS
    ctype.save()

    competition.refresh_from_db()
    assert competition.rules.pylon_penalty == 5
    assert competition.rules.timing_precision == CompetitionType.Precision.HUNDREDTHS
    # The discipline it belongs to is a different question and still answers live.
    assert competition.competition_type.pylon_penalty == 10


def test_a_live_competition_still_follows_its_type():
    """The freeze must be the archived event's alone — an app where every
    competition quietly reads a snapshot would be one where editing a type does
    nothing and nobody could say why."""
    competition = make_competition()
    competition.competition_type.pylon_penalty = 7
    competition.competition_type.save()

    assert competition.rules.pylon_penalty == 7
    assert competition.rules.pk == competition.competition_type.pk


def test_there_is_no_way_to_un_archive():
    """One-way on purpose: an event signed off has had its results printed, and
    putting it back on settings that have moved on since is the exact failure
    archiving exists to stop. The way to edit one is to duplicate it."""
    assert not hasattr(archiving, "reopen")
    assert "reopen" not in {p.name for p in urls.urlpatterns}


def test_archiving_twice_keeps_the_first_snapshot():
    """A second click must not re-take the snapshot against a type that has
    moved on since — which is the one way this feature could quietly lose the
    settings it exists to keep."""
    competition = make_competition()
    competition.competition_type.pylon_penalty = 3
    competition.competition_type.save()
    archiving.archive(competition)
    taken_at = competition.archived_at

    competition.competition_type.pylon_penalty = 12
    competition.competition_type.save()
    archiving.archive(competition)

    assert competition.archived_at == taken_at
    assert competition.rules.pylon_penalty == 3


def test_the_snapshot_carries_every_setting_of_the_type():
    """FROZEN_FIELDS is derived from the model, so a field added to
    CompetitionType is frozen the day it is added rather than the day somebody
    remembers to add it here."""
    competition = make_competition()
    archiving.archive(competition)

    stored = set(competition.archived_rules)
    model_fields = {
        f.name for f in CompetitionType._meta.concrete_fields if not f.primary_key
    }
    assert stored == model_fields


def test_a_frozen_snapshot_cannot_be_saved_back():
    """It behaves like the type in every way a reader needs; writing it is the
    one thing it refuses, because a snapshot that can become a row is a snapshot
    somebody will eventually turn into one."""
    competition = make_competition()
    archiving.archive(competition)

    with pytest.raises(RuntimeError):
        competition.rules.save()


def test_the_archive_view_signs_the_event_off(client):
    competition = make_active_competition()

    response = client.post(reverse("competitions:archive", args=[competition.pk]))

    competition.refresh_from_db()
    assert response.status_code == 302
    assert competition.is_archived
    assert competition.archived_rules


def test_archiving_needs_a_post(client):
    competition = make_active_competition()
    assert client.get(reverse("competitions:archive", args=[competition.pk])).status_code == 405
    competition.refresh_from_db()
    assert not competition.is_archived


def test_the_list_offers_archive_and_the_archived_tile_swaps_it_for_its_settings(client):
    competition = make_active_competition()

    page = client.get(reverse("competitions:list")).content.decode()
    assert reverse("competitions:archive", args=[competition.pk]) in page

    archiving.archive(competition)
    page = client.get(reverse("competitions:list")).content.decode()
    # The action it can no longer offer is replaced by what it is, and by the
    # one thing there is left to ask of it.
    assert reverse("competitions:archive", args=[competition.pk]) not in page
    assert reverse("competitions:archived-rules", args=[competition.pk]) in page
    assert "Archived" in page


def test_the_pages_that_manage_competitions_stay_live(client):
    """The lock is about the *active* event, not about the app. A blanket one
    turned the list you archive from — and the duplicate dialog, and the device
    settings — into dead pages the moment it worked."""
    competition = make_active_competition()
    archiving.archive(competition)

    for url in (reverse("competitions:list"),
                reverse("competitions:type-list"),
                reverse("timing:settings"),
                reverse("transfer:export"),
                reverse("competitions:archived-rules", args=[competition.pk])):
        page = client.get(url).content.decode()
        assert "<body data-read-only>" not in page, url


def test_every_page_says_so_once_in_the_topbar(client):
    """Once, in the chrome — not a banner over every page. The state does not
    change while somebody is looking at it, and a block above the results table
    pushed the thing they came to read down the screen."""
    competition = make_active_competition()
    archiving.archive(competition)

    for url in (reverse("timing:manual"), reverse("participants:list"),
                reverse("results:index"), reverse("competitions:general")):
        page = client.get(url).content.decode()
        assert "topbar-archived" in page, url
        # …and the page is one somebody would try to operate, so it is also
        # turned off rather than merely labelled.
        assert "<body data-read-only>" in page, url


def test_the_archived_settings_are_readable_after_the_type_moves_on(client):
    competition = make_active_competition()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 11
    competition.competition_type.save()

    page = client.get(
        reverse("competitions:archived-rules", args=[competition.pk])
    ).content.decode()

    # The frozen value, not the live one — this pop-up is the only screen that
    # can answer what the event was actually run under.
    assert ">5<" in page
    assert "archived-rules" in page


def test_the_setup_pages_refuse_to_save_and_say_why(client):
    competition = make_active_competition()
    archiving.archive(competition)

    response = client.post(
        reverse("competitions:general"),
        {"competition_type": competition.competition_type.pk,
         "name": "Renamed", "date": "2026-06-01"},
        follow=True,
    )

    competition.refresh_from_db()
    assert competition.name != "Renamed"
    assert str(archiving.READ_ONLY) in response.content.decode()


def test_duplicating_an_archived_event_asks_what_the_copy_should_carry(client):
    """A copy is made for two quite different reasons — next season's setup, or
    an editable version of an event that can no longer be changed — and the app
    cannot tell them apart. So it asks, and copies nothing until it is answered."""
    competition = make_active_competition()
    archiving.archive(competition)

    response = client.post(reverse("competitions:duplicate", args=[competition.pk]))

    assert response.status_code == 200
    assert Competition.objects.count() == 1
    assert b"data-dup-settings" in response.content or b"dup-choice" in response.content


def test_a_duplicate_of_an_archived_event_is_a_live_one(client):
    """The copy has to be editable — an archived duplicate would be a competition
    nobody could put starters into, for no reason anyone could see."""
    competition = make_active_competition()
    archiving.archive(competition)

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "setup"})

    copy = Competition.objects.exclude(pk=competition.pk).get()
    assert not copy.is_archived
    assert copy.archived_rules == {}
    assert not copy.archived_starters.exists()


# ----- the competitors are frozen too -----
#
# A Participant belongs to a *discipline*, not to an event, and is edited (and
# deleted) for years afterwards. Every one of those edits used to rewrite the
# printed result of an event that had already happened.

def _entered(competition, bib, first="Ada", last="Lovelace", club="Old Club"):
    from apps.participants.models import ClassAssignment, EventEntry, Participant

    person = Participant.objects.create(
        competition_type=competition.competition_type,
        first_name=first, last_name=last, club=club,
        date_of_birth=datetime.date(2010, 1, 1),
    )
    EventEntry.objects.create(
        participant=person, competition=competition, bib_number=bib)
    cclass = competition.classes.filter(is_running=True).first()
    if cclass is not None:
        ClassAssignment.objects.create(participant=person, competition_class=cclass)
    return person


def _with_a_running_class(**kwargs):
    competition = make_competition(**kwargs)
    competition.classes.filter(name="1").update(is_running=True, run_position=0)
    return competition


def test_editing_a_participant_cannot_change_an_archived_event():
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    archiving.archive(competition)

    person.club = "New Club"
    person.last_name = "Changed"
    person.save()

    competition.refresh_from_db()
    entry = competition.entry_rows()[0]
    assert entry.participant.club == "Old Club"
    assert entry.participant.last_name == "Lovelace"
    assert entry.bib_number == 7


def test_deleting_a_participant_cannot_empty_an_archived_event():
    """The retention case, and the reason this is a copy rather than a pointer:
    a person removed from the system takes their entry with them, and an event
    that read the live tables would lose a competitor out of a printed result."""
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    archiving.archive(competition)

    person.delete()

    competition.refresh_from_db()
    rows = competition.entry_rows()
    assert [e.bib_number for e in rows] == [7]
    assert rows[0].participant.last_name == "Lovelace"


def test_a_class_assignment_changed_later_cannot_re_sort_an_archived_field():
    from apps.participants.models import ClassAssignment

    competition = _with_a_running_class()
    person = _entered(competition, 7)
    cclass = competition.classes.get(name="1")
    archiving.archive(competition)

    ClassAssignment.objects.filter(participant=person).delete()

    competition.refresh_from_db()
    assert [cc.pk for cc in competition.classes_for_participant(person)] == [cclass.pk]
    assert competition.starters_by_class()[cclass.pk]


def test_a_live_event_still_follows_its_participants():
    """The mirror, without which a bug that froze every competition would pass."""
    competition = _with_a_running_class()
    person = _entered(competition, 7)

    person.club = "New Club"
    person.save()

    assert competition.entry_rows()[0].participant.club == "New Club"


# ----- duplicating an archived event, to edit it -----

def test_a_full_duplicate_carries_the_competitors_bibs_and_times():
    from apps.timing.models import TimedRun, TimingSignal

    competition = _with_a_running_class()
    _entered(competition, 7)
    cclass = competition.classes.get(name="1")
    start = TimingSignal.objects.create(
        competition=competition, running_number=1, port=1,
        device_time=datetime.time(10, 0, 0))
    finish = TimingSignal.objects.create(
        competition=competition, running_number=1, port=2,
        device_time=datetime.time(10, 0, 30))
    TimedRun.objects.create(
        competition=competition, start_signal=start, finish_signal=finish,
        bib_number=7, competition_class=cclass, run_type=TimedRun.RunType.COUNTED,
        run_number=1, manual_entry=True, pylon_count=2)
    archiving.archive(competition)

    copy = duplication.copy(competition, with_data=True)

    assert not copy.is_archived
    assert [e.bib_number for e in copy.entry_rows()] == [7]
    runs = TimedRun.objects.filter(competition=copy)
    assert runs.count() == 1
    assert runs.first().pylon_count == 2
    # Its own signals, not the original's — a run points at them through a
    # OneToOne, so sharing them would move half the original event.
    assert TimingSignal.objects.filter(competition=copy).count() == 2
    assert TimingSignal.objects.filter(competition=competition).count() == 2


def test_a_setup_only_duplicate_leaves_the_times_and_bibs_behind():
    from apps.timing.models import TimedRun

    competition = _with_a_running_class()
    _entered(competition, 7)
    archiving.archive(competition)

    copy = duplication.copy(competition, with_data=False)

    assert not copy.entries.exists()
    assert not TimedRun.objects.filter(competition=copy).exists()
    # But the classes, and who was in them, are the point of this copy.
    assert copy.classes.filter(is_running=True).exists()


def test_a_full_duplicate_puts_back_a_competitor_who_has_since_been_deleted():
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    archiving.archive(competition)
    person.delete()

    copy = duplication.copy(competition, with_data=True)

    rows = copy.entry_rows()
    assert [e.bib_number for e in rows] == [7]
    assert rows[0].participant.last_name == "Lovelace"


def test_the_original_is_untouched_by_a_full_duplicate():
    competition = _with_a_running_class()
    _entered(competition, 7)
    archiving.archive(competition)
    before = competition.archived_rules

    duplication.copy(competition, with_data=True)

    competition.refresh_from_db()
    assert competition.is_archived
    assert competition.archived_rules == before
    assert [e.bib_number for e in competition.entry_rows()] == [7]


def test_the_dialog_lists_only_the_settings_that_have_actually_moved():
    competition = _with_a_running_class()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)

    assert archiving.rule_differences(competition) == []

    competition.competition_type.pylon_penalty = 10
    competition.competition_type.save()
    competition.refresh_from_db()

    moved = archiving.rule_differences(competition)
    assert [row["field"] for row in moved] == ["pylon_penalty"]
    assert moved[0]["incoming"] == 5 and moved[0]["current"] == 10


def test_keeping_an_archived_setting_writes_it_back_to_the_live_type(client):
    competition = _with_a_running_class()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 10
    competition.competition_type.save()

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "full", "setting-pylon_penalty": "incoming"})

    competition.competition_type.refresh_from_db()
    assert competition.competition_type.pylon_penalty == 5
    # …and the archived event is unmoved either way, which is the whole point.
    competition.refresh_from_db()
    assert competition.rules.pylon_penalty == 5


def test_keeping_the_current_setting_leaves_the_type_alone(client):
    competition = _with_a_running_class()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 10
    competition.competition_type.save()

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "full", "setting-pylon_penalty": "current"})

    competition.competition_type.refresh_from_db()
    assert competition.competition_type.pylon_penalty == 10


def test_a_setup_only_duplicate_never_touches_the_type(client):
    """A copy made to set up next season should run under this season's rules,
    so the reconciliation is not even asked — and a stale page that posts an
    answer anyway must not be able to apply one."""
    competition = _with_a_running_class()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 10
    competition.competition_type.save()

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "setup", "setting-pylon_penalty": "incoming"})

    competition.competition_type.refresh_from_db()
    assert competition.competition_type.pylon_penalty == 10


# ----- a full duplicate resolves its competitors like an import -----
#
# The three things asked of it: a competitor whose record has changed since is
# an operator decision, not a silent choice; one who no longer exists is put
# back; and neither counts as a *use* of the person, so the retention clock
# (Participant.last_used_at) is not restarted by copying an old event.

def _archived_with(bib=7, **participant):
    competition = _with_a_running_class()
    person = _entered(competition, bib, **participant)
    archiving.archive(competition)
    return competition, person


def test_a_competitor_whose_record_changed_needs_a_decision():
    competition, person = _archived_with()
    person.club = "Moved Clubs"
    person.save()

    plan = duplication.plan(competition)

    assert [m.status for m in plan.matches] == ["conflict"]
    assert [d.field for d in plan.matches[0].candidates[0].diffs] == ["club"]


def test_a_competitor_who_has_not_changed_needs_no_decision():
    competition, _person = _archived_with()

    plan = duplication.plan(competition)

    assert [m.status for m in plan.matches] == ["identical"]
    assert not plan.needs_review


def test_a_deleted_competitor_is_recreated():
    competition, person = _archived_with()
    person.delete()

    plan = duplication.plan(competition)
    assert [m.status for m in plan.matches] == ["new"]

    copy = duplication.copy(competition, with_data=True, competitor_plan=plan)
    rows = copy.entry_rows()
    assert [e.bib_number for e in rows] == [7]
    assert rows[0].participant.last_name == "Lovelace"


def test_duplicating_does_not_restart_the_retention_clock():
    """Copying a finished event is not somebody racing again. last_used_at is
    the field a retention sweep ages off (see Participant), so a duplicate that
    stamped it fresh would keep every competitor of every archived event alive
    for ever."""
    competition, person = _archived_with()
    long_ago = datetime.datetime(2019, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
    Participant.objects.filter(pk=person.pk).update(last_used_at=long_ago)

    duplication.copy(competition, with_data=True)

    person.refresh_from_db()
    assert person.last_used_at == long_ago


def test_a_recreated_competitor_keeps_the_last_use_they_had():
    competition, person = _archived_with()
    long_ago = datetime.datetime(2019, 5, 1, 12, 0, tzinfo=datetime.timezone.utc)
    Participant.objects.filter(pk=person.pk).update(last_used_at=long_ago)
    # Re-archive so the snapshot carries that date, then lose the person.
    competition.archived_starters.all().delete()
    competition.archived_at = None
    competition.save(update_fields=["archived_at"])
    archiving.archive(competition)
    person.delete()

    copy = duplication.copy(competition, with_data=True)

    restored = Participant.objects.get(last_name="Lovelace")
    assert restored.last_used_at == long_ago
    assert copy.entry_rows()[0].participant.last_name == "Lovelace"


def test_a_full_duplicate_stops_at_the_merge_window(client):
    """Nothing is written until the decisions are made — the review is a step,
    not a warning you can click past."""
    competition, person = _archived_with()
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    person.club = "Moved Clubs"
    person.save()

    response = client.post(
        reverse("competitions:duplicate", args=[competition.pk]), {"mode": "full"})

    assert response.status_code == 200
    assert b"choice-" in response.content
    assert Competition.objects.count() == 1


def test_the_merge_window_keeps_the_record_on_file_by_default(client):
    """Unlike an import, the copy leads with what is on file *now*: the person
    moved house since, the copy is a new live event, and the archived original
    keeps its own version whatever is chosen here."""
    competition, person = _archived_with()
    person.club = "Moved Clubs"
    person.save()

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "full", "step": "review",
                 f"choice-{person.pk}": f"merge:{person.pk}"})

    person.refresh_from_db()
    assert person.club == "Moved Clubs"
    copy = Competition.objects.exclude(pk=competition.pk).get()
    assert copy.entry_rows()[0].participant.club == "Moved Clubs"
    # The archived event still shows them as they raced.
    assert competition.entry_rows()[0].participant.club == "Old Club"


def test_the_merge_window_can_take_the_archived_value_instead(client):
    competition, person = _archived_with()
    person.club = "Typo Clbu"
    person.save()

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "full", "step": "review",
                 f"choice-{person.pk}": f"merge:{person.pk}",
                 f"field-{person.pk}-{person.pk}-club": "imported"})

    person.refresh_from_db()
    assert person.club == "Old Club"


def test_the_merge_window_can_keep_them_apart(client):
    """Two people can share a name and a birthday. Choosing "a different person"
    registers the competitor separately rather than folding two into one."""
    competition, person = _archived_with()
    person.club = "Someone Else"
    person.save()

    client.post(reverse("competitions:duplicate", args=[competition.pk]),
                {"mode": "full", "step": "review", f"choice-{person.pk}": "create"})

    assert Participant.objects.filter(last_name="Lovelace").count() == 2
    copy = Competition.objects.exclude(pk=competition.pk).get()
    assert copy.entry_rows()[0].participant.pk != person.pk


def test_an_unchanged_field_never_reaches_the_merge_window(client):
    """No decision to make means no screen to click through: a copy of an event
    whose people are all still on file exactly as they raced goes straight
    through."""
    competition, _person = _archived_with()
    competition.is_active = True
    competition.save(update_fields=["is_active"])

    response = client.post(
        reverse("competitions:duplicate", args=[competition.pk]), {"mode": "full"})

    assert response.status_code == 302
    assert Competition.objects.count() == 2


# ----- the participants screen is part of the freeze too -----

def test_the_participant_list_shows_an_archived_event_as_it_froze_it(client):
    """The one screen that still read the live table. A Participant belongs to
    the *discipline*, so editing a vehicle to set up next season changed what a
    finished event said its competitors drove — and this is the page an operator
    looks at to check that it didn't."""
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    Participant.objects.filter(pk=person.pk).update(vehicle="Before archive")
    competition.competition_type.requires_vehicle = True
    competition.competition_type.save()
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    archiving.archive(competition)

    Participant.objects.filter(pk=person.pk).update(vehicle="After archive")

    page = client.get(reverse("participants:list")).content.decode()
    assert "Before archive" in page
    assert "After archive" not in page


def test_a_live_event_still_shows_the_participant_list_live(client):
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    Participant.objects.filter(pk=person.pk).update(club="Brand New Club")

    page = client.get(reverse("participants:list")).content.decode()
    assert "Brand New Club" in page


def test_the_participant_list_offers_no_way_to_edit_an_archived_event(client):
    """Edit and Delete are *links*, and the read-only sweep disables form
    controls — a link is not one, deliberately, because every PDF export is a
    link. So these have to go from the template."""
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    archiving.archive(competition)

    page = client.get(reverse("participants:list")).content.decode()

    assert reverse("participants:edit", args=[person.pk]) not in page
    assert reverse("participants:delete", args=[person.pk]) not in page
    assert reverse("participants:add") not in page


def test_the_participant_list_still_offers_them_on_a_live_event(client):
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    competition.is_active = True
    competition.save(update_fields=["is_active"])

    page = client.get(reverse("participants:list")).content.decode()

    assert reverse("participants:edit", args=[person.pk]) in page
    assert reverse("participants:delete", args=[person.pk]) in page


# ----- a pop-up opens over the page it was opened from -----

def test_the_archived_settings_open_over_the_competition_list(client):
    """A dialog that replaces the page behind it is a page with a box on it:
    closing it leaves the operator somewhere they never navigated to, and the
    tile they pressed is gone."""
    competition = make_active_competition()
    archiving.archive(competition)

    page = client.get(
        reverse("competitions:archived-rules", args=[competition.pk])
    ).content.decode()

    assert "competition-tiles" in page
    assert competition.name in page
    assert "data-modal-open" in page


def test_the_duplicate_dialog_opens_over_the_competition_list(client):
    competition = make_active_competition()
    archiving.archive(competition)

    page = client.post(
        reverse("competitions:duplicate", args=[competition.pk])
    ).content.decode()

    assert "competition-tiles" in page
    assert "data-modal-open" in page


# ----- an archive is the *starters*, not the address book -----
#
# A Participant belongs to the discipline and may be on file for years without
# entering anything; an EventEntry is a bib (the column is not nullable, and
# clearing a bib deletes the row). So the line between "was in this event" and
# "is known to this club" is the entry, and the archive is drawn on it.

def _on_file_only(competition, first="Never", last="Entered"):
    """Somebody registered under the discipline who never got a bib here."""
    from apps.participants.models import Participant

    return Participant.objects.create(
        competition_type=competition.competition_type,
        first_name=first, last_name=last,
        date_of_birth=datetime.date(2010, 1, 1),
    )


def test_a_participant_without_a_bib_is_not_archived():
    competition = _with_a_running_class()
    _entered(competition, 7)
    outsider = _on_file_only(competition)

    archiving.archive(competition)

    frozen = {row.participant_pk for row in competition.archived_starters.all()}
    assert outsider.pk not in frozen
    assert competition.archived_starters.count() == 1


def test_a_class_assignment_without_a_bib_is_not_archived_either():
    """Assigning somebody to a class is setup, not entry — they have no bib and
    no start list place, so they were never in the event."""
    from apps.participants.models import ClassAssignment

    competition = _with_a_running_class()
    _entered(competition, 7)
    outsider = _on_file_only(competition)
    ClassAssignment.objects.create(
        participant=outsider,
        competition_class=competition.classes.get(name="1"),
    )

    archiving.archive(competition)

    assert competition.archived_starters.count() == 1
    assert [e.bib_number for e in competition.entry_rows()] == [7]


def test_the_archived_participant_list_shows_only_who_had_a_bib(client):
    """The live list is the discipline's address book with an "active only"
    filter over it. An archived event has no such distinction — it is the
    starters, and nothing else was ever part of it."""
    competition = _with_a_running_class()
    _entered(competition, 7, last="Started")
    _on_file_only(competition, last="NeverStarted")
    competition.is_active = True
    competition.save(update_fields=["is_active"])
    archiving.archive(competition)

    page = client.get(reverse("participants:list")).content.decode()

    assert "Started" in page
    assert "NeverStarted" not in page


def test_the_live_participant_list_still_shows_everybody(client):
    competition = _with_a_running_class()
    _entered(competition, 7, last="Started")
    _on_file_only(competition, last="NeverStarted")
    competition.is_active = True
    competition.save(update_fields=["is_active"])

    page = client.get(reverse("participants:list")).content.decode()

    assert "Started" in page
    assert "NeverStarted" in page


def test_a_bib_cleared_before_archiving_takes_them_out_of_it():
    """Clearing a bib deletes the entry (EventEntry.bib_number is not nullable),
    so somebody withdrawn before the event was signed off is not in it."""
    from apps.participants.models import EventEntry

    competition = _with_a_running_class()
    staying = _entered(competition, 7, last="Staying")
    withdrawn = _entered(competition, 8, last="Withdrawn")
    EventEntry.objects.filter(competition=competition, participant=withdrawn).delete()

    archiving.archive(competition)

    frozen = {row.participant_pk for row in competition.archived_starters.all()}
    assert frozen == {staying.pk}


# ----- the frozen settings, beside what the type says today -----

def test_the_settings_summary_carries_the_live_value_and_whether_it_moved():
    competition = make_competition()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 9
    competition.competition_type.save()
    competition.refresh_from_db()

    rows = {r["label"]: r
            for g in archiving.rule_summary(competition) for r in g["rows"]}

    moved = rows["Pylon"]
    assert (moved["value"], moved["current"], moved["changed"]) == (5, 9, True)
    # Everything else is untouched, and says so rather than being left blank.
    steady = rows["Task"]
    assert steady["changed"] is False
    assert steady["current"] == steady["value"]


def test_the_settings_popup_shows_both_columns_and_marks_what_changed(client):
    competition = make_active_competition()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)
    competition.competition_type.pylon_penalty = 9
    competition.competition_type.save()

    page = client.get(
        reverse("competitions:archived-rules", args=[competition.pk])
    ).content.decode()

    assert "As archived" in page and "Type today" in page
    assert ">5<" in page and ">\n                    9" in page.replace("\r", "") or "9" in page
    # The marker is not colour alone: the state is a word too.
    assert "archived-rules-now--changed" in page
    assert "changed:" in page and "unchanged:" in page


def test_the_settings_popup_says_so_when_nothing_has_moved(client):
    competition = make_active_competition()
    archiving.archive(competition)

    page = client.get(
        reverse("competitions:archived-rules", args=[competition.pk])
    ).content.decode()

    assert "Nothing has changed" in page
    assert "archived-rules-now--changed" not in page


# ----- an archived event has nothing to configure -----

def test_an_archived_current_event_offers_no_configure_button(client):
    """The setup sub-pages edit the active competition, and an archived one has
    nothing for them to change — every setting it does have is under "Archived
    settings". A button onto a page of disabled fields is worse than no button."""
    competition = make_active_competition()
    archiving.archive(competition)

    page = client.get(reverse("competitions:list")).content.decode()

    assert "Configure" not in page
    assert reverse("competitions:archived-rules", args=[competition.pk]) in page


def test_a_live_current_event_still_offers_configure(client):
    make_active_competition()

    page = client.get(reverse("competitions:list")).content.decode()

    assert "Configure" in page


def test_an_archived_event_that_is_not_current_can_still_be_selected(client):
    """Selecting it is how its results are opened at all, so that stays."""
    competition = make_competition()
    archiving.archive(competition)

    page = client.get(reverse("competitions:list")).content.decode()

    assert reverse("competitions:select", args=[competition.pk]) in page


def test_every_frozen_setting_is_in_exactly_one_group():
    """The pop-up's groups mirror the settings page's sections, and the mapping
    is spelled out by hand — so a field added to CompetitionType later has to be
    given a home. It falls into the last group rather than disappearing, and this
    fails so that placement is a decision instead of an accident."""
    claimed = [name for _title, names in archiving.RULE_GROUPS for name in names]

    assert len(claimed) == len(set(claimed)), "a setting is in two groups"
    unclaimed = sorted(set(archiving.FROZEN_FIELDS) - set(claimed))
    assert not unclaimed, f"settings in no group (they land in the last one): {unclaimed}"
    stale = sorted(set(claimed) - set(archiving.FROZEN_FIELDS))
    assert not stale, f"groups name settings that no longer exist: {stale}"


def test_the_settings_summary_groups_the_way_the_settings_page_does():
    competition = make_competition()
    archiving.archive(competition)

    titles = [group["title"] for group in archiving.rule_summary(competition)]

    assert titles == ["", "Penalties", "Evaluation", "Required participant info"]


def test_a_penalty_amount_carries_its_unit():
    competition = make_competition()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)

    rows = {r["label"]: r
            for g in archiving.rule_summary(competition) for r in g["rows"]}

    assert rows["Pylon"]["value"] == 5
    assert rows["Pylon"]["value_unit"] == "s"
    # A word or a switch has no unit to carry.
    assert rows["Tie-break"]["value_unit"] == ""
    # …and neither does the precision, whose own label already says "1/100 s".
    assert rows["Timing precision"]["value_unit"] == ""


def test_a_penalty_that_was_never_set_shows_no_unit():
    """"— s" is not a reading of an amount that does not exist."""
    competition = make_competition()
    competition.competition_type.pylon_penalty = None
    competition.competition_type.save()
    archiving.archive(competition)

    rows = {r["label"]: r
            for g in archiving.rule_summary(competition) for r in g["rows"]}

    assert rows["Pylon"]["value"] == "—"
    assert rows["Pylon"]["value_unit"] == ""


def test_the_popup_renders_the_groups_and_the_units(client):
    competition = make_active_competition()
    competition.competition_type.pylon_penalty = 5
    competition.competition_type.save()
    archiving.archive(competition)

    page = client.get(
        reverse("competitions:archived-rules", args=[competition.pk])
    ).content.decode()

    assert "archived-rules-group" in page
    assert "Penalties" in page and "Evaluation" in page
    assert 'class="settings-unit">s<' in page


def test_duplicating_survives_a_competitor_the_form_would_now_refuse():
    """A value already in the database is not a value arriving from outside.

    Participant.date_of_birth carries a validator (nothing before 1900 — a
    slipped keystroke used to give age-based classes a nonsense age), and
    apps/transfer/schema.load runs it, which is exactly right for a *document*:
    a damaged file has to be refused rather than written. But a row this
    database already holds got in some other way, and re-validating it on the
    way out can only fail for data the operator cannot reach — an archived
    event is read-only. It crashed the duplicate of a real event.
    """
    competition = _with_a_running_class()
    person = _entered(competition, 7)
    # update(), like whatever put it there: Model.save() does not validate.
    Participant.objects.filter(pk=person.pk).update(
        date_of_birth=datetime.date(1512, 2, 21))
    archiving.archive(competition)

    copy = duplication.copy(competition, with_data=True)

    rows = copy.entry_rows()
    assert [e.bib_number for e in rows] == [7]
    assert rows[0].participant.date_of_birth == datetime.date(1512, 2, 21)


def test_an_import_still_refuses_such_a_value_in_a_file():
    """The mirror, and the reason the validation exists: a *document* is hostile
    until checked, so a damaged one is still a sentence rather than a row every
    later read chokes on."""
    from apps.participants.models import Participant
    from apps.transfer import schema

    with pytest.raises(schema.TransferError):
        schema.load(
            Participant,
            {"first_name": "A", "last_name": "B", "date_of_birth": "1512-02-21"},
            schema.PARTICIPANT_FIELDS,
        )
