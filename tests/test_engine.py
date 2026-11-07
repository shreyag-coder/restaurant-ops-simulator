"""Agent 1 acceptance checks; helpers here stand in for unmerged agents."""

import copy
from collections import Counter

import pytest

from orchestrator import SimulationEngine, create_state


def tiny_config(order_count=1, event_at=None, backup=True, deadline=30):
    skills = {"grill": 1.5, "prep": 1, "assemble": 1, "fry": 1, "pack": 1, "verify": 1}
    if not backup:
        skills.pop("grill")
    return {
        "menu": {
            "ingredients": {key: {"available": 100, "unit": "piece"} for key in ("patty", "lettuce", "cheese", "potato", "bag")},
            "stations": {key: {"capacity": 1} for key in ("grill", "prep", "assembly", "fryer", "packing", "pickup")},
        },
        "workers": {
            "A": {"name": "Primary", "skills": {"grill": 1}},
            "B": {"name": "Backup", "skills": skills},
        },
        "scenario": {
            "target": {"completed_orders": order_count}, "deadline_minutes": deadline,
            "orders": [{"id": "O" + str(i), "arrival_minutes": 0, "deadline_minutes": deadline,
                        "items": [{"recipe_id": "burger", "quantity": 1, "modifiers": []},
                                  {"recipe_id": "fries", "quantity": 1, "modifiers": []}]}
                       for i in range(1, order_count + 1)],
            "events": [] if event_at is None else [{"at_minutes": event_at, "type": "set_worker_available", "worker_id": "A", "available": False}],
        },
    }


def expand_example(order, menu):
    """Test-only task graph; production generation belongs to Agent 2."""
    tasks, expected, terminal = [], [], []
    for line, item in enumerate(order["items"]):
        for unit in range(item["quantity"]):
            item_id = "{}.{}.{}".format(order["id"], line, unit)
            expected.append({"id": item_id, "recipe_id": item["recipe_id"], "modifiers": item.get("modifiers", [])[:]})
            if item["recipe_id"] == "burger":
                steps = [("patty", "grill", "grill", 2, {"patty": 1})]
                if "no_lettuce" not in item.get("modifiers", []):
                    steps.append(("toppings", "prep", "prep", 1, {"lettuce": 1}))
                cheese = 0 if "no_cheese" in item.get("modifiers", []) else (2 if "extra_cheese" in item.get("modifiers", []) else 1)
                steps.append(("assemble", "assemble", "assembly", 1, {"cheese": cheese} if cheese else {}))
            else:
                steps = [("fry", "fry", "fryer", 2, {"potato": 1})]
            dependencies = []
            for step_id, skill, station, duration, ingredients in steps:
                task_id = item_id + "." + step_id
                tasks.append({"id": task_id, "order_id": order["id"], "item_id": item_id,
                              "step_id": step_id, "required_skill": skill, "station_id": station,
                              "base_minutes": duration, "ingredients": ingredients,
                              "depends_on": dependencies, "status": "pending"})
                dependencies = [task_id]
            terminal.extend(dependencies)
    for step_id, station, dependencies, ingredients in [
        ("pack", "packing", terminal, {"bag": 1}),
        ("verify", "pickup", [order["id"] + ".pack"], {}),
    ]:
        tasks.append({"id": order["id"] + "." + step_id, "order_id": order["id"],
                      "item_id": None, "step_id": step_id, "required_skill": step_id,
                      "station_id": station, "base_minutes": 1, "ingredients": ingredients,
                      "depends_on": dependencies, "status": "pending"})
    return {"tasks": tasks, "expected_items": expected}


def example_view(state, worker_id):
    """Test-only view; production views and policy belong to Agent 3."""
    worker = state["workers"][worker_id]
    ready = []
    for task in state["tasks"].values():
        if (task["status"] != "pending" or task["required_skill"] not in worker["skills"]
                or any(state["tasks"][key]["status"] != "done" for key in task["depends_on"])):
            continue
        station = state["stations"][task["station_id"]]
        ready.append({"id": task["id"], "order_deadline": state["orders"][task["order_id"]]["deadline_minutes"],
                      "station_has_room": station["available"] and len(station["active_tasks"]) < station["capacity"],
                      "ingredients_ok": all(state["ingredients"][key]["available"] >= n for key, n in task["ingredients"].items())})
    return {"time": state["time"], "state_version": state["state_version"], "worker_id": worker_id,
            "ready_tasks": sorted(ready, key=lambda t: (t["order_deadline"], t["id"]))}


def example_policy(view, worker_id):
    ready = [task for task in view["ready_tasks"] if task["station_has_room"] and task["ingredients_ok"]]
    return {"proposal": {"action": "start_task" if ready else "wait", "task_id": ready[0]["id"] if ready else None,
                         "why": "Explicit test policy"}, "mode": "det", "model": None, "latency_ms": 0, "attempts": 1}


def make_engine(config=None, policy=example_policy, expander=expand_example):
    return SimulationEngine(create_state(config or tiny_config(), "det", run_id="test-run"), expander, example_view, policy)


def admitted(config=None):
    engine = make_engine(config)
    engine._admit_orders()
    return engine


def start(engine, worker="A", task="O1.0.0.patty"):
    return engine.apply_action(worker, {"action": "start_task", "task_id": task, "why": "Test"})


def resources(engine):
    state = engine.snapshot()
    return {key: state[key] for key in ("ingredients", "stations", "tasks", "produced_items")}, {
        key: (w["available"], w["current_task"]) for key, w in state["workers"].items()}


def test_one_real_order_completes_with_inventory_and_no_early_tasks():
    engine = make_engine()
    result = engine.run()
    assert result["status"] == "completed"
    assert result["orders"]["O1"]["correct"] is True
    assert result["orders"]["O1"]["on_time"] is True
    assert len(result["produced_items"]) == 2
    assert all(task["completed_at"] == task["finishes_at"] > task["started_at"] for task in result["tasks"].values())
    assert all(value["available"] == 99 and value["reserved"] == 0 for value in result["ingredients"].values())
    assert all(not station["active_tasks"] for station in result["stations"].values())
    assert all(w["current_task"] is None for w in result["workers"].values())


def test_conflicting_claim_reserves_once():
    engine = admitted()
    assert start(engine)["accepted"]
    before = resources(engine)
    result = start(engine, worker="B")
    assert not result["accepted"] and "pending" in result["reason"]
    assert resources(engine) == before
    assert engine.state["ingredients"]["patty"] == {"id": "patty", "unit": "piece", "available": 99, "reserved": 1}


@pytest.mark.parametrize("fault,reason", [
    ("skill", "skill"), ("stock", "ingredients"), ("prerequisite", "Prerequisites"),
    ("station", "Station"), ("availability", "unavailable"), ("busy", "busy"),
])
def test_invalid_start_has_no_partial_reservations(fault, reason):
    engine = admitted()
    worker, task = "A", "O1.0.0.patty"
    if fault == "skill":
        engine.state["workers"]["A"]["skills"] = {"prep": 1}
    elif fault == "stock":
        engine.state["ingredients"]["patty"]["available"] = 0
    elif fault == "prerequisite":
        worker, task = "B", "O1.0.0.assemble"
    elif fault == "station":
        engine.state["stations"]["grill"]["available"] = False
    elif fault == "availability":
        engine.state["workers"]["A"]["available"] = False
    elif fault == "busy":
        assert start(engine)["accepted"]
    before = resources(engine)
    result = start(engine, worker, task)
    assert not result["accepted"] and reason in result["reason"]
    assert resources(engine) == before


def test_full_station_rejects_independent_task():
    engine = admitted(tiny_config(order_count=2))
    start(engine)
    before = resources(engine)
    result = start(engine, "B", "O2.0.0.patty")
    assert not result["accepted"] and "full" in result["reason"]
    assert resources(engine) == before


@pytest.mark.parametrize("proposal", [None, {}, {"action": [], "task_id": None, "why": "Test"},
    {"action": "wait", "task_id": "O1.pack", "why": "Test"},
    {"action": "start_task", "task_id": None, "why": "Test"},
    {"action": "start_task", "task_id": "O1.0.0.patty", "why": "Test", "worker_id": "B"}])
def test_bad_action_schema_rejected_without_resource_changes(proposal):
    engine = admitted()
    before = resources(engine)
    assert not engine.apply_action("A", proposal)["accepted"]
    assert resources(engine) == before


def test_timing_and_completion_idempotence():
    engine = admitted()
    start(engine)
    engine.state["time"] = 1
    engine._complete_tasks()
    assert engine.state["tasks"]["O1.0.0.patty"]["status"] == "active"
    engine.state["time"] = 2
    engine._complete_tasks()
    before = engine.snapshot()
    engine._complete_tasks()
    assert engine.snapshot() == before
    assert before["ingredients"]["patty"]["available"] == 99
    assert before["ingredients"]["patty"]["reserved"] == 0


def test_duration_rounding_has_no_binary_float_extra_tick():
    engine = admitted()
    engine.state["tasks"]["O1.0.0.patty"]["base_minutes"] = 10
    engine.state["workers"]["A"]["skills"]["grill"] = 1.1
    assert start(engine)["accepted"]
    assert engine.state["tasks"]["O1.0.0.patty"]["finishes_at"] == 11


def test_interrupt_restores_resources_and_backup_restarts_full_rounded_duration():
    engine = admitted()
    start(engine)
    engine.state["time"] = 1
    engine.set_worker_available("A", False)
    assert engine.state["ingredients"]["patty"]["available"] == 100
    assert engine.state["ingredients"]["patty"]["reserved"] == 0
    assert start(engine, "B")["accepted"]
    task = engine.state["tasks"]["O1.0.0.patty"]
    assert (task["started_at"], task["finishes_at"]) == (1, 4)
    engine.state["time"] = 4
    engine._complete_tasks()
    assert engine.state["ingredients"]["patty"]["available"] == 99


def test_disruption_precedes_same_tick_completion():
    engine = make_engine(tiny_config(event_at=2))
    engine.run(max_ticks=3)
    task = engine.state["tasks"]["O1.0.0.patty"]
    assert task["worker_id"] == "B" and task["started_at"] == 2 and task["finishes_at"] == 5
    assert any(e["type"] == "task_interrupted" for e in engine.pending_events)
    assert not any(e["type"] == "task_completed" and e.get("task_id") == task["id"] for e in engine.pending_events)


def test_completed_prerequisites_survive_interruption():
    engine = admitted()
    start(engine)
    engine.state["time"] = 2
    engine._complete_tasks()
    assert start(engine, "B", "O1.0.0.toppings")["accepted"]
    engine.set_worker_available("B", False)
    assert engine.state["tasks"]["O1.0.0.patty"]["status"] == "done"
    assert engine.state["ingredients"]["patty"]["available"] == 99
    assert engine.state["ingredients"]["lettuce"]["available"] == 100


def test_blocker_deduplicated_and_never_invents_qualification():
    engine = make_engine(tiny_config(event_at=1, backup=False))
    state = engine.run()
    assert state["status"] == "timed_out"
    blockers = [e for e in engine.pending_events if e["type"] == "blocker"]
    assert len(blockers) == 1 and blockers[0]["task_id"] == "O1.0.0.patty"
    assert not any(e["type"] == "task_started" and e.get("task_id") == "O1.0.0.patty" and e["tick"] >= 1 for e in engine.pending_events)
    assert state["orders"]["O1"]["correct"] is False


def test_modifiers_quantities_packing_and_production_verified():
    config = tiny_config()
    item = config["scenario"]["orders"][0]["items"][0]
    item.update(quantity=2, modifiers=["no_cheese", "no_lettuce"])
    engine = make_engine(config)
    result = engine.run()
    assert result["status"] == "completed"
    assert len(result["produced_items"]) == 3
    assert result["ingredients"]["cheese"]["available"] == 100
    assert result["ingredients"]["lettuce"]["available"] == 100
    assert result["ingredients"]["patty"]["available"] == 98
    assert result["ingredients"]["bag"]["available"] == 99


@pytest.mark.parametrize("damage", ["quantity", "modifier", "packing", "prerequisite"])
def test_verification_fails_on_incorrect_output(damage):
    engine = make_engine()
    engine.run()
    if damage == "quantity":
        engine.state["produced_items"].pop("O1.1.0")
    elif damage == "modifier":
        engine.state["produced_items"]["O1.0.0"]["modifiers"] = ["no_cheese"]
    elif damage == "packing":
        engine.state["orders"]["O1"]["packed"] = False
    else:
        engine.state["tasks"]["O1.0.0.patty"]["status"] = "pending"
    engine._verify(engine.state["orders"]["O1"])
    assert engine.state["orders"]["O1"]["correct"] is False


def test_completion_at_deadline_counts_and_timeliness_is_separate():
    reference = make_engine().run()
    finish = reference["time"]
    engine = make_engine(tiny_config(deadline=finish))
    assert engine.run()["status"] == "completed"
    config = tiny_config()
    config["scenario"]["orders"][0]["deadline_minutes"] = 1
    result = make_engine(config).run()
    assert result["status"] == "completed"
    assert result["orders"]["O1"]["correct"] is True
    assert result["orders"]["O1"]["on_time"] is False


def test_no_start_at_deadline_and_future_orders_admitted_once():
    config = tiny_config(deadline=3)
    config["scenario"]["orders"][0]["arrival_minutes"] = 2
    engine = make_engine(config)
    result = engine.run()
    assert result["status"] == "timed_out"
    assert len([e for e in engine.pending_events if e["type"] == "order_admitted"]) == 1
    assert all(e["tick"] < 3 for e in engine.pending_events if e["type"] == "task_started")


def test_same_snapshot_first_round_conflict_reasked_once():
    seen = []

    def policy(view, worker_id):
        seen.append(copy.deepcopy(view))
        return example_policy(view, worker_id)

    engine = make_engine(policy=policy)
    engine.run(max_ticks=1)
    first_round = [e for e in engine.pending_events if e["type"] == "decision" and e["payload"]["round"] == 0]
    retry = [e for e in engine.pending_events if e["type"] == "decision" and e["payload"]["round"] == 1]
    assert len(first_round) == 2 and len(retry) == 1
    assert first_round[0]["payload"]["observed_state_version"] == first_round[1]["payload"]["observed_state_version"]
    assert retry[0]["payload"]["result"]["accepted"]
    assert seen[0]["state_version"] == seen[1]["state_version"] < seen[2]["state_version"]


def test_rejected_worker_gets_no_third_round():
    def always_wrong(view, worker_id):
        envelope = example_policy(view, worker_id)
        envelope["proposal"]["task_id"] = "does-not-exist"
        envelope["proposal"]["action"] = "start_task"
        return envelope

    engine = make_engine(policy=always_wrong)
    engine.run(max_ticks=1)
    decisions = [e for e in engine.pending_events if e["type"] == "decision"]
    assert Counter(e["worker_id"] for e in decisions) == {"A": 2, "B": 2}


def test_decision_error_pause_resumes_saved_collection_without_repeating_world_or_calls():
    calls = []
    fail = True

    def policy(view, worker_id):
        nonlocal fail
        calls.append((view["time"], worker_id))
        if worker_id == "B" and fail:
            fail = False
            raise TimeoutError("secret-key-do-not-save")
        return example_policy(view, worker_id)

    engine = make_engine(policy=policy)
    paused = engine.run()
    assert paused["status"] == "paused" and paused["next_tick"] == 0
    assert paused["continuation"]["phase"] == "choose" and paused["continuation"]["cursor"] == 1
    assert not any(t["status"] == "active" for t in paused["tasks"].values())
    assert "secret-key" not in str(paused) + str(engine.pending_events)
    resumed = SimulationEngine(paused, expand_example, example_view, policy)
    assert resumed.run()["status"] == "completed"
    assert calls.count((0, "A")) == 1
    assert calls.count((0, "B")) == 3  # failed collection, resumed collection, conflict re-ask
    assert not any(e["type"] == "order_admitted" for e in resumed.pending_events)


def test_mid_second_round_error_does_not_repeat_accepted_action():
    calls = 0

    def policy(view, worker_id):
        nonlocal calls
        if worker_id == "B":
            calls += 1
            if calls == 2:
                raise RuntimeError("test provider failure")
        return example_policy(view, worker_id)

    engine = make_engine(policy=policy)
    paused = engine.run()
    assert paused["status"] == "paused" and paused["continuation"]["round"] == 1
    assert paused["workers"]["A"]["current_task"] == "O1.0.0.patty"
    resumed = SimulationEngine(paused, expand_example, example_view, policy)
    result = resumed.run()
    assert result["status"] == "completed"
    assert result["ingredients"]["patty"]["available"] == 99
    assert not any(e["type"] == "task_started" and e.get("task_id") == "O1.0.0.patty" for e in resumed.pending_events)


def test_pause_resume_matches_uninterrupted_with_same_events():
    full = make_engine(tiny_config(order_count=3, event_at=5))
    full_state = full.run()
    stepped = make_engine(tiny_config(order_count=3, event_at=5))
    all_events = []
    while True:
        state = stepped.run(max_ticks=1)
        all_events.extend(stepped.pending_events)
        if state["status"] != "paused":
            break
        stepped = SimulationEngine(state, expand_example, example_view, example_policy)
    assert state == full_state
    assert all_events == full.pending_events
    assert [e["seq"] for e in all_events] == list(range(1, len(all_events) + 1))


def test_repeatable_ten_order_scenario_and_input_purity():
    config = tiny_config(order_count=10, deadline=100)
    original = copy.deepcopy(config)
    first, second = make_engine(config), make_engine(config)
    assert first.run() == second.run()
    assert first.pending_events == second.pending_events
    assert first.state["status"] == "completed"
    assert sum(o["correct"] for o in first.state["orders"].values()) == 10
    assert config == original


@pytest.mark.parametrize("variant", ["baseline", "disruption", "blocker"])
def test_four_workers_ten_orders_thirty_minute_target_and_conservation(variant):
    config = tiny_config(order_count=10, event_at=None if variant == "baseline" else 5,
                         backup=variant != "blocker")
    config["workers"]["C"] = {"name": "Preparation", "skills": {"prep": 1, "assemble": 1}}
    config["workers"]["D"] = {"name": "Fryer", "skills": {"fry": 1, "pack": 1, "verify": 1}}
    engine = make_engine(config)
    result = engine.run()
    if variant == "baseline":
        assert result["status"] == "completed" and result["time"] <= 30
        assert sum(o["correct"] for o in result["orders"].values()) == 10
    else:
        interrupted = [e for e in engine.pending_events if e["type"] == "task_interrupted"]
        assert len(interrupted) == 1 and interrupted[0]["tick"] == 5
        if variant == "disruption":
            assert any(e["type"] == "task_started" and e.get("worker_id") == "B"
                       and e["task_id"] == interrupted[0]["task_id"] for e in engine.pending_events)
        else:
            assert result["status"] == "timed_out" and result["blockers"]
            assert all(e["tick"] < 5 for e in engine.pending_events
                       if e["type"] == "task_started" and e.get("task_id", "").endswith(".patty"))
    consumed = Counter()
    for task in result["tasks"].values():
        if task["status"] == "done":
            consumed.update(task["ingredients"])
    for key, stock in result["ingredients"].items():
        assert stock["available"] >= 0 and stock["reserved"] >= 0
        assert stock["available"] + stock["reserved"] + consumed[key] == 100
    assert all(len(station["active_tasks"]) <= station["capacity"] for station in result["stations"].values())


@pytest.mark.parametrize("envelope", [None, {}, {"mode": "ai", "proposal": {}},
    {"mode": "det", "proposal": {}, "latency_ms": float("nan")}])
def test_invalid_decision_envelope_pauses_instead_of_losing_checkpoint(envelope):
    engine = make_engine(policy=lambda view, worker: envelope)
    result = engine.run()
    assert result["status"] == "paused" and result["pause_reason"] == "decision_error"
    assert result["continuation"]["phase"] == "choose"
    assert not any(t["status"] == "active" for t in result["tasks"].values())


def test_provider_failure_attempt_metadata_recorded_without_raw_message():
    class DecisionError(Exception):
        attempts = 2
        latency_ms = 120

    def fail(view, worker_id):
        raise DecisionError("private input and credentials")
    engine = make_engine(policy=fail)
    engine.run()
    event = next(e for e in engine.pending_events if e["type"] == "decision_error")
    assert event["payload"]["attempts"] == 2
    assert event["payload"]["latency_ms"] == 120
    assert "credentials" not in str(engine.snapshot()) + str(engine.pending_events)


def test_policy_view_carries_frozen_model_and_accurate_state_version():
    def minimal_view(state, worker_id):
        return {"worker_id": "wrong", "state_version": -1, "model": "wrong"}

    state = create_state(tiny_config(), "ai", model="frozen/model")
    engine = SimulationEngine(state, expand_example, minimal_view, None)
    engine._admit_orders()
    observed = engine.state["state_version"]
    engine._start_round(["A"], 0)
    view = engine.state["continuation"]["views"]["A"]
    assert view["worker_id"] == "A" and view["state_version"] == observed
    assert view["model"] == "frozen/model" and view["mode"] == "ai"


def test_helpers_cannot_mutate_authoritative_inputs():
    def mutate_view(state, worker_id):
        view = example_view(state, worker_id)
        state["ingredients"]["patty"]["available"] = -100
        return view

    engine = make_engine()
    engine.build_worker_view = mutate_view
    assert engine.run()["ingredients"]["patty"]["available"] == 99


def test_bad_expander_is_rejected_before_order_tasks_are_installed():
    def broken(order, menu):
        expanded = expand_example(order, menu)
        expanded["expected_items"].pop()
        return expanded

    engine = make_engine(expander=broken)
    with pytest.raises(ValueError, match="quantities/modifiers"):
        engine.run()
    assert engine.state["tasks"] == {}


def test_available_agent2_scenario_integrates_without_mutating_inputs():
    scenario = pytest.importorskip("scenario", reason="Agent 2's production module not merged yet")
    config = scenario.load_config("sim/config", "baseline")
    original = copy.deepcopy(config)
    config["orders"] = config["orders"][:1]
    config["target"]["orders"] = 1
    engine = SimulationEngine(create_state(config, "det"), scenario.expand_order, example_view, example_policy)
    result = engine.run()
    assert result["status"] == "completed" and result["orders"]["O1"]["correct"]
    for ingredient_id, stock in result["ingredients"].items():
        consumed = sum(t["consumed_ingredients"].get(ingredient_id, 0) for t in result["tasks"].values())
        assert stock["available"] + stock["reserved"] + consumed == config["menu"]["ingredients"][ingredient_id]["stock"]
    assert scenario.load_config("sim/config", "baseline") == original


def test_verification_checks_actual_ingredient_receipts_against_item_plan():
    def receipt_plan(order, menu):
        expanded = expand_example(order, menu)
        for item in expanded["expected_items"]:
            used = Counter()
            for task in expanded["tasks"]:
                if task["item_id"] == item["id"]:
                    used.update(task["ingredients"])
            item["ingredients"] = dict(used)
        return expanded

    engine = make_engine(expander=receipt_plan)
    engine.run()
    assert engine.state["orders"]["O1"]["correct"]
    engine.state["produced_items"]["O1.0.0"]["ingredients"]["cheese"] = 0
    engine._verify(engine.state["orders"]["O1"])
    assert engine.state["orders"]["O1"]["correct"] is False


@pytest.mark.parametrize("max_ticks", [0, -1, True, 1.5])
def test_invalid_tick_limit(max_ticks):
    with pytest.raises(ValueError, match="positive"):
        make_engine().run(max_ticks=max_ticks)


def test_terminal_runs_cannot_resume():
    engine = make_engine()
    engine.run()
    with pytest.raises(ValueError, match="terminal"):
        engine.run()
