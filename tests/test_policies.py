import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import policies  # noqa: E402
from policies import (  # noqa: E402
    build_worker_view,
    choose_deterministic,
    load_persona,
    task_minutes,
    validate_proposal,
)

WORKER_IDS = ["w_expo", "w_fry", "w_grill", "w_prep"]


def _task(task_id, skill, station, base, deps=(), status="pending", ingredients=None):
    order_id, _, rest = task_id.partition(".")
    item_id, _, step_id = rest.rpartition(".")
    return {
        "id": task_id,
        "order_id": order_id,
        "item_id": item_id or None,
        "step_id": step_id,
        "required_skill": skill,
        "station_id": station,
        "base_minutes": base,
        "ingredients": ingredients or {},
        "depends_on": list(deps),
        "status": status,
        "worker": None,
    }


def make_state():
    tasks = [
        # O1: one burger, patty already done; toppings ready.
        _task("O1.b1.patty", "grill", "grill", 4, status="done", ingredients={"patty": 1}),
        _task("O1.b1.toppings", "prep", "prep", 2, ["O1.b1.patty"], ingredients={"lettuce": 1}),
        _task("O1.b1.assemble", "assemble", "assembly", 1, ["O1.b1.toppings"], ingredients={"bun": 1}),
        _task("O1.pack", "pack", "packing", 1, ["O1.b1.assemble"], ingredients={"bag": 1}),
        # O2: two burgers (no_lettuce on both -> no toppings step) + one fries.
        _task("O2.b1.patty", "grill", "grill", 4, ingredients={"patty": 1}),
        _task("O2.b2.patty", "grill", "grill", 4, ingredients={"patty": 1}),
        _task("O2.b1.assemble", "assemble", "assembly", 1, ["O2.b1.patty"], ingredients={"bun": 1}),
        _task("O2.f1.fry", "fry", "fryer", 3, ingredients={"fries": 1}),
    ]
    return {
        "time": 3,
        "deadline": 30,
        "status": "running",
        "workers": {
            "w_grill": {"skills": {"grill": 1.0, "prep": 1.2}, "available": True,
                        "status": "busy", "current_task": "O3.b1.patty", "last_result": None},
            "w_prep": {"skills": {"prep": 1.0, "assemble": 1.0, "grill": 1.5}, "available": True,
                       "status": "idle", "current_task": None,
                       "last_result": {"accepted": False, "reason": "station full"}},
            "w_fry": {"skills": {"fry": 1.0, "pack": 1.0}, "available": True,
                      "status": "idle", "current_task": None, "last_result": None},
            "w_expo": {"skills": {"assemble": 1.0, "pack": 1.0, "verify": 1.0}, "available": True,
                       "status": "idle", "current_task": None, "last_result": None},
        },
        "tasks": {t["id"]: t for t in tasks},
        "orders": {
            "O1": {"deadline": 20, "status": "in_progress"},
            "O2": {"deadline": 25, "status": "open"},
        },
        "stations": {
            "grill": {"capacity": 2, "active": ["O3.b1.patty"]},
            "prep": {"capacity": 1, "active": []},
            "assembly": {"capacity": 1, "active": []},
            "fryer": {"capacity": 1, "active": []},
            "packing": {"capacity": 1, "active": []},
        },
        "ingredients": {
            "patty": {"available": 5, "reserved": 1},
            "lettuce": {"available": 3, "reserved": 0},
            "bun": {"available": 5, "reserved": 0},
            "fries": {"available": 4, "reserved": 0},
            "bag": {"available": 2, "reserved": 0},
        },
    }


def ready_ids(view):
    return [t["task_id"] for t in view["ready_tasks"]]


# ---------------------------------------------------------------- durations

@pytest.mark.parametrize("base,mult,expected", [
    (10, 1.1, 11), (4, 1.5, 6), (3, 1.5, 5), (4, 1.0, 4), (2, 1.2, 3), (1, 0.5, 1),
])
def test_task_minutes_rounds_up_without_float_error(base, mult, expected):
    assert task_minutes(base, mult) == expected


def test_task_minutes_rejects_non_positive():
    with pytest.raises(ValueError):
        task_minutes(0, 1.0)


# ---------------------------------------------------------------- views

def test_view_is_pure_and_detached():
    state = make_state()
    before = copy.deepcopy(state)
    view = build_worker_view(state, "w_prep")
    assert state == before
    view["skills"]["grill"] = 0.1
    view["stock"]["patty"]["available"] = 0
    view["ready_tasks"].clear()
    assert state == before
    json.dumps(view)  # serializable


def test_view_filters_by_skill_and_dependencies():
    state = make_state()
    assert ready_ids(build_worker_view(state, "w_fry")) == ["O2.f1.fry"]
    # w_prep: prep + grill; O2.b1.assemble waits on its patty, O1 assemble on toppings.
    assert ready_ids(build_worker_view(state, "w_prep")) == [
        "O1.b1.toppings", "O2.b1.patty", "O2.b2.patty"]
    assert ready_ids(build_worker_view(state, "w_expo")) == []


def test_view_fields_and_minutes():
    view = build_worker_view(make_state(), "w_prep")
    assert view["worker_id"] == "w_prep"
    assert view["time"] == 3 and view["deadline"] == 30
    assert view["last_result"] == {"accepted": False, "reason": "station full"}
    patty = next(t for t in view["ready_tasks"] if t["task_id"] == "O2.b1.patty")
    assert patty["my_minutes"] == 6          # ceil(4 * 1.5)
    assert patty["best_minutes"] == 4        # busy w_grill still counts
    assert patty["order_deadline"] == 25
    assert patty["station_has_room"] is True
    assert patty["ingredients_ok"] is True
    assert [w["worker_id"] for w in view["other_workers"]] == ["w_expo", "w_fry", "w_grill"]
    assert view["stock"]["patty"] == {"available": 5, "reserved": 1}
    assert [o["order_id"] for o in view["orders"]] == ["O1", "O2"]


def test_best_minutes_excludes_unavailable_workers():
    state = make_state()
    state["workers"]["w_grill"].update(available=False, status="unavailable")
    patty = next(t for t in build_worker_view(state, "w_prep")["ready_tasks"]
                 if t["task_id"] == "O2.b1.patty")
    assert patty["best_minutes"] == 6


def test_view_feasibility_flags():
    state = make_state()
    state["stations"]["fryer"]["active"] = ["X.f1.fry"]
    state["ingredients"]["lettuce"]["available"] = 0
    fry = build_worker_view(state, "w_fry")["ready_tasks"][0]
    assert fry["station_has_room"] is False
    top = next(t for t in build_worker_view(state, "w_prep")["ready_tasks"]
               if t["task_id"] == "O1.b1.toppings")
    assert top["ingredients_ok"] is False


def test_view_does_not_grant_removed_skill():
    state = make_state()
    del state["workers"]["w_prep"]["skills"]["grill"]  # blocker variant
    assert "O2.b1.patty" not in ready_ids(build_worker_view(state, "w_prep"))


def test_unknown_worker_raises():
    with pytest.raises(KeyError):
        build_worker_view(make_state(), "w_nobody")


# ---------------------------------------------------------------- deterministic

def _rt(task_id, deadline, mine, best, room=True, ok=True):
    return {"task_id": task_id, "order_deadline": deadline, "my_minutes": mine,
            "best_minutes": best, "station_has_room": room, "ingredients_ok": ok}


def test_det_prefers_earliest_deadline():
    env = choose_deterministic(build_worker_view(make_state(), "w_prep"))
    assert env == {
        "proposal": {"action": "start_task", "task_id": "O1.b1.toppings",
                     "why": env["proposal"]["why"]},
        "mode": "det", "model": None, "latency_ms": 0, "attempts": 1,
    }
    assert env["proposal"]["why"]


def test_det_prefers_fastest_then_task_id():
    view = {"ready_tasks": [
        _rt("A.slow", 10, 6, 4),
        _rt("C.fast", 10, 4, 4),
        _rt("B.fast", 10, 4, 4),
    ]}
    assert choose_deterministic(view)["proposal"]["task_id"] == "B.fast"


def test_det_deadline_beats_speed():
    view = {"ready_tasks": [_rt("A.fast", 20, 4, 4), _rt("B.slow", 10, 6, 4)]}
    assert choose_deterministic(view)["proposal"]["task_id"] == "B.slow"


def test_det_skips_infeasible_and_waits():
    view = {"ready_tasks": [_rt("A", 10, 4, 4, room=False), _rt("B", 10, 4, 4, ok=False)]}
    env = choose_deterministic(view)
    assert env["proposal"]["action"] == "wait"
    assert env["proposal"]["task_id"] is None
    assert env["proposal"]["why"]


def test_det_waits_with_no_tasks():
    env = choose_deterministic(build_worker_view(make_state(), "w_expo"))
    assert env["proposal"]["action"] == "wait" and env["proposal"]["why"]


def test_det_is_repeatable():
    state = make_state()
    runs = [choose_deterministic(build_worker_view(state, w)) for w in WORKER_IDS for _ in range(2)]
    assert runs[0::2] == runs[1::2]


def test_det_proposals_pass_validation():
    state = make_state()
    for wid in WORKER_IDS:
        view = build_worker_view(state, wid)
        env = choose_deterministic(view)
        assert validate_proposal(env["proposal"], view) == env["proposal"]


# ---------------------------------------------------------------- validation

VIEW = {"ready_tasks": [{"task_id": "O2.b1.patty"}]}


@pytest.mark.parametrize("obj,msg", [
    ("not a dict", "JSON object"),
    ({"action": "wait", "task_id": None}, "missing"),
    ({"action": "wait", "task_id": None, "why": "x", "station_id": "grill"}, "extra"),
    ({"action": "fly", "task_id": None, "why": "x"}, "unknown action"),
    ({"action": 3, "task_id": None, "why": "x"}, "unknown action"),
    ({"action": "wait", "task_id": None, "why": "   "}, "non-empty"),
    ({"action": "wait", "task_id": None, "why": 5}, "non-empty"),
    ({"action": "wait", "task_id": None, "why": "x" * 501}, "exceeds"),
    ({"action": "wait", "task_id": "O2.b1.patty", "why": "x"}, "null"),
    ({"action": "start_task", "task_id": None, "why": "x"}, "string task_id"),
    ({"action": "start_task", "task_id": 7, "why": "x"}, "string task_id"),
    ({"action": "start_task", "task_id": "O9.b1.patty", "why": "x"}, "ready_tasks"),
    ({"action": "report_blocker", "task_id": "O9.b1.patty", "why": "x"}, "ready_tasks"),
])
def test_validate_rejects(obj, msg):
    with pytest.raises(ValueError, match=msg):
        validate_proposal(obj, VIEW)


@pytest.mark.parametrize("action", ["start_task", "report_blocker", "request_help"])
def test_validate_accepts_task_actions(action):
    out = validate_proposal({"action": action, "task_id": "O2.b1.patty", "why": " go "}, VIEW)
    assert out == {"action": action, "task_id": "O2.b1.patty", "why": "go"}


def test_validate_accepts_wait():
    out = validate_proposal({"action": "wait", "task_id": None, "why": "nothing ready"}, VIEW)
    assert out["action"] == "wait"


# ---------------------------------------------------------------- personas

PERSONAS = ROOT / "sim" / "personas"


@pytest.mark.parametrize("worker_id", WORKER_IDS)
def test_personas_exist_and_load(worker_id):
    text = load_persona(PERSONAS, worker_id)
    assert len(text) > 50


def test_load_persona_missing_and_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_persona(tmp_path, "w_ghost")
    (tmp_path / "w_blank.txt").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_persona(tmp_path, "w_blank")


def test_policies_reexports_decision_error():
    import llm
    assert policies.DecisionError is llm.DecisionError
