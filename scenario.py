"""Scenario inputs: config loading, validation, and pure task generation.

Owned by Agent 2 (see TASKS.md). Python 3.9+ standard library only.

Public interface used by the orchestrator and the other helpers:

    load_config(config_dir, variant) -> dict      fresh, validated, JSON-serializable
    validate_config(config) -> None                raises ValueError
    expand_order(order, menu) -> dict              {"tasks": [...], "expected_items": [...]}
    effective_minutes(base_minutes, multiplier)    ceil(base x multiplier), exact arithmetic

Nothing in this module mutates its inputs, writes files, or touches run state.
The config files are read-only seed data; variant overrides are applied to a
fresh copy on every load, so the files on disk never change.

Command line (handy for sharing examples with the other agents):

    python scenario.py                          # validate every variant, print a summary
    python scenario.py --variant blocker        # summary for one variant
    python scenario.py --order O2               # print one order's expanded tasks as JSON
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

SCHEMA_VERSION = 1
CONFIG_FILES = ("menu.json", "workers.json", "scenario.json")
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "sim" / "config"
EVENT_TYPES = ("worker_unavailable",)
TASK_FIELDS = (
    "id", "order_id", "item_id", "step_id", "required_skill", "station_id",
    "base_minutes", "ingredients", "depends_on", "status",
)
MAX_MULTIPLIER = 10

# Config identifiers are also used in file names (personas/<worker_id>.txt) and
# task IDs ("O2.b1.patty"), so they must not contain dots, slashes, or spaces.
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ORDER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PREFIX = re.compile(r"^[a-z]+$")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _is_int(value):
    return type(value) is int  # excludes bool


def _is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _check_keys(obj, required, optional, where):
    _require(isinstance(obj, dict), f"{where}: expected an object")
    missing = [key for key in required if key not in obj]
    _require(not missing, f"{where}: missing field(s) {', '.join(missing)}")
    unknown = sorted(set(obj) - set(required) - set(optional))
    _require(not unknown, f"{where}: unknown field(s) {', '.join(unknown)}")


def _check_text(value, where):
    _require(isinstance(value, str) and value.strip(), f"{where}: expected non-empty text")


def _check_id(value, where, pattern=_ID):
    _require(isinstance(value, str) and pattern.match(value) is not None,
             f"{where}: invalid id {value!r}")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def effective_minutes(base_minutes, multiplier):
    """Task duration for one worker: ceil(base_minutes x multiplier), at least 1.

    Uses exact decimal arithmetic so that, for example, 50 x 1.1 is 55, not 56
    (plain floats give 55.00000000000001, which ceil() rounds up to 56).
    The engine and the policies should both call this so they always agree.
    """
    _require(_is_int(base_minutes) and base_minutes > 0,
             f"base_minutes must be a positive integer, got {base_minutes!r}")
    _require(_is_number(multiplier) and multiplier > 0,
             f"skill multiplier must be a positive number, got {multiplier!r}")
    return max(1, math.ceil(Fraction(base_minutes) * Fraction(str(multiplier))))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_config(config_dir, variant):
    """Read the three seed files fresh and return one validated run config.

    The returned dict is a private copy: the variant's worker overrides are
    applied to it, never to the files. It is JSON-serializable so the
    orchestrator can freeze it into a run's checkpoint.
    """
    config_dir = Path(config_dir)
    raw, digests = {}, {}
    for name in CONFIG_FILES:
        path = config_dir / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read config file {path}: {exc}") from exc
        digests[name] = hashlib.sha256(data).hexdigest()
        try:
            raw[name] = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except ValueError as exc:  # includes JSONDecodeError and UnicodeDecodeError
            raise ValueError(f"{path}: invalid JSON: {exc}") from exc

    menu_doc = raw["menu.json"]
    workers_doc = raw["workers.json"]
    scenario_doc = raw["scenario.json"]
    _check_keys(menu_doc, ("schema_version", "ingredients", "stations", "recipes", "order_steps"),
                (), "menu.json")
    _check_keys(workers_doc, ("schema_version", "workers"), (), "workers.json")
    _check_keys(scenario_doc, ("schema_version", "name", "target", "orders", "variants"), (),
                "scenario.json")
    for name, doc in raw.items():
        _require(doc["schema_version"] == SCHEMA_VERSION,
                 f"{name}: unsupported schema_version {doc['schema_version']!r} "
                 f"(expected {SCHEMA_VERSION})")

    variants = scenario_doc["variants"]
    _require(isinstance(variants, dict) and variants,
             "scenario.json: 'variants' must be a non-empty object")
    if variant not in variants:
        raise ValueError(f"unknown variant {variant!r}; choose one of: {', '.join(variants)}")
    spec = variants[variant]
    where = f"scenario.json variant {variant!r}"
    _check_keys(spec, ("description",), ("events", "worker_overrides", "expected_missing_skills"),
                where)

    workers = workers_doc["workers"]
    _require(isinstance(workers, list), "workers.json: 'workers' must be a list")
    workers = _apply_worker_overrides(workers, spec.get("worker_overrides", {}), where)

    config = {
        "schema_version": SCHEMA_VERSION,
        "scenario": scenario_doc["name"],
        "variant": variant,
        "description": spec["description"],
        "target": scenario_doc["target"],
        "menu": {key: menu_doc[key] for key in ("ingredients", "stations", "recipes", "order_steps")},
        "workers": workers,
        "orders": scenario_doc["orders"],
        "events": spec.get("events", []),
        "expected_missing_skills": spec.get("expected_missing_skills", []),
        "source": {"config_dir": str(config_dir), "sha256": digests},
    }
    validate_config(config)
    # json.loads already produced fresh objects; deepcopy keeps that guarantee
    # explicit if this function is ever changed to cache the parsed files.
    return copy.deepcopy(config)


def _apply_worker_overrides(workers, overrides, where):
    """Return a copy of `workers` with skills removed as the variant asks."""
    workers = copy.deepcopy(workers)
    _require(isinstance(overrides, dict), f"{where}: 'worker_overrides' must be an object")
    by_id = {w.get("id"): w for w in workers if isinstance(w, dict)}
    for worker_id, override in overrides.items():
        ctx = f"{where} override for {worker_id!r}"
        _require(worker_id in by_id, f"{ctx}: unknown worker")
        _check_keys(override, ("remove_skills",), (), ctx)
        remove = override["remove_skills"]
        _require(isinstance(remove, list) and remove, f"{ctx}: 'remove_skills' must be a non-empty list")
        skills = by_id[worker_id].get("skills")
        _require(isinstance(skills, dict), f"{ctx}: worker has no skills object")
        for skill in remove:
            _require(skill in skills, f"{ctx}: worker does not have skill {skill!r} to remove")
            del skills[skill]
    return workers


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_config(config):
    """Check references, types, coverage, stock, and events. Raises ValueError.

    These are static checks. They do not prove that the orders can be
    finished before the deadline; only running the engine shows that.
    """
    try:
        _validate(config)
    except ValueError:
        raise
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError(f"malformed config: {exc!r}") from exc


def _validate(config):
    _check_keys(config, ("schema_version", "scenario", "variant", "description", "target", "menu",
                         "workers", "orders", "events", "expected_missing_skills"),
                ("source",), "config")
    _require(config["schema_version"] == SCHEMA_VERSION, "config: unsupported schema_version")

    target = config["target"]
    _check_keys(target, ("orders", "deadline", "description"), (), "target")
    _require(_is_int(target["orders"]) and target["orders"] > 0,
             "target.orders must be a positive integer")
    _require(_is_int(target["deadline"]) and target["deadline"] > 0,
             "target.deadline must be a positive integer number of minutes")
    _check_text(target["description"], "target.description")
    deadline = target["deadline"]

    menu = config["menu"]
    _check_keys(menu, ("ingredients", "stations", "recipes", "order_steps"), (), "menu")
    skills = _validate_menu(menu)
    workers = _validate_workers(config["workers"], skills)

    # Every skill a task can require must be held by at least one worker at the
    # start of the run (after overrides). Missing skills later, from events,
    # are declared in expected_missing_skills instead.
    held = {skill for worker in workers for skill in worker["skills"]}
    uncovered = sorted(skills - held)
    _require(not uncovered, f"no worker has required skill(s): {', '.join(uncovered)}")

    orders = config["orders"]
    _require(isinstance(orders, list) and orders, "orders must be a non-empty list")
    _require(len(orders) == target["orders"],
             f"target.orders is {target['orders']} but {len(orders)} orders are defined")
    expansions = {}
    for order in orders:
        _validate_order_fields(order, deadline)
        _require(order["id"] not in expansions, f"duplicate order id {order['id']!r}")
        expansions[order["id"]] = expand_order(order, menu)

    task_ids = [task["id"] for exp in expansions.values() for task in exp["tasks"]]
    duplicates = sorted(tid for tid, n in Counter(task_ids).items() if n > 1)
    _require(not duplicates, f"duplicate task ids: {', '.join(duplicates)}")

    demand = _demand(expansions.values())
    for ingredient_id, needed in sorted(demand.items()):
        stock = menu["ingredients"][ingredient_id]["stock"]
        _require(needed <= stock,
                 f"insufficient {ingredient_id}: orders need {needed}, stock is {stock}")

    _validate_events(config["events"], workers, deadline)

    expected = config["expected_missing_skills"]
    _require(isinstance(expected, list) and all(isinstance(s, str) for s in expected)
             and len(set(expected)) == len(expected),
             "expected_missing_skills must be a list of distinct skill names")
    actual = missing_skills_after_events(config)
    _require(set(expected) == set(actual),
             f"variant {config['variant']!r}: skills missing after events are "
             f"{actual or 'none'}, but expected_missing_skills says {sorted(expected) or 'none'}")


def _validate_menu(menu):
    """Validate the menu and return the set of skills its steps require."""
    ingredients = menu["ingredients"]
    _require(isinstance(ingredients, dict) and ingredients, "menu.ingredients must be a non-empty object")
    for ingredient_id, ingredient in ingredients.items():
        where = f"ingredient {ingredient_id!r}"
        _check_id(ingredient_id, where)
        _check_keys(ingredient, ("name", "unit", "stock", "location"), (), where)
        _check_text(ingredient["name"], f"{where}.name")
        _check_text(ingredient["unit"], f"{where}.unit")
        _check_text(ingredient["location"], f"{where}.location")
        _require(_is_int(ingredient["stock"]) and ingredient["stock"] >= 0,
                 f"{where}.stock must be a non-negative integer")

    stations = menu["stations"]
    _require(isinstance(stations, dict) and stations, "menu.stations must be a non-empty object")
    for station_id, station in stations.items():
        where = f"station {station_id!r}"
        _check_id(station_id, where)
        _check_keys(station, ("name", "type", "equipment", "capacity"), (), where)
        _check_text(station["name"], f"{where}.name")
        _check_text(station["type"], f"{where}.type")
        _require(isinstance(station["equipment"], list)
                 and all(isinstance(e, str) and e.strip() for e in station["equipment"]),
                 f"{where}.equipment must be a list of names")
        _require(_is_int(station["capacity"]) and station["capacity"] >= 1,
                 f"{where}.capacity must be a positive integer")

    skills = set()
    recipes = menu["recipes"]
    _require(isinstance(recipes, dict) and recipes, "menu.recipes must be a non-empty object")
    prefixes = {}
    for recipe_id, recipe in recipes.items():
        where = f"recipe {recipe_id!r}"
        _check_id(recipe_id, where)
        _check_keys(recipe, ("name", "item_prefix", "steps", "modifiers"), (), where)
        _check_text(recipe["name"], f"{where}.name")
        prefix = recipe["item_prefix"]
        _check_id(prefix, f"{where}.item_prefix", _PREFIX)
        _require(prefix not in prefixes,
                 f"{where}: item_prefix {prefix!r} already used by {prefixes.get(prefix)!r}")
        prefixes[prefix] = recipe_id
        steps = recipe["steps"]
        _require(isinstance(steps, list) and steps, f"{where}: needs at least one step")
        for step in steps:
            skills.add(_validate_step(step, menu, where))
        step_ids = [step["id"] for step in steps]
        _require(len(set(step_ids)) == len(step_ids), f"{where}: duplicate step ids")
        for step in steps:
            for dep in step["depends_on"]:
                _require(dep in step_ids, f"{where} step {step['id']!r}: unknown dependency {dep!r}")
        _check_acyclic(steps, where)
        _require(len(_final_steps(steps)) == 1, f"{where}: must have exactly one final step")
        _validate_modifiers(recipe, menu, where)
        # Each modifier on its own must leave a usable chain (also run at expansion).
        _plan_item(recipe, [], where)
        for modifier in recipe["modifiers"]:
            _plan_item(recipe, [modifier], where)

    order_steps = menu["order_steps"]
    where = "menu.order_steps"
    _require(isinstance(order_steps, list) and order_steps, f"{where}: needs at least one step")
    for step in order_steps:
        skills.add(_validate_step(step, menu, where, order_level=True))
    step_ids = [step["id"] for step in order_steps]
    _require(len(set(step_ids)) == len(step_ids), f"{where}: duplicate step ids")
    for step in order_steps:
        for dep in step["depends_on"]:
            _require(dep in step_ids, f"{where} step {step['id']!r}: unknown dependency {dep!r}")
    _check_acyclic(order_steps, where)
    _require(any(step.get("after_items") for step in order_steps),
             f"{where}: one step must set after_items so it waits for every item")
    # The engine runs order verification when the 'verify' task completes, so
    # 'verify' must exist and be the last thing that happens to an order.
    _require(_final_steps(order_steps) == ["verify"],
             f"{where}: 'verify' must be the single final order step")
    return skills


def _validate_step(step, menu, where, order_level=False):
    optional = ("after_items",) if order_level else ()
    _require(isinstance(step, dict), f"{where}: each step must be an object")
    where = f"{where} step {step.get('id')!r}"
    _check_keys(step, ("id", "name", "skill", "station", "minutes", "ingredients", "depends_on"),
                optional, where)
    _check_id(step["id"], where)
    _check_text(step["name"], f"{where}.name")
    _check_id(step["skill"], f"{where}.skill")
    _require(step["station"] in menu["stations"], f"{where}: unknown station {step['station']!r}")
    _require(_is_int(step["minutes"]) and step["minutes"] > 0,
             f"{where}.minutes must be a positive integer")
    _check_amounts(step["ingredients"], menu, f"{where}.ingredients", allow_zero=False)
    _require(isinstance(step["depends_on"], list)
             and all(isinstance(d, str) for d in step["depends_on"])
             and len(set(step["depends_on"])) == len(step["depends_on"]),
             f"{where}.depends_on must be a list of distinct step ids")
    if order_level:
        _require(type(step.get("after_items", False)) is bool, f"{where}.after_items must be true/false")
    return step["skill"]


def _check_amounts(amounts, menu, where, allow_zero):
    _require(isinstance(amounts, dict), f"{where}: expected an object of ingredient amounts")
    for ingredient_id, amount in amounts.items():
        _require(ingredient_id in menu["ingredients"], f"{where}: unknown ingredient {ingredient_id!r}")
        low = 0 if allow_zero else 1
        _require(_is_int(amount) and amount >= low,
                 f"{where}.{ingredient_id} must be an integer >= {low}, got {amount!r}")


def _validate_modifiers(recipe, menu, where):
    modifiers = recipe["modifiers"]
    _require(isinstance(modifiers, dict), f"{where}.modifiers must be an object")
    step_ids = {step["id"] for step in recipe["steps"]}
    for name, rule in modifiers.items():
        ctx = f"{where} modifier {name!r}"
        _check_id(name, ctx)
        _check_keys(rule, ("label",), ("skip_steps", "set_ingredients"), ctx)
        _check_text(rule["label"], f"{ctx}.label")
        _require("skip_steps" in rule or "set_ingredients" in rule,
                 f"{ctx}: must skip steps or set ingredient amounts")
        _require(isinstance(rule.get("skip_steps", []), list), f"{ctx}.skip_steps must be a list")
        for step_id in rule.get("skip_steps", []):
            _require(step_id in step_ids, f"{ctx}: cannot skip unknown step {step_id!r}")
        changes = rule.get("set_ingredients", {})
        _require(isinstance(changes, dict), f"{ctx}.set_ingredients must be an object")
        for step_id, amounts in changes.items():
            _require(step_id in step_ids, f"{ctx}: unknown step {step_id!r}")
            _check_amounts(amounts, menu, f"{ctx}.set_ingredients.{step_id}", allow_zero=True)
            _require(amounts, f"{ctx}.set_ingredients.{step_id}: no amounts given")


def _check_acyclic(steps, where):
    deps = {step["id"]: step["depends_on"] for step in steps}
    done, active = set(), set()

    def visit(step_id):
        _require(step_id not in active, f"{where}: dependency cycle through {step_id!r}")
        if step_id in done:
            return
        active.add(step_id)
        for dep in deps[step_id]:
            visit(dep)
        active.discard(step_id)
        done.add(step_id)

    for step_id in deps:
        visit(step_id)


def _final_steps(steps):
    """Ids of steps nothing else depends on, in definition order."""
    used = {dep for step in steps for dep in step["depends_on"]}
    return [step["id"] for step in steps if step["id"] not in used]


def _validate_workers(workers, skills):
    _require(isinstance(workers, list) and workers, "workers must be a non-empty list")
    seen = set()
    for worker in workers:
        _require(isinstance(worker, dict), "each worker must be an object")
        where = f"worker {worker.get('id')!r}"
        _check_keys(worker, ("id", "name", "role", "description", "skills"), (), where)
        _check_id(worker["id"], where)
        _require(worker["id"] not in seen, f"duplicate worker id {worker['id']!r}")
        seen.add(worker["id"])
        for field in ("name", "role", "description"):
            _check_text(worker[field], f"{where}.{field}")
        worker_skills = worker["skills"]
        _require(isinstance(worker_skills, dict) and worker_skills,
                 f"{where}: needs at least one skill")
        for skill, multiplier in worker_skills.items():
            _require(skill in skills,
                     f"{where}: skill {skill!r} is not required by any menu step (typo?)")
            _require(_is_number(multiplier) and 0 < multiplier <= MAX_MULTIPLIER,
                     f"{where}: multiplier for {skill!r} must be in (0, {MAX_MULTIPLIER}], "
                     f"got {multiplier!r}")
    return workers


def _validate_order_fields(order, deadline):
    _require(isinstance(order, dict), "each order must be an object")
    where = f"order {order.get('id')!r}"
    _check_keys(order, ("id", "arrival", "deadline", "items"), (), where)
    _check_id(order["id"], where, _ORDER_ID)
    _require(_is_int(order["arrival"]) and order["arrival"] >= 0,
             f"{where}.arrival must be a non-negative integer minute")
    _require(_is_int(order["deadline"]) and order["arrival"] < order["deadline"] <= deadline,
             f"{where}.deadline must be an integer after arrival and no later than {deadline}")
    _require(isinstance(order["items"], list) and order["items"], f"{where}: needs at least one item")


def _validate_events(events, workers, deadline):
    _require(isinstance(events, list), "events must be a list")
    unavailable = set()
    last_tick = 0
    worker_ids = {worker["id"] for worker in workers}
    for index, event in enumerate(events):
        where = f"event #{index + 1}"
        _check_keys(event, ("tick", "type", "worker_id"), (), where)
        _require(event["type"] in EVENT_TYPES,
                 f"{where}: unsupported type {event['type']!r} (supported: {', '.join(EVENT_TYPES)})")
        tick = event["tick"]
        _require(_is_int(tick) and 0 <= tick < deadline,
                 f"{where}.tick must be an integer minute in [0, {deadline})")
        _require(tick >= last_tick, f"{where}: events must be listed in tick order")
        last_tick = tick
        _require(event["worker_id"] in worker_ids, f"{where}: unknown worker {event['worker_id']!r}")
        _require(event["worker_id"] not in unavailable,
                 f"{where}: worker {event['worker_id']!r} is already unavailable")
        unavailable.add(event["worker_id"])


def _validate_order_items(order, menu):
    where = f"order {order.get('id')!r}"
    for index, line in enumerate(order["items"]):
        ctx = f"{where} item #{index + 1}"
        _check_keys(line, ("recipe", "quantity", "modifiers"), (), ctx)
        _require(line["recipe"] in menu["recipes"], f"{ctx}: unknown recipe {line['recipe']!r}")
        _require(_is_int(line["quantity"]) and line["quantity"] > 0,
                 f"{ctx}.quantity must be a positive integer")


# --------------------------------------------------------------------------
# Task generation
# --------------------------------------------------------------------------

def _plan_item(recipe, modifiers, where):
    """Apply modifiers to one recipe and return its effective steps.

    Returns a list of {"step": <menu step>, "ingredients": {...}, "depends_on": [...]}
    in recipe order. Skipped steps are removed and anything that depended on
    them inherits their prerequisites (e.g. no_lettuce: assemble -> patty).
    """
    _require(isinstance(modifiers, list) and all(isinstance(m, str) for m in modifiers),
             f"{where}: modifiers must be a list of names")
    repeated = sorted(m for m, n in Counter(modifiers).items() if n > 1)
    _require(not repeated, f"{where}: repeated modifier(s) {', '.join(repeated)}")
    rules = recipe["modifiers"]
    for name in modifiers:
        _require(name in rules,
                 f"{where}: unknown modifier {name!r} for {recipe['name']} "
                 f"(allowed: {', '.join(rules) or 'none'})")

    skipped = {}  # step id -> modifier that skips it
    claims = {}   # (step id, ingredient) -> (modifier, amount)
    for name in modifiers:
        for step_id in rules[name].get("skip_steps", []):
            skipped.setdefault(step_id, name)
        for step_id, amounts in rules[name].get("set_ingredients", {}).items():
            for ingredient_id, amount in amounts.items():
                key = (step_id, ingredient_id)
                if key in claims:
                    raise ValueError(f"{where}: contradictory modifiers {claims[key][0]!r} and "
                                     f"{name!r} both set {ingredient_id} in step {step_id!r}")
                claims[key] = (name, amount)
    for (step_id, ingredient_id), (name, _) in claims.items():
        if step_id in skipped:
            raise ValueError(f"{where}: contradictory modifiers {skipped[step_id]!r} skips step "
                             f"{step_id!r} but {name!r} changes its {ingredient_id}")

    by_id = {step["id"]: step for step in recipe["steps"]}

    def resolve(dep_ids):
        result = []
        for dep in dep_ids:
            for real in (resolve(by_id[dep]["depends_on"]) if dep in skipped else [dep]):
                if real not in result:
                    result.append(real)
        return result

    plan = []
    for step in recipe["steps"]:
        if step["id"] in skipped:
            continue
        amounts = dict(step["ingredients"])
        for (step_id, ingredient_id), (_, amount) in claims.items():
            if step_id == step["id"]:
                amounts[ingredient_id] = amount
        amounts = {key: value for key, value in amounts.items() if value > 0}
        plan.append({"step": step, "ingredients": amounts, "depends_on": resolve(step["depends_on"])})

    _require(plan, f"{where}: modifiers {modifiers} skip every step")
    finals = _final_steps([{"id": p["step"]["id"], "depends_on": p["depends_on"]} for p in plan])
    _require(len(finals) == 1,
             f"{where}: modifiers {modifiers} leave {len(finals)} final steps; exactly one is needed")
    return plan


def expand_order(order, menu):
    """Expand one order into its task graph. Pure: inputs are not modified.

    Each unit of each line gets its own chain ("O2.b1.patty", "O2.b2.patty", ...);
    units of the same recipe are numbered across lines within the order.
    The order-level steps (pack, verify) appear once per order; a step with
    after_items depends on every item's final task.

    Returns {"tasks": [...], "expected_items": [...]}. expected_items lists
    what verification should find for each unit: its recipe, sorted
    modifiers, the task ids that must be done, and the total raw ingredients
    that unit consumes.
    """
    _require(isinstance(order, dict) and isinstance(order.get("id"), str),
             "order must be an object with an id")
    where = f"order {order['id']!r}"
    _check_id(order["id"], where, _ORDER_ID)
    _require(isinstance(order.get("items"), list) and order["items"], f"{where}: needs at least one item")
    _validate_order_items(order, menu)
    order_id = order["id"]
    tasks, expected_items, final_task_ids = [], [], []
    unit_numbers = Counter()

    for index, line in enumerate(order["items"]):
        recipe = menu["recipes"][line["recipe"]]
        plan = _plan_item(recipe, line["modifiers"], f"{where} item #{index + 1}")
        final_step = _final_steps([{"id": p["step"]["id"], "depends_on": p["depends_on"]} for p in plan])[0]
        for _ in range(line["quantity"]):
            unit_numbers[recipe["item_prefix"]] += 1
            item_id = f"{order_id}.{recipe['item_prefix']}{unit_numbers[recipe['item_prefix']]}"
            item_tasks = []
            totals = Counter()
            for entry in plan:
                step = entry["step"]
                task = _make_task(
                    task_id=f"{item_id}.{step['id']}", order_id=order_id, item_id=item_id, step=step,
                    ingredients=entry["ingredients"],
                    depends_on=[f"{item_id}.{dep}" for dep in entry["depends_on"]],
                )
                item_tasks.append(task)
                totals.update(entry["ingredients"])
            tasks.extend(item_tasks)
            final_id = f"{item_id}.{final_step}"
            final_task_ids.append(final_id)
            expected_items.append({
                "item_id": item_id,
                "order_id": order_id,
                "recipe_id": line["recipe"],
                "modifiers": sorted(line["modifiers"]),
                "task_ids": [task["id"] for task in item_tasks],
                "final_task_id": final_id,
                "ingredients": dict(sorted(totals.items())),
            })

    for step in menu["order_steps"]:
        depends_on = list(final_task_ids) if step.get("after_items") else []
        depends_on += [f"{order_id}.{dep}" for dep in step["depends_on"]]
        tasks.append(_make_task(
            task_id=f"{order_id}.{step['id']}", order_id=order_id, item_id=None, step=step,
            ingredients=dict(step["ingredients"]), depends_on=depends_on,
        ))
    return {"tasks": tasks, "expected_items": expected_items}


def _make_task(task_id, order_id, item_id, step, ingredients, depends_on):
    return {
        "id": task_id,
        "order_id": order_id,
        "item_id": item_id,
        "step_id": step["id"],
        "required_skill": step["skill"],
        "station_id": step["station"],
        "base_minutes": step["minutes"],
        "ingredients": dict(ingredients),
        "depends_on": list(depends_on),
        "status": "pending",
    }


# --------------------------------------------------------------------------
# Derived facts (pure; useful to the CLI, tests, and the other agents)
# --------------------------------------------------------------------------

def expand_all(config):
    """{order_id: expand_order(order, menu)} for every order, in config order."""
    return {order["id"]: expand_order(order, config["menu"]) for order in config["orders"]}


def _demand(expansions):
    demand = Counter()
    for expansion in expansions:
        for task in expansion["tasks"]:
            demand.update(task["ingredients"])
    return demand


def ingredient_demand(config):
    """Total raw ingredients consumed if every order is completed once."""
    return dict(sorted(_demand(expand_all(config).values()).items()))


def missing_skills_after_events(config):
    """Skills some task needs that no worker has once every event has applied."""
    gone = {event["worker_id"] for event in config["events"] if event["type"] == "worker_unavailable"}
    held = {skill for worker in config["workers"] if worker["id"] not in gone
            for skill in worker["skills"]}
    needed = {task["required_skill"] for exp in expand_all(config).values() for task in exp["tasks"]}
    return sorted(needed - held)


def skill_work_lower_bound(config, skill):
    """Earliest minute by which all tasks needing `skill` could possibly be done.

    A lower bound for any schedule: every worker who holds the skill (after
    overrides) works only on it at their best speed, orders arrive on time,
    and prerequisites take their fastest possible duration. If this is later
    than a disruption tick, unfinished work for that skill is guaranteed to
    exist at that tick. Returns None if no task needs the skill.
    """
    fastest = {}
    for worker in config["workers"]:
        for name, multiplier in worker["skills"].items():
            fastest[name] = min(fastest.get(name, multiplier), multiplier)
    arrivals = {order["id"]: order["arrival"] for order in config["orders"]}
    tasks = {task["id"]: task for exp in expand_all(config).values() for task in exp["tasks"]}

    def minutes(task):
        return effective_minutes(task["base_minutes"], fastest[task["required_skill"]])

    finish = {}

    def earliest_finish(task_id):
        if task_id not in finish:
            task = tasks[task_id]
            start = max([arrivals[task["order_id"]]] + [earliest_finish(d) for d in task["depends_on"]])
            finish[task_id] = start + minutes(task)
        return finish[task_id]

    relevant = [task for task in tasks.values() if task["required_skill"] == skill]
    if not relevant:
        return None
    holders = sum(1 for worker in config["workers"] if skill in worker["skills"])
    slots = sum(config["menu"]["stations"][s]["capacity"] for s in {t["station_id"] for t in relevant})
    lanes = max(1, min(holders, slots))
    first_start = min(earliest_finish(t["id"]) - minutes(t) for t in relevant)
    total = sum(minutes(t) for t in relevant)
    return max(max(earliest_finish(t["id"]) for t in relevant),
               first_start + math.ceil(total / lanes))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _variant_names(config_dir):
    path = Path(config_dir) / "scenario.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return list(doc["variants"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"cannot list variants from {path}: {exc}") from exc


def _summary(config):
    lines = [f"[{config['variant']}] {config['description']}"]
    for worker in config["workers"]:
        skills = ", ".join(f"{s} x{m}" for s, m in worker["skills"].items())
        lines.append(f"  worker {worker['id']:<11} {worker['name']:<7} {skills}")
    counts = Counter()
    for exp in expand_all(config).values():
        for item in exp["expected_items"]:
            counts[item["recipe_id"]] += 1
    lines.append(f"  {len(config['orders'])} orders, items: "
                 + ", ".join(f"{r}={n}" for r, n in sorted(counts.items())))
    stock = config["menu"]["ingredients"]
    lines.append("  demand/stock: " + ", ".join(
        f"{i} {n}/{stock[i]['stock']}" for i, n in ingredient_demand(config).items()))
    for event in config["events"]:
        worker = next(w for w in config["workers"] if w["id"] == event["worker_id"])
        bounds = ", ".join(f"{s} >= {skill_work_lower_bound(config, s)}" for s in worker["skills"])
        lines.append(f"  event t={event['tick']}: {event['type']} {event['worker_id']} "
                     f"(all-work-done lower bound: {bounds})")
    lines.append(f"  missing skills after events: {', '.join(missing_skills_after_events(config)) or 'none'}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate scenario config and show expanded tasks.")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--variant", help="variant to load (default: validate every variant)")
    parser.add_argument("--order", help="print this order's expanded tasks as JSON")
    args = parser.parse_args(argv)
    try:
        names = [args.variant] if args.variant else _variant_names(args.config_dir)
        configs = [load_config(args.config_dir, name) for name in names]
        if args.order:
            config = configs[0]
            order = next((o for o in config["orders"] if o["id"] == args.order), None)
            if order is None:
                raise ValueError(f"unknown order {args.order!r}")
            print(json.dumps({"order": order, **expand_order(order, config["menu"])}, indent=2))
            return 0
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    for config in configs:
        print(_summary(config))
    print("Static checks passed for: " + ", ".join(names)
          + ". Deadline completion is only shown by running the engine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
