"""Tests for scenario.py and sim/config/*.json (Agent 2).

Static checks only: these prove the inputs and task graphs are consistent,
not that the engine finishes orders on time.
"""

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow plain `pytest` from the repo root
    sys.path.insert(0, str(ROOT))

import scenario  # noqa: E402
from scenario import (  # noqa: E402
    DEFAULT_CONFIG_DIR, TASK_FIELDS, effective_minutes, expand_order, load_config,
    validate_config,
)

VARIANTS = ("baseline", "disruption", "blocker")
WORKER_IDS = ["w_grill", "w_prep", "w_fry", "w_expo"]


@pytest.fixture
def baseline():
    return load_config(DEFAULT_CONFIG_DIR, "baseline")


@pytest.fixture
def config_copy(tmp_path):
    """A writable copy of the shipped config directory."""
    target = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, target)
    return target


def order(config, order_id):
    return next(o for o in config["orders"] if o["id"] == order_id)


def tasks_by_id(expansion):
    return {task["id"]: task for task in expansion["tasks"]}


def rewrite(path, change):
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


def invalid(config, change, match):
    config = copy.deepcopy(config)
    change(config)
    with pytest.raises(ValueError, match=match):
        validate_config(config)


# --------------------------------------------------------------------------
# Shipped config
# --------------------------------------------------------------------------

@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_loads_and_validates(variant):
    config = load_config(DEFAULT_CONFIG_DIR, variant)
    validate_config(config)
    assert config["variant"] == variant
    assert [w["id"] for w in config["workers"]] == WORKER_IDS
    assert len(config["orders"]) == config["target"]["orders"] == 10
    assert config["target"]["deadline"] == 30
    assert json.loads(json.dumps(config)) == config  # JSON-serializable, freezable


def test_shipped_menu_matches_plan(baseline):
    menu = baseline["menu"]
    burger = [(s["id"], s["skill"], s["station"]) for s in menu["recipes"]["burger"]["steps"]]
    assert burger == [("patty", "grill", "grill"), ("toppings", "prep", "prep"),
                      ("assemble", "assemble", "assembly")]
    fries = [(s["id"], s["skill"], s["station"]) for s in menu["recipes"]["fries"]["steps"]]
    assert fries == [("fry", "fry", "fryer")]
    assert [s["id"] for s in menu["order_steps"]] == ["pack", "verify"]
    assert set(menu["recipes"]["burger"]["modifiers"]) == {"no_lettuce", "no_cheese", "extra_cheese"}


def test_every_worker_has_a_matching_persona(baseline):
    """Agent 3 owns sim/personas/<worker_id>.txt; the IDs must line up exactly."""
    personas_dir = ROOT / "sim" / "personas"
    if not personas_dir.is_dir():
        pytest.skip("sim/personas/ not present in this checkout yet (Agent 3)")
    personas = {p.stem for p in personas_dir.glob("*.txt")}
    assert personas == {w["id"] for w in baseline["workers"]}
    for persona in personas:
        assert (personas_dir / f"{persona}.txt").read_text(encoding="utf-8").strip()


def test_shipped_workers_have_a_single_grill_backup(baseline):
    grillers = [w["id"] for w in baseline["workers"] if "grill" in w["skills"]]
    assert grillers == ["w_grill", "w_expo"]
    verifiers = [w["id"] for w in baseline["workers"] if "verify" in w["skills"]]
    assert verifiers == ["w_expo"]


def test_blocker_variant_removes_backup_grill_skill_only_in_memory():
    blocker = load_config(DEFAULT_CONFIG_DIR, "blocker")
    w_expo = next(w for w in blocker["workers"] if w["id"] == "w_expo")
    assert "grill" not in w_expo["skills"]
    assert scenario.missing_skills_after_events(blocker) == ["grill"]

    baseline_after = load_config(DEFAULT_CONFIG_DIR, "baseline")
    w_expo = next(w for w in baseline_after["workers"] if w["id"] == "w_expo")
    assert "grill" in w_expo["skills"]


def test_disruption_keeps_a_qualified_backup():
    disruption = load_config(DEFAULT_CONFIG_DIR, "disruption")
    assert disruption["events"] == [{"tick": 5, "type": "worker_unavailable", "worker_id": "w_grill"}]
    assert scenario.missing_skills_after_events(disruption) == []


def test_loading_never_changes_config_files():
    def digests():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(DEFAULT_CONFIG_DIR.glob("*.json"))}

    before = digests()
    for variant in VARIANTS:
        config = load_config(DEFAULT_CONFIG_DIR, variant)
        config["workers"][0]["skills"].clear()  # mutate the copy
        config["menu"]["ingredients"]["patty"]["stock"] = 0
    assert digests() == before
    fresh = load_config(DEFAULT_CONFIG_DIR, "baseline")
    assert fresh["workers"][0]["skills"] and fresh["menu"]["ingredients"]["patty"]["stock"] > 0
    assert fresh["source"]["sha256"] == before


@pytest.mark.parametrize("variant", ["disruption", "blocker"])
def test_grill_work_is_unfinished_at_the_disruption(variant):
    """Any schedule still has grill work left when the grill cook leaves."""
    config = load_config(DEFAULT_CONFIG_DIR, variant)
    tick = config["events"][0]["tick"]
    assert tick == 5
    assert scenario.skill_work_lower_bound(config, "grill") > tick
    # and some burgers have not even arrived yet
    assert any(o["arrival"] > tick and any(i["recipe"] == "burger" for i in o["items"])
               for o in config["orders"])


def test_shipped_demand_fits_stock(baseline):
    demand = scenario.ingredient_demand(baseline)
    assert demand == {"bag": 10, "bun": 7, "cheese": 6, "lettuce": 80, "patty": 7, "potato": 1350}
    for ingredient_id, needed in demand.items():
        assert needed <= baseline["menu"]["ingredients"][ingredient_id]["stock"]


def test_shipped_orders_exercise_every_modifier(baseline):
    used = {m for o in baseline["orders"] for line in o["items"] for m in line["modifiers"]}
    assert used == {"no_lettuce", "no_cheese", "extra_cheese"}
    assert any(len(line["modifiers"]) > 1 for o in baseline["orders"] for line in o["items"])
    assert any(line["quantity"] > 1 for o in baseline["orders"] for line in o["items"])


# --------------------------------------------------------------------------
# Task expansion
# --------------------------------------------------------------------------

def test_modified_multi_quantity_order_expansion(baseline):
    """The handoff example: O2 = 2 burgers no_lettuce + 1 fries."""
    expansion = expand_order(order(baseline, "O2"), baseline["menu"])
    tasks = tasks_by_id(expansion)
    assert list(tasks) == ["O2.b1.patty", "O2.b1.assemble", "O2.b2.patty", "O2.b2.assemble",
                           "O2.f1.fry", "O2.pack", "O2.verify"]
    for task in expansion["tasks"]:
        assert tuple(task) == TASK_FIELDS
        assert task["status"] == "pending"
        assert task["order_id"] == "O2"
    # no_lettuce removes toppings and rewires assemble straight to patty
    assert tasks["O2.b1.assemble"]["depends_on"] == ["O2.b1.patty"]
    assert tasks["O2.b2.assemble"]["depends_on"] == ["O2.b2.patty"]
    assert tasks["O2.pack"]["depends_on"] == ["O2.b1.assemble", "O2.b2.assemble", "O2.f1.fry"]
    assert tasks["O2.verify"]["depends_on"] == ["O2.pack"]
    assert tasks["O2.pack"]["item_id"] is None and tasks["O2.verify"]["item_id"] is None
    assert tasks["O2.pack"]["ingredients"] == {"bag": 1}
    assert tasks["O2.verify"]["ingredients"] == {}
    assert tasks["O2.b1.patty"]["station_id"] == "grill"
    assert tasks["O2.b1.patty"]["required_skill"] == "grill"
    assert tasks["O2.b1.patty"]["base_minutes"] == 2


def test_plain_burger_chain(baseline):
    tasks = tasks_by_id(expand_order(order(baseline, "O1"), baseline["menu"]))
    assert tasks["O1.b1.patty"]["depends_on"] == []
    assert tasks["O1.b1.toppings"]["depends_on"] == ["O1.b1.patty"]
    assert tasks["O1.b1.assemble"]["depends_on"] == ["O1.b1.toppings"]
    assert tasks["O1.b1.assemble"]["ingredients"] == {"bun": 1, "cheese": 1}


def test_expected_items_describe_each_unit(baseline):
    expansion = expand_order(order(baseline, "O2"), baseline["menu"])
    items = expansion["expected_items"]
    assert [i["item_id"] for i in items] == ["O2.b1", "O2.b2", "O2.f1"]
    assert items[0] == {
        "item_id": "O2.b1", "order_id": "O2", "recipe_id": "burger", "modifiers": ["no_lettuce"],
        "task_ids": ["O2.b1.patty", "O2.b1.assemble"], "final_task_id": "O2.b1.assemble",
        "ingredients": {"bun": 1, "cheese": 1, "patty": 1},
    }
    assert items[2]["ingredients"] == {"potato": 150}
    # every unit's tasks are its own chain
    all_ids = [tid for item in items for tid in item["task_ids"]]
    assert len(all_ids) == len(set(all_ids))


def test_quantities_produce_independent_units(baseline):
    o4 = expand_order(order(baseline, "O4"), baseline["menu"])  # 2 fries
    assert [i["item_id"] for i in o4["expected_items"]] == ["O4.f1", "O4.f2"]
    assert tasks_by_id(o4)["O4.pack"]["depends_on"] == ["O4.f1.fry", "O4.f2.fry"]


def test_units_are_numbered_across_lines_with_their_own_modifiers(baseline):
    """O8 = burger (plain) + burger (no_cheese, no_lettuce)."""
    expansion = expand_order(order(baseline, "O8"), baseline["menu"])
    items = {i["item_id"]: i for i in expansion["expected_items"]}
    assert list(items) == ["O8.b1", "O8.b2"]
    assert items["O8.b1"]["modifiers"] == []
    assert items["O8.b2"]["modifiers"] == ["no_cheese", "no_lettuce"]
    tasks = tasks_by_id(expansion)
    assert "O8.b1.toppings" in tasks and "O8.b2.toppings" not in tasks
    assert tasks["O8.b1.assemble"]["ingredients"] == {"bun": 1, "cheese": 1}
    assert tasks["O8.b2.assemble"]["ingredients"] == {"bun": 1}
    assert tasks["O8.b2.assemble"]["depends_on"] == ["O8.b2.patty"]


def test_cheese_modifiers_use_configured_amounts(baseline):
    menu = baseline["menu"]
    configured = menu["recipes"]["burger"]["modifiers"]["extra_cheese"]["set_ingredients"]["assemble"]["cheese"]
    assert configured == 2
    line = {"recipe": "burger", "quantity": 3, "modifiers": ["extra_cheese"]}
    expansion = expand_order({"id": "X", "arrival": 0, "deadline": 9, "items": [line]}, menu)
    assembles = [t for t in expansion["tasks"] if t["step_id"] == "assemble"]
    assert len(assembles) == 3
    assert all(t["ingredients"]["cheese"] == configured for t in assembles)

    line["modifiers"] = ["no_cheese"]
    expansion = expand_order({"id": "X", "arrival": 0, "deadline": 9, "items": [line]}, menu)
    assert all("cheese" not in t["ingredients"] for t in expansion["tasks"])
    assert all("cheese" not in i["ingredients"] for i in expansion["expected_items"])


def test_modifier_order_does_not_matter(baseline):
    menu = baseline["menu"]

    def expand(mods):
        line = {"recipe": "burger", "quantity": 1, "modifiers": mods}
        return expand_order({"id": "X", "arrival": 0, "deadline": 9, "items": [line]}, menu)

    assert expand(["no_lettuce", "extra_cheese"]) == expand(["extra_cheese", "no_lettuce"])


@pytest.mark.parametrize("recipe, modifiers, match", [
    ("burger", ["no_pickles"], "unknown modifier 'no_pickles'"),
    ("fries", ["no_cheese"], "unknown modifier 'no_cheese' for Fries"),
    ("burger", ["no_cheese", "extra_cheese"], "contradictory modifiers"),
    ("burger", ["extra_cheese", "no_cheese"], "contradictory modifiers"),
    ("burger", ["no_lettuce", "no_lettuce"], "repeated modifier"),
    ("burger", "no_lettuce", "modifiers must be a list"),
    ("burger", [3], "modifiers must be a list"),
])
def test_bad_modifiers_are_rejected(baseline, recipe, modifiers, match):
    bad = {"id": "X", "arrival": 0, "deadline": 9,
           "items": [{"recipe": recipe, "quantity": 1, "modifiers": modifiers}]}
    with pytest.raises(ValueError, match=match):
        expand_order(bad, baseline["menu"])


def test_skip_and_set_on_same_step_is_contradictory(baseline):
    menu = copy.deepcopy(baseline["menu"])
    menu["recipes"]["burger"]["modifiers"]["extra_lettuce"] = {
        "label": "Extra lettuce", "set_ingredients": {"toppings": {"lettuce": 40}}}
    line = {"recipe": "burger", "quantity": 1, "modifiers": ["no_lettuce", "extra_lettuce"]}
    with pytest.raises(ValueError, match="contradictory modifiers 'no_lettuce' skips step"):
        expand_order({"id": "X", "arrival": 0, "deadline": 9, "items": [line]}, menu)


@pytest.mark.parametrize("line, match", [
    ({"recipe": "pizza", "quantity": 1, "modifiers": []}, "unknown recipe"),
    ({"recipe": "burger", "quantity": 0, "modifiers": []}, "quantity must be a positive integer"),
    ({"recipe": "burger", "quantity": 1.0, "modifiers": []}, "quantity must be a positive integer"),
    ({"recipe": "burger", "quantity": True, "modifiers": []}, "quantity must be a positive integer"),
    ({"recipe": "burger", "quantity": 1}, "missing field"),
    ({"recipe": "burger", "quantity": 1, "modifiers": [], "notes": "x"}, "unknown field"),
])
def test_bad_order_lines_are_rejected(baseline, line, match):
    with pytest.raises(ValueError, match=match):
        expand_order({"id": "X", "arrival": 0, "deadline": 9, "items": [line]}, baseline["menu"])


def test_order_ids_must_be_task_id_safe(baseline):
    line = {"recipe": "fries", "quantity": 1, "modifiers": []}
    with pytest.raises(ValueError, match="invalid id"):
        expand_order({"id": "O.1", "arrival": 0, "deadline": 9, "items": [line]}, baseline["menu"])


def test_expansion_is_pure_and_repeatable(baseline):
    menu_before = copy.deepcopy(baseline["menu"])
    o2 = order(baseline, "O2")
    o2_before = copy.deepcopy(o2)
    first = expand_order(o2, baseline["menu"])
    second = expand_order(o2, baseline["menu"])
    assert first == second
    assert baseline["menu"] == menu_before and o2 == o2_before
    # outputs do not alias the menu or each other
    first["tasks"][0]["ingredients"]["patty"] = 99
    first["tasks"][0]["depends_on"].append("junk")
    assert baseline["menu"] == menu_before
    assert expand_order(o2, baseline["menu"]) == second


def test_task_ids_unique_across_all_orders(baseline):
    ids = [t["id"] for exp in scenario.expand_all(baseline).values() for t in exp["tasks"]]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("base, multiplier, expected", [
    (2, 1.0, 2), (2, 1.5, 3), (1, 1.5, 2), (1, 1, 1), (4, 0.5, 2), (1, 0.5, 1),
    (50, 1.1, 55),  # float 50 * 1.1 = 55.00000000000001 must not round up to 56
    (100, 1.1, 110),
    (3, 1.2, 4), (5, 0.2, 1),
])
def test_effective_minutes(base, multiplier, expected):
    assert effective_minutes(base, multiplier) == expected


@pytest.mark.parametrize("base, multiplier", [(0, 1.0), (1.5, 1.0), (2, 0), (2, -1), (2, True), (True, 1)])
def test_effective_minutes_rejects_bad_input(base, multiplier):
    with pytest.raises(ValueError):
        effective_minutes(base, multiplier)


# --------------------------------------------------------------------------
# validate_config rejections
# --------------------------------------------------------------------------

def burger_step(config, step_id):
    return next(s for s in config["menu"]["recipes"]["burger"]["steps"] if s["id"] == step_id)


def worker(config, worker_id):
    return next(w for w in config["workers"] if w["id"] == worker_id)


@pytest.mark.parametrize("change, match", [
    (lambda c: burger_step(c, "patty").update(station="oven"), "unknown station 'oven'"),
    (lambda c: burger_step(c, "patty").update(depends_on=["assemble"]), "dependency cycle"),
    (lambda c: burger_step(c, "toppings").update(depends_on=["bake"]), "unknown dependency 'bake'"),
    (lambda c: burger_step(c, "patty").update(minutes=0), "minutes must be a positive integer"),
    (lambda c: burger_step(c, "patty").update(minutes=1.5), "minutes must be a positive integer"),
    (lambda c: burger_step(c, "patty").update(ingredients={"tofu": 1}), "unknown ingredient 'tofu'"),
    (lambda c: burger_step(c, "patty").update(ingredients={"patty": 0}), "must be an integer >= 1"),
    (lambda c: burger_step(c, "patty").update(depend_on=[]), "unknown field"),
    (lambda c: burger_step(c, "toppings").update(depends_on=[]), "exactly one final step"),
    (lambda c: c["menu"]["ingredients"]["patty"].update(stock=3), "insufficient patty"),
    (lambda c: c["menu"]["ingredients"]["bag"].update(stock=9), "insufficient bag"),
    (lambda c: c["menu"]["ingredients"]["patty"].update(stock=-1), "non-negative integer"),
    (lambda c: c["menu"]["stations"]["grill"].update(capacity=0), "capacity must be a positive integer"),
    (lambda c: c["menu"]["recipes"]["fries"].update(item_prefix="b"), "item_prefix 'b' already used"),
    (lambda c: c["menu"]["order_steps"][1].update(depends_on=[]), "'verify' must be the single final"),
    (lambda c: c["menu"]["order_steps"][0].update(after_items=False), "after_items"),
    (lambda c: c["menu"]["recipes"]["burger"]["modifiers"]["no_lettuce"].update(skip_steps=["bake"]),
     "cannot skip unknown step"),
    (lambda c: c["menu"]["recipes"]["burger"]["modifiers"]["no_cheese"]["set_ingredients"].update(
        assemble={"cheese": -1}), "must be an integer >= 0"),
    (lambda c: c["menu"]["recipes"]["fries"]["modifiers"].update(
        no_fry={"label": "No fry", "skip_steps": ["fry"]}), "skip every step"),
])
def test_menu_problems_are_rejected(baseline, change, match):
    invalid(baseline, change, match)


@pytest.mark.parametrize("change, match", [
    (lambda c: [w["skills"].pop("grill", None) for w in c["workers"]], "no worker has required skill"),
    (lambda c: worker(c, "w_grill")["skills"].update(gril=1.0), "not required by any menu step"),
    (lambda c: worker(c, "w_grill")["skills"].update(grill=0), "multiplier"),
    (lambda c: worker(c, "w_grill")["skills"].update(grill=-1.0), "multiplier"),
    (lambda c: worker(c, "w_grill")["skills"].update(grill=True), "multiplier"),
    (lambda c: worker(c, "w_grill")["skills"].update(grill=float("nan")), "multiplier"),
    (lambda c: worker(c, "w_grill").update(skills={}), "at least one skill"),
    (lambda c: worker(c, "w_prep").update(id="w_grill"), "duplicate worker id"),
    (lambda c: worker(c, "w_prep").update(id="Prep Cook"), "invalid id"),
    (lambda c: worker(c, "w_prep").pop("description"), "missing field"),
    (lambda c: c.update(workers={}), "workers must be a non-empty list"),
])
def test_worker_problems_are_rejected(baseline, change, match):
    invalid(baseline, change, match)


@pytest.mark.parametrize("change, match", [
    (lambda c: c["orders"][1].update(id="O1"), "duplicate order id"),
    (lambda c: c["orders"][0].update(deadline=31), "no later than 30"),
    (lambda c: c["orders"][0].update(arrival=12), "after arrival"),
    (lambda c: c["orders"][0].update(arrival=-1), "non-negative"),
    (lambda c: c["orders"][0].update(items=[]), "at least one item"),
    (lambda c: c["orders"].pop(), "10 but 9 orders"),
    (lambda c: c["target"].update(deadline=0), "target.deadline"),
    (lambda c: c["orders"][0]["items"][0].update(modifiers=["no_cheese", "extra_cheese"]),
     "contradictory modifiers"),
])
def test_order_problems_are_rejected(baseline, change, match):
    invalid(baseline, change, match)


def event(tick=5, worker_id="w_grill", type_="worker_unavailable"):
    return {"tick": tick, "type": type_, "worker_id": worker_id}


@pytest.mark.parametrize("events, match", [
    ([event(worker_id="ghost")], "unknown worker 'ghost'"),
    ([event(type_="worker_sick")], "unsupported type"),
    ([event(tick=30)], r"tick must be an integer minute in \[0, 30\)"),
    ([event(tick=-1)], "tick must be"),
    ([event(tick=5.0)], "tick must be"),
    ([event(tick=9, worker_id="w_prep"), event(tick=5)], "tick order"),
    ([event(), event(tick=7)], "already unavailable"),
    ([{"tick": 5, "type": "worker_unavailable"}], "missing field"),
])
def test_event_problems_are_rejected(baseline, events, match):
    invalid(baseline, lambda c: c.update(events=events), match)


def test_expected_missing_skills_must_match():
    disruption = load_config(DEFAULT_CONFIG_DIR, "disruption")
    invalid(disruption, lambda c: c.update(expected_missing_skills=["grill"]),
            "skills missing after events are none")
    blocker = load_config(DEFAULT_CONFIG_DIR, "blocker")
    invalid(blocker, lambda c: c.update(expected_missing_skills=[]),
            r"skills missing after events are \['grill'\]")


def test_malformed_shapes_raise_value_error(baseline):
    invalid(baseline, lambda c: c["menu"].update(recipes=[]), "menu.recipes")
    invalid(baseline, lambda c: c.update(orders=None), "orders must be a non-empty list")
    invalid(baseline, lambda c: c["orders"].__setitem__(0, "O1"), "each order must be an object")
    invalid(baseline, lambda c: c.pop("target"), "missing field")
    invalid(baseline, lambda c: c.update(extra=1), "unknown field")


# --------------------------------------------------------------------------
# load_config errors
# --------------------------------------------------------------------------

def test_unknown_variant_lists_choices():
    with pytest.raises(ValueError, match="unknown variant 'lunch'.*baseline, disruption, blocker"):
        load_config(DEFAULT_CONFIG_DIR, "lunch")


def test_missing_config_file(config_copy):
    (config_copy / "menu.json").unlink()
    with pytest.raises(ValueError, match="cannot read config file"):
        load_config(config_copy, "baseline")


def test_invalid_json(config_copy):
    (config_copy / "workers.json").write_text("{not json")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_config(config_copy, "baseline")


def test_duplicate_json_keys_are_rejected(config_copy):
    path = config_copy / "menu.json"
    text = path.read_text().replace('"bun":', '"patty": {"name": "x", "unit": "piece", "stock": 1, '
                                    '"location": "x"},\n    "bun":', 1)
    path.write_text(text)
    with pytest.raises(ValueError, match="duplicate JSON key 'patty'"):
        load_config(config_copy, "baseline")


def test_schema_version_is_checked(config_copy):
    rewrite(config_copy / "scenario.json", lambda d: d.update(schema_version=2))
    with pytest.raises(ValueError, match="unsupported schema_version"):
        load_config(config_copy, "baseline")


@pytest.mark.parametrize("overrides, match", [
    ({"nobody": {"remove_skills": ["grill"]}}, "unknown worker"),
    ({"w_prep": {"remove_skills": ["grill"]}}, "does not have skill 'grill'"),
    ({"w_expo": {"remove_skills": []}}, "non-empty list"),
    ({"w_expo": {"add_skills": ["fry"]}}, "missing field"),
])
def test_bad_worker_overrides(config_copy, overrides, match):
    rewrite(config_copy / "scenario.json",
            lambda d: d["variants"]["blocker"].update(worker_overrides=overrides))
    with pytest.raises(ValueError, match=match):
        load_config(config_copy, "blocker")


def test_other_variants_still_load_when_one_is_edited(config_copy):
    rewrite(config_copy / "scenario.json",
            lambda d: d["variants"]["blocker"]["events"].append(event(tick=4, worker_id="ghost")))
    load_config(config_copy, "baseline")
    with pytest.raises(ValueError):
        load_config(config_copy, "blocker")


def test_small_custom_scenario_is_accepted(config_copy):
    """Other agents' tests can use a one-order scenario with the same menu."""
    def shrink(doc):
        doc["target"]["orders"] = 1
        doc["orders"] = [{"id": "T1", "arrival": 0, "deadline": 10,
                          "items": [{"recipe": "burger", "quantity": 1, "modifiers": []}]}]
    rewrite(config_copy / "scenario.json", shrink)
    config = load_config(config_copy, "baseline")
    tasks = tasks_by_id(expand_order(config["orders"][0], config["menu"]))
    assert list(tasks) == ["T1.b1.patty", "T1.b1.toppings", "T1.b1.assemble", "T1.pack", "T1.verify"]


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def test_cli_validates_all_variants(capsys):
    assert scenario.main([]) == 0
    out = capsys.readouterr().out
    assert "Static checks passed for: baseline, disruption, blocker" in out
    assert "missing skills after events: grill" in out


def test_cli_prints_order_expansion(capsys):
    assert scenario.main(["--variant", "baseline", "--order", "O2"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [t["id"] for t in printed["tasks"]][-2:] == ["O2.pack", "O2.verify"]


def test_cli_reports_errors(capsys):
    assert scenario.main(["--order", "O99"]) == 1
    assert "unknown order 'O99'" in capsys.readouterr().err
