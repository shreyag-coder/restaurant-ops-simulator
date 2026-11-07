"""Single-writer restaurant engine and CLI (Python 3.9+ standard library).

Integration handoff for Agents 2/3/4:
* Agent 2 config contains menu/workers/orders/events/target and a scenario
  name. Target has orders/deadline; orders use arrival/deadline and recipe.
  The engine also accepts a nested scenario dictionary for isolated tests.
  Ingredients/stations/workers may be
  ID-keyed dictionaries or lists with an `id`. Ingredient stock is `available`
  (or `stock`); reservations start at zero. Workers have `skills` multipliers.
* Runtime orders expose arrival_minutes/deadline_minutes. Worker-unavailable
  events (tick, worker_id) become availability events internally. Variant
  overrides belong to scenario.load_config, never this engine.
* expand_order returns tasks per TASKS.md and expected_items with id,
  recipe_id, modifiers (one record per unit); item_id is accepted as the item
  identifier and exposed as both id/item_id. Task IDs/dependencies are global.
* State uses ID-keyed workers/tasks/orders/stations/ingredients. `time` is the
  current/last processed tick, `next_tick` is the next tick to process, and
  `continuation` saves choose/apply cursors for a paused, partially processed
  tick. Policies receive deep copies, never authoritative objects. The engine
  attaches worker_id, state_version, mode, and the frozen model to every view;
  AI adapters should use view['model'] rather than a changed environment value.
* Events have seq/run_id/tick/type and a payload. Checkpoint writers receive
  a detached state and only new events. last_committed_seq marks the boundary;
  last_event_seq includes buffered events. No helper is imported at startup.

SimulationEngine(state, expand_order, build_worker_view, choose_decision) is
dependency-injected so engine tests and other agents can work independently.
choose_decision(view, worker_id) returns the TASKS.md decision envelope.
Only the orchestrator calls recording.save_checkpoint, under a per-run lock.
"""

import argparse
import copy
import importlib
import json
import math
import os
import socket
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path


SCHEMA_VERSION = 1
TERMINAL = {"completed", "timed_out"}
ACTIONS = {"start_task", "wait", "report_blocker", "request_help"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _number(value, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _records(rows, label):
    if isinstance(rows, dict):
        result = copy.deepcopy(rows)
        for key, row in result.items():
            _require(isinstance(row, dict), "Invalid " + label)
            _require(row.get("id", key) == key, "Mismatched " + label + " ID")
            row["id"] = key
    else:
        _require(isinstance(rows, list), "Expected records for " + label)
        result = {row["id"]: copy.deepcopy(row) for row in rows}
        _require(len(result) == len(rows), "Duplicate " + label + " IDs")
    _require(all(isinstance(key, str) and key for key in result), "Invalid " + label + " IDs")
    return result


def _item_key(item):
    return (item.get("recipe_id", item.get("recipe")), tuple(sorted(item.get("modifiers", []))))


def create_state(config, mode, run_id=None, model=None, personas=None):
    """Return a fresh JSON-serializable state; never mutate seed inputs."""
    _require(mode in {"det", "ai"}, "Mode must be explicitly det or ai")
    frozen = copy.deepcopy(config)
    scenario = frozen["scenario"] if isinstance(frozen["scenario"], dict) else frozen
    menu = frozen["menu"]
    target = scenario.get("target", {})
    deadline = scenario.get("deadline_minutes", target.get("deadline_minutes", target.get("deadline")) if isinstance(target, dict) else None)
    _require(type(deadline) is int and deadline > 0, "deadline_minutes must be a positive integer")
    workers = _records(frozen["workers"], "worker")
    stations = _records(menu["stations"], "station")
    ingredients = _records(menu["ingredients"], "ingredient")
    orders = _records(scenario["orders"], "order")
    _require(bool(workers) and bool(stations) and bool(orders), "Workers, stations, and orders are required")
    target_count = target.get("completed_orders", target.get("orders", len(orders))) if isinstance(target, dict) else target
    _require(type(target_count) is int and 0 < target_count <= len(orders), "Invalid completed-order target")
    for worker in workers.values():
        _require(bool(worker["skills"]) and all(_number(v, True) for v in worker["skills"].values()), "Invalid worker skills")
        _require(type(worker.get("available", True)) is bool, "Invalid worker availability")
        worker.update(current_task=None, history=[], last_result=None)
        worker.setdefault("available", True)
    for station in stations.values():
        _require(type(station["capacity"]) is int and station["capacity"] > 0, "Invalid station capacity")
        _require(type(station.get("available", True)) is bool, "Invalid station availability")
        station.update(active_tasks=[])
        station.setdefault("available", True)
    for ingredient in ingredients.values():
        available = ingredient.get("available", ingredient.get("stock"))
        _require(_number(available) and ingredient.get("reserved", 0) == 0, "Invalid starting stock/reservations")
        ingredient.update(available=available, reserved=0)
    for order in orders.values():
        arrival = order.get("arrival_minutes", order.get("arrival", 0))
        due = order.get("deadline_minutes", order.get("deadline", deadline))
        _require(type(arrival) is int and type(due) is int and 0 <= arrival < due <= deadline, "Invalid order times")
        _require(bool(order["items"]), "Empty order")
        for item in order["items"]:
            _require(type(item["quantity"]) is int and item["quantity"] > 0, "Invalid item quantity")
            _require(isinstance(item.get("modifiers", []), list), "Invalid modifiers")
        order.update(arrival_minutes=arrival, deadline_minutes=due, status="scheduled",
                     task_ids=[], expected_items=[], packed=False, correct=None,
                     on_time=None, completed_at=None)
    events = copy.deepcopy(scenario.get("events", []))
    for event in events:
        if event["type"] == "worker_unavailable":
            event.update(type="set_worker_available", at_minutes=event["tick"], available=False)
        _require(event["type"] == "set_worker_available", "Unsupported scenario event")
        _require(event["worker_id"] in workers, "Unknown event worker")
        _require(type(event["at_minutes"]) is int and 0 <= event["at_minutes"] <= deadline, "Invalid event time")
        _require(type(event["available"]) is bool, "Invalid event availability")
    return {
        "schema_version": SCHEMA_VERSION, "run_id": run_id or uuid.uuid4().hex,
        "state_version": 0, "mode": mode, "model": model,
        "status": "running", "pause_reason": None, "error": None,
        "time": 0, "next_tick": 0, "deadline": deadline,
        "target": {"completed_orders": target_count},
        "config": frozen, "personas": copy.deepcopy(personas or {}),
        "workers": workers, "stations": stations, "ingredients": ingredients,
        "orders": orders, "tasks": {}, "produced_items": {},
        "scenario_events": events, "applied_events": [], "blockers": {},
        "last_event_seq": 0, "last_committed_seq": 0, "recent_events": [],
        "continuation": {"phase": "begin"},
    }


class SimulationEngine:
    """Own all mutations. Helper calls receive copies and return data only."""

    def __init__(self, state, expand_order, build_worker_view, choose_decision):
        _require(state.get("schema_version") == SCHEMA_VERSION, "Unsupported state schema")
        self.state = copy.deepcopy(state)
        self.expand_order = expand_order
        self.build_worker_view = build_worker_view
        self.choose_decision = choose_decision
        self.pending_events = []

    def snapshot(self):
        return copy.deepcopy(self.state)

    def _emit(self, event_type, **payload):
        s = self.state
        s["state_version"] += 1
        s["last_event_seq"] += 1
        event = {"seq": s["last_event_seq"], "run_id": s["run_id"],
                 "tick": s["time"], "type": event_type, "payload": copy.deepcopy(payload)}
        for field in ("worker_id", "task_id"):
            if field in payload:
                event[field] = payload[field]
        self.pending_events.append(event)
        s["recent_events"] = (s["recent_events"] + [copy.deepcopy(event)])[-50:]

    def _ready(self, task):
        return task["status"] == "pending" and all(
            self.state["tasks"][key]["status"] == "done" for key in task["depends_on"])

    def _eligible(self, worker_id):
        worker = self.state["workers"][worker_id]
        return worker["available"] and worker["current_task"] is None and any(
            self._ready(t) and t["required_skill"] in worker["skills"]
            for t in self.state["tasks"].values())

    def _admit_orders(self):
        s = self.state
        for order in s["orders"].values():
            if order["status"] != "scheduled" or order["arrival_minutes"] > s["time"]:
                continue
            expanded = self.expand_order(copy.deepcopy(order), copy.deepcopy(s["config"]["menu"]))
            tasks = _records(expanded["tasks"], "task")
            expected_items = copy.deepcopy(expanded["expected_items"])
            for item in expected_items:
                item.setdefault("id", item.get("item_id"))
                item.setdefault("item_id", item["id"])
            items = _records(expected_items, "item")
            requested = Counter()
            for item in order["items"]:
                requested[_item_key(item)] += item["quantity"]
            _require(Counter(_item_key(item) for item in items.values()) == requested,
                     "Expanded items do not match requested quantities/modifiers")
            _require(not (set(tasks) & set(s["tasks"])), "Task IDs must be globally unique")
            _require(not (set(items) & {i["id"] for o in s["orders"].values() for i in o["expected_items"]}), "Item IDs must be globally unique")
            for task in tasks.values():
                _require(task["order_id"] == order["id"], "Mismatched task order")
                _require(task["station_id"] in s["stations"], "Unknown task station")
                _require(isinstance(task["required_skill"], str) and task["required_skill"], "Missing task skill")
                _require(_number(task["base_minutes"], True), "Invalid task duration")
                _require(set(task["depends_on"]) <= set(tasks) and task["id"] not in task["depends_on"], "Invalid task dependencies")
                _require(all(key in s["ingredients"] and _number(n, True) for key, n in task["ingredients"].items()), "Invalid task ingredients")
                _require(task["step_id"] in {"pack", "verify"} or task["item_id"] in items, "Unknown task item")
                task.update(status="pending", worker_id=None, started_at=None, finishes_at=None,
                            reserved_ingredients={}, consumed_ingredients={}, completed_at=None)
            remaining, done = set(tasks), set()
            while remaining:
                ready = {key for key in remaining if set(tasks[key]["depends_on"]) <= done}
                _require(bool(ready), "Cyclic task dependencies")
                remaining -= ready
                done |= ready
            packs = [t for t in tasks.values() if t["step_id"] == "pack"]
            verifies = [t for t in tasks.values() if t["step_id"] == "verify"]
            _require(len(packs) == len(verifies) == 1, "Each order requires one pack and verify")
            terminals = []
            for item_id in items:
                unit_tasks = [t for t in tasks.values() if t["item_id"] == item_id and t["step_id"] not in {"pack", "verify"}]
                _require(bool(unit_tasks), "An item has no recipe tasks")
                _require(all(set(t["depends_on"]) <= {u["id"] for u in unit_tasks} for t in unit_tasks), "Cross-item recipe dependencies")
                referenced = {key for t in unit_tasks for key in t["depends_on"]}
                terminals.extend(t["id"] for t in unit_tasks if t["id"] not in referenced)
            _require(set(packs[0]["depends_on"]) == set(terminals), "Pack must depend on every final item step")
            _require(verifies[0]["depends_on"] == [packs[0]["id"]], "Verify must depend on pack")
            s["tasks"].update(tasks)
            order.update(status="pending", task_ids=list(tasks), expected_items=list(items.values()))
            self._emit("order_admitted", order_id=order["id"], task_ids=list(tasks))

    def apply_action(self, worker_id, proposal):
        """Return a result; rejected proposals only affect result/event metadata."""
        s = self.state
        worker = s["workers"].get(worker_id)
        task = None

        def reject(reason):
            result = {"accepted": False, "reason": reason}
            if worker is not None:
                worker["last_result"] = result.copy()
            self._emit("action_rejected", worker_id=worker_id, proposal=proposal, reason=reason)
            return result

        if s["status"] != "running" or s["time"] >= s["deadline"]:
            return reject("Run is not accepting actions")
        if not worker or not worker["available"] or worker["current_task"] is not None:
            return reject("Worker is unavailable or busy")
        if (not isinstance(proposal, dict) or set(proposal) != {"action", "task_id", "why"}
                or not isinstance(proposal["action"], str) or proposal["action"] not in ACTIONS
                or not isinstance(proposal["why"], str) or not proposal["why"].strip()):
            return reject("Invalid action schema")
        action, task_id = proposal["action"], proposal["task_id"]
        if action == "wait":
            if task_id is not None:
                return reject("Wait requires a null task_id")
        elif not isinstance(task_id, str) or task_id not in s["tasks"]:
            return reject("Task does not exist")
        else:
            task = s["tasks"][task_id]
        if action == "start_task":
            if task["status"] != "pending":
                return reject("Task is not pending")
            if task["required_skill"] not in worker["skills"]:
                return reject("Worker lacks required skill")
            if not self._ready(task):
                return reject("Prerequisites are unfinished")
            if any(s["ingredients"][key]["available"] < n for key, n in task["ingredients"].items()):
                return reject("Insufficient ingredients")
            station = s["stations"][task["station_id"]]
            if not station["available"] or len(station["active_tasks"]) >= station["capacity"]:
                return reject("Station is unavailable or full")
            # Decimal text avoids float artifacts such as ceil(10 * 1.1) = 12.
            duration = math.ceil(Fraction(str(task["base_minutes"]))
                                 * Fraction(str(worker["skills"][task["required_skill"]])))
            # All checks precede all resource mutations. Only this process writes.
            for key, n in task["ingredients"].items():
                s["ingredients"][key]["available"] -= n
                s["ingredients"][key]["reserved"] += n
            task.update(status="active", worker_id=worker_id, started_at=s["time"],
                        finishes_at=s["time"] + duration,
                        reserved_ingredients=copy.deepcopy(task["ingredients"]))
            worker["current_task"] = task_id
            station["active_tasks"].append(task_id)
            s["orders"][task["order_id"]]["status"] = "active"
            self._emit("task_started", worker_id=worker_id, task_id=task_id,
                       finishes_at=task["finishes_at"], ingredients=task["ingredients"])
        result = {"accepted": True, "reason": "Accepted"}
        worker["last_result"] = result.copy()
        self._emit("action_accepted", worker_id=worker_id, task_id=task_id, proposal=proposal)
        return result

    def set_worker_available(self, worker_id, available):
        _require(type(available) is bool, "Availability must be boolean")
        s = self.state
        worker = s["workers"][worker_id]
        if worker["available"] == available:
            return
        worker["available"] = available
        if not available and worker["current_task"]:
            task = s["tasks"][worker["current_task"]]
            self._release(task, consume=False)
            task.update(status="pending", worker_id=None, started_at=None, finishes_at=None)
            self._emit("task_interrupted", worker_id=worker_id, task_id=task["id"])
        self._emit("worker_availability", worker_id=worker_id, available=available)

    def _release(self, task, consume):
        s = self.state
        for key, n in task["reserved_ingredients"].items():
            s["ingredients"][key]["reserved"] -= n
            if not consume:
                s["ingredients"][key]["available"] += n
        task["reserved_ingredients"] = {}
        s["stations"][task["station_id"]]["active_tasks"].remove(task["id"])
        s["workers"][task["worker_id"]]["current_task"] = None

    def _verify(self, order):
        s = self.state
        requested = Counter()
        for item in order["items"]:
            requested[_item_key(item)] += item["quantity"]
        produced = [item for item in s["produced_items"].values() if item["order_id"] == order["id"]]
        expected = {item["id"]: item for item in order["expected_items"]}
        ingredients_match = all(
            "ingredients" not in expected.get(item["id"], {})
            or item.get("ingredients") == expected[item["id"]]["ingredients"]
            for item in produced)
        order["correct"] = (order["packed"] and all(s["tasks"][key]["status"] == "done" for key in order["task_ids"])
                            and Counter(_item_key(item) for item in produced) == requested and ingredients_match)
        order["completed_at"] = s["time"]
        order["on_time"] = s["time"] <= order["deadline_minutes"]
        order["status"] = "completed" if order["correct"] else "failed"
        self._emit("order_verified", order_id=order["id"], correct=order["correct"], on_time=order["on_time"])

    def _complete_tasks(self):
        s = self.state
        for task in s["tasks"].values():
            if task["status"] != "active" or task["finishes_at"] > s["time"]:
                continue
            worker_id = task["worker_id"]
            task["consumed_ingredients"] = copy.deepcopy(task["reserved_ingredients"])
            self._release(task, consume=True)
            task.update(status="done", completed_at=s["time"])
            s["workers"][worker_id]["history"].append(task["id"])
            self._emit("task_completed", worker_id=worker_id, task_id=task["id"])
            order = s["orders"][task["order_id"]]
            if task["step_id"] == "pack":
                order["packed"] = True
            elif task["step_id"] == "verify":
                self._verify(order)
            else:
                item_id = task["item_id"]
                item_tasks = [t for t in s["tasks"].values() if t["order_id"] == order["id"] and t["item_id"] == item_id and t["step_id"] not in {"pack", "verify"}]
                if all(t["status"] == "done" for t in item_tasks):
                    item = next(i for i in order["expected_items"] if i["id"] == item_id)
                    used = Counter()
                    for step in item_tasks:
                        used.update(step["consumed_ingredients"])
                    s["produced_items"][item_id] = dict(copy.deepcopy(item), order_id=order["id"], ingredients=dict(used))
                    self._emit("item_produced", order_id=order["id"], item_id=item_id)

    def _detect_blockers(self):
        s = self.state
        blocked = {}
        for task in s["tasks"].values():
            if self._ready(task) and not any(w["available"] and task["required_skill"] in w["skills"] for w in s["workers"].values()):
                blocked[task["id"]] = "No available worker has skill " + task["required_skill"]
                if task["id"] not in s["blockers"]:
                    self._emit("blocker", task_id=task["id"], reason=blocked[task["id"]])
        s["blockers"] = blocked

    def _start_round(self, candidates, round_number):
        snapshot = self.snapshot()
        views = {key: self.build_worker_view(copy.deepcopy(snapshot), key) for key in candidates}
        for worker_id, view in views.items():
            _require(isinstance(view, dict), "Worker view must be a dictionary")
            view.update(worker_id=worker_id, state_version=snapshot["state_version"],
                        mode=snapshot["mode"], model=snapshot["model"])
        # Views may not alias the authoritative state or each other.
        self.state["continuation"] = {"phase": "choose", "round": round_number,
                                      "candidates": list(candidates), "views": copy.deepcopy(views),
                                      "decisions": {}, "cursor": 0, "rejected": []}

    def _decision_rounds(self):
        s = self.state
        while s["continuation"]["phase"] in {"choose", "apply"}:
            c = s["continuation"]
            while c["phase"] == "choose" and c["cursor"] < len(c["candidates"]):
                worker_id = c["candidates"][c["cursor"]]
                try:
                    envelope = self.choose_decision(copy.deepcopy(c["views"][worker_id]), worker_id)
                    _require(isinstance(envelope, dict) and envelope.get("mode") == s["mode"], "Policy mode mismatch")
                    _require("proposal" in envelope, "Policy did not return a proposal")
                    _require(s["mode"] != "ai" or envelope.get("model") == s["model"], "Policy model mismatch")
                    json.dumps(envelope, allow_nan=False)
                except Exception as exc:
                    # Provider adapters must sanitize errors. Avoid persisting exception
                    # text/HTTP bodies, which can contain credentials or private input.
                    s.update(status="paused", pause_reason="decision_error",
                             error={"worker_id": worker_id, "type": type(exc).__name__,
                                    "message": "Decision failed; inspect provider configuration and retry resume."})
                    metadata = {}
                    attempts, latency = getattr(exc, "attempts", None), getattr(exc, "latency_ms", None)
                    if type(attempts) is int and attempts > 0:
                        metadata["attempts"] = attempts
                    if _number(latency):
                        metadata["latency_ms"] = latency
                    self._emit("decision_error", worker_id=worker_id, mode=s["mode"], model=s["model"], error_type=type(exc).__name__, **metadata)
                    return
                # Proposals themselves are validated by apply_action.
                c["decisions"][worker_id] = copy.deepcopy(envelope)
                c["cursor"] += 1
            if c["phase"] == "choose":
                c.update(phase="apply", cursor=0)
            while c["cursor"] < len(c["candidates"]):
                worker_id = c["candidates"][c["cursor"]]
                envelope = c["decisions"][worker_id]
                result = self.apply_action(worker_id, envelope["proposal"])
                self._emit("decision", worker_id=worker_id, mode=s["mode"],
                           model=envelope.get("model"), proposal=envelope["proposal"],
                           latency_ms=envelope.get("latency_ms", 0), attempts=envelope.get("attempts", 1),
                           observed_state_version=c["views"][worker_id].get("state_version"),
                           round=c["round"], result=result)
                if not result["accepted"] and self._eligible(worker_id):
                    c["rejected"].append(worker_id)
                c["cursor"] += 1
            if c["round"] == 0 and c["rejected"]:
                self._start_round(c["rejected"], 1)
            else:
                s["continuation"] = {"phase": "finish"}

    def run(self, max_ticks=None, checkpoint=None):
        """Run/resume; max_ticks counts finished ticks, not provider retries."""
        s = self.state
        _require(max_ticks is None or (type(max_ticks) is int and max_ticks > 0), "max_ticks must be positive")
        _require(s["status"] not in TERMINAL, "Cannot resume a terminal run")
        if s["status"] == "paused":
            s.update(status="running", pause_reason=None, error=None)
        count = 0
        while s["status"] == "running":
            if s["continuation"]["phase"] == "begin":
                s["time"] = s["next_tick"]
                for index, event in enumerate(s["scenario_events"]):
                    if index not in s["applied_events"] and event["at_minutes"] <= s["time"]:
                        self.set_worker_available(event["worker_id"], event["available"])
                        s["applied_events"].append(index)
                self._admit_orders()
                self._complete_tasks()
                self._detect_blockers()
                completed = sum(o["correct"] is True for o in s["orders"].values())
                if completed >= s["target"]["completed_orders"]:
                    s["status"] = "completed"
                elif s["time"] >= s["deadline"]:
                    s["status"] = "timed_out"
                if s["status"] in TERMINAL:
                    for order in s["orders"].values():
                        if order["correct"] is None:
                            order.update(correct=False, on_time=False)
                    s.update(next_tick=None, continuation={"phase": "terminal"})
                    self._emit("run_finished", status=s["status"], correct_orders=completed)
                else:
                    self._start_round([key for key in sorted(s["workers"]) if self._eligible(key)], 0)
            if s["status"] == "running":
                self._decision_rounds()
            if s["status"] == "paused":
                if checkpoint:
                    checkpoint(self)
                break
            if s["status"] == "running":
                s.update(next_tick=s["time"] + 1, continuation={"phase": "begin"})
                count += 1
                if max_ticks is not None and count >= max_ticks:
                    s.update(status="paused", pause_reason="max_ticks")
            if checkpoint:
                checkpoint(self)
        return self.snapshot()


@contextmanager
def run_lock(run_dir):
    """OS advisory lock: automatically released even when a process crashes."""
    import fcntl

    lock_path = Path(run_dir) / ".writer.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another orchestrator is writing this run") from exc
        # Record the owner in the format recording.py expects, so its
        # save_checkpoint recognizes this process as the run's writer.
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "host": socket.gethostname()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise ValueError("Required integration module is not available: " + name) from exc


def _engine_for(state, scenario, policies):
    def decide(view, worker_id):
        if state["mode"] == "det":
            return policies.choose_deterministic(view)
        return policies.choose_ai(view, state["personas"][worker_id])
    return SimulationEngine(state, scenario.expand_order, policies.build_worker_view, decide)


def _checkpoint(engine, run_dir, recording):
    state = engine.snapshot()
    committed_seq = state["last_event_seq"]
    state["last_committed_seq"] = committed_seq
    recording.save_checkpoint(run_dir, state, copy.deepcopy(engine.pending_events))
    engine.state["last_committed_seq"] = committed_seq
    engine.pending_events.clear()


def _select_run(root, run_id):
    if run_id:
        _require(Path(run_id).name == run_id and run_id not in {".", ".."}, "Invalid run ID")
        result = root / run_id
        _require(result.is_dir() and not result.is_symlink(), "Run not found")
        return result
    candidates = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / "state.json").is_file()] if root.exists() else []
    _require(bool(candidates), "No recorded runs found")
    return max(candidates, key=lambda p: ((p / "state.json").stat().st_mtime_ns, p.name))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs-dir", type=Path, default=Path("sim/runs"))
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run", help="Start a fresh run; existing runs are kept")
    start.add_argument("--scenario", required=True, choices=("baseline", "disruption", "blocker"))
    start.add_argument("--mode", required=True, choices=("det", "ai"))
    start.add_argument("--config-dir", type=Path, default=Path("sim/config"))
    start.add_argument("--personas-dir", type=Path, default=Path("sim/personas"))
    start.add_argument("--max-ticks", type=int)
    resume = sub.add_parser("resume", help="Continue a paused run from its checkpoint")
    resume.add_argument("--run")
    resume.add_argument("--max-ticks", type=int)
    replay = sub.add_parser("replay", help="Read a saved run without policies or networking")
    replay.add_argument("--run")
    replay.add_argument("--at", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            _require(args.at is None or args.at >= 0, "Replay tick must be nonnegative")
            run_dir = _select_run(args.runs_dir, args.run)
            print(_module("replay").replay_run(run_dir, at=args.at))
            return 0
        _require(args.max_ticks is None or args.max_ticks > 0, "max_ticks must be positive")
        scenario, policies, recording = (_module(name) for name in ("scenario", "policies", "recording"))
        if args.command == "run":
            config = scenario.load_config(args.config_dir, args.scenario)
            scenario.validate_config(config)
            model = os.environ.get("OPENROUTER_MODEL") if args.mode == "ai" else None
            personas = {}
            if args.mode == "ai":
                _require(bool(model) and bool(os.environ.get("OPENROUTER_API_KEY")), "AI mode requires OPENROUTER_MODEL and OPENROUTER_API_KEY")
                for worker_id in _records(config["workers"], "worker"):
                    _require(Path(worker_id).name == worker_id and worker_id not in {".", ".."}, "Invalid persona ID")
                    personas[worker_id] = (args.personas_dir / (worker_id + ".txt")).read_text(encoding="utf-8")
            state = create_state(config, args.mode, model=model, personas=personas)
            run_dir = args.runs_dir / state["run_id"]
            run_dir.mkdir(parents=True, exist_ok=False)
        else:
            run_dir = _select_run(args.runs_dir, args.run)
        with run_lock(run_dir):
            if args.command == "resume":
                state = recording.load_checkpoint(run_dir)
                _require(state["run_id"] == run_dir.name, "Checkpoint run ID mismatch")
                _require(state["last_event_seq"] == state["last_committed_seq"], "Incomplete checkpoint")
                _require(state["status"] == "paused", "Only paused runs may resume")
                if state["mode"] == "ai":
                    _require(bool(os.environ.get("OPENROUTER_API_KEY")), "AI resume requires OPENROUTER_API_KEY")
            engine = _engine_for(state, scenario, policies)
            result = engine.run(args.max_ticks, lambda current: _checkpoint(current, run_dir, recording))
        print("Run {}: {} at minute {} ({})".format(result["run_id"], result["status"], result["time"], run_dir))
        if result["pause_reason"] == "decision_error":
            print(result["error"]["message"], file=sys.stderr)
            return 2
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
