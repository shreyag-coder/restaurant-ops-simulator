"""Worker decision policies: detached views, deterministic and AI choices.

Everything here is pure with respect to the simulation: functions read the
authoritative state passed in by the orchestrator, return fresh dicts, and
never mutate state or write run files. The orchestrator validates and applies
proposals; these helpers only propose.
"""

import copy
import math
from decimal import Decimal
from pathlib import Path

from llm import DecisionError, request_decision  # noqa: F401  (re-exported)

ACTIONS = ("start_task", "wait", "report_blocker", "request_help")
TASK_ACTIONS = ("start_task", "report_blocker", "request_help")
PROPOSAL_KEYS = frozenset(("action", "task_id", "why"))
MAX_WHY_CHARS = 500

__all__ = [
    "ACTIONS",
    "DecisionError",
    "build_worker_view",
    "choose_ai",
    "choose_deterministic",
    "load_persona",
    "task_minutes",
    "validate_proposal",
]


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------

def task_minutes(base_minutes, multiplier):
    """Return ceil(base_minutes * multiplier) as an int, without float error.

    Decimal(str(x)) keeps 10 * 1.1 == 11 exactly (plain floats give
    11.000000000000002, which would wrongly round up to 12).
    """
    if base_minutes <= 0 or multiplier <= 0:
        raise ValueError("base_minutes and multiplier must be positive")
    product = Decimal(str(base_minutes)) * Decimal(str(multiplier))
    return int(math.ceil(product))


# --------------------------------------------------------------------------
# State access (the only place that knows the state field names; keep in
# sync with Agent 1's state schema)
# --------------------------------------------------------------------------

def _workers(state):
    return state.get("workers", {})


def _tasks(state):
    return state.get("tasks", {})


def _is_available(worker):
    return bool(worker.get("available", True)) and worker.get("status") != "unavailable"


def _station_has_room(state, station_id):
    station = state.get("stations", {}).get(station_id)
    if station is None or not station.get("available", True):
        return False
    # The engine keeps occupancy in "active_tasks"; "active" is the older name.
    active = station.get("active_tasks", station.get("active", []))
    return len(active) < station.get("capacity", 0)


def _ingredients_ok(state, needed):
    stock = state.get("ingredients", {})
    for ingredient_id, qty in (needed or {}).items():
        if qty <= 0:
            continue
        if stock.get(ingredient_id, {}).get("available", 0) < qty:
            return False
    return True


def _order_deadline(state, order_id):
    order = state.get("orders", {}).get(order_id, {})
    return order.get("deadline", state.get("deadline"))


def _dependency_ready_tasks(state):
    tasks = _tasks(state)
    ready = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        if task.get("status") != "pending":
            continue
        deps = task.get("depends_on", [])
        if all(tasks.get(dep, {}).get("status") == "done" for dep in deps):
            ready.append(task)
    return ready


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

def build_worker_view(state, worker_id):
    """Return a detached, JSON-serializable view of the state for one worker.

    ready_tasks lists dependency-ready pending tasks this worker is qualified
    for. best_minutes is the fastest duration among available qualified
    workers, busy ones included (a proficiency comparison, not a claim).
    """
    workers = _workers(state)
    if worker_id not in workers:
        raise KeyError("unknown worker: %s" % worker_id)
    me = workers[worker_id]
    my_skills = me.get("skills", {})

    ready_tasks = []
    for task in _dependency_ready_tasks(state):
        skill = task["required_skill"]
        if skill not in my_skills:
            continue
        base = task["base_minutes"]
        # A slowed-down station (e.g. old fryer oil) stretches every worker's time.
        slow = state.get("stations", {}).get(task["station_id"], {}).get("duration_multiplier", 1)
        if slow != 1:
            base = Decimal(str(base)) * Decimal(str(slow))
        qualified = [
            task_minutes(base, w["skills"][skill])
            for w in workers.values()
            if _is_available(w) and skill in w.get("skills", {})
        ]
        best = min(qualified) if qualified else None
        ready_tasks.append({
            "task_id": task["id"],
            "order_id": task["order_id"],
            "step_id": task["step_id"],
            "required_skill": skill,
            "station_id": task["station_id"],
            "order_deadline": _order_deadline(state, task["order_id"]),
            "my_minutes": task_minutes(base, my_skills[skill]),
            "best_minutes": best,
            "station_has_room": _station_has_room(state, task["station_id"]),
            "ingredients_ok": _ingredients_ok(state, task.get("ingredients")),
        })

    orders = state.get("orders", {})
    view = {
        "time": state.get("time"),
        "deadline": state.get("deadline"),
        "worker_id": worker_id,
        "skills": dict(sorted(my_skills.items())),
        "last_result": me.get("last_result"),
        "ready_tasks": ready_tasks,
        "orders": [
            {
                "order_id": oid,
                "deadline": orders[oid].get("deadline", state.get("deadline")),
                "status": orders[oid].get("status"),
            }
            for oid in sorted(orders)
        ],
        "other_workers": [
            {
                "worker_id": wid,
                "status": workers[wid].get("status"),
                "available": _is_available(workers[wid]),
                "current_task": workers[wid].get("current_task"),
            }
            for wid in sorted(workers)
            if wid != worker_id
        ],
        "stock": {
            iid: {"available": rec.get("available", 0), "reserved": rec.get("reserved", 0)}
            for iid, rec in sorted(state.get("ingredients", {}).items())
        },
        "stations": {
            sid: {
                "working": bool(rec.get("available", True)),
                "in_use": len(rec.get("active_tasks", rec.get("active", []))),
                "capacity": rec.get("capacity", 0),
                "slowdown": rec.get("duration_multiplier", 1),
            }
            for sid, rec in sorted(state.get("stations", {}).items())
        },
        # Latest messages from the kitchen manager (live operator commands).
        "announcements": [dict(a) for a in state.get("announcements", [])[-5:]],
    }
    # Detach completely: nothing in the view may alias the live state.
    return copy.deepcopy(view)


# --------------------------------------------------------------------------
# Proposal validation (shared by the AI path and tests)
# --------------------------------------------------------------------------

def validate_proposal(obj, view):
    """Return a normalized proposal dict or raise ValueError with a reason.

    Checks shape and references only. Feasibility (room, stock, conflicts) is
    the engine's job.
    """
    if not isinstance(obj, dict):
        raise ValueError("proposal must be a JSON object")
    keys = set(obj)
    if keys != PROPOSAL_KEYS:
        extra = sorted(keys - PROPOSAL_KEYS)
        missing = sorted(PROPOSAL_KEYS - keys)
        raise ValueError("proposal keys mismatch (extra=%s, missing=%s)" % (extra, missing))

    action = obj["action"]
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unknown action: %r" % (action,))

    why = obj["why"]
    if not isinstance(why, str) or not why.strip():
        raise ValueError("why must be a non-empty string")
    why = why.strip()
    if len(why) > MAX_WHY_CHARS:
        raise ValueError("why exceeds %d characters" % MAX_WHY_CHARS)

    task_id = obj["task_id"]
    if action == "wait":
        if task_id is not None:
            raise ValueError("wait must have task_id null")
    else:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("%s requires a string task_id" % action)
        known = {t["task_id"] for t in view.get("ready_tasks", [])}
        if task_id not in known:
            raise ValueError("task_id %r is not in this worker's ready_tasks" % task_id)

    return {"action": action, "task_id": task_id, "why": why}


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------

def _envelope(proposal, mode, model=None, latency_ms=0, attempts=1):
    return {
        "proposal": proposal,
        "mode": mode,
        "model": model,
        "latency_ms": latency_ms,
        "attempts": attempts,
    }


def choose_deterministic(view):
    """Earliest deadline, then tasks where I'm fastest, then task ID."""
    feasible = [
        t for t in view.get("ready_tasks", [])
        if t["station_has_room"] and t["ingredients_ok"]
    ]
    if not feasible:
        if view.get("ready_tasks"):
            why = "Ready tasks lack station room or ingredients; waiting."
        else:
            why = "No ready tasks I am qualified for."
        return _envelope({"action": "wait", "task_id": None, "why": why}, "det")

    def rank(t):
        slower = t["best_minutes"] is not None and t["my_minutes"] > t["best_minutes"]
        return (t["order_deadline"], slower, t["task_id"])

    pick = min(feasible, key=rank)
    if pick["best_minutes"] is not None and pick["my_minutes"] > pick["best_minutes"]:
        why = "Earliest-deadline feasible task (deadline %s)." % pick["order_deadline"]
    else:
        why = "Earliest-deadline feasible task where I am fastest (deadline %s)." % (
            pick["order_deadline"],
        )
    proposal = {"action": "start_task", "task_id": pick["task_id"], "why": why}
    return _envelope(proposal, "det")


def choose_ai(view, persona, *, model=None, api_key=None, timeout=30.0, transport=None):
    """Ask the configured OpenRouter model. Raises DecisionError on failure."""
    return request_decision(
        view, persona, model=model, api_key=api_key, timeout=timeout, transport=transport
    )


# --------------------------------------------------------------------------
# Personas
# --------------------------------------------------------------------------

def load_persona(personas_dir, worker_id):
    """Read sim/personas/<worker_id>.txt; raise on missing or empty text."""
    path = Path(personas_dir) / ("%s.txt" % worker_id)
    if not path.is_file():
        raise FileNotFoundError("persona not found for worker %s: %s" % (worker_id, path))
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("persona for worker %s is empty" % worker_id)
    return text
