"""Scenario agent: turns a manager's free-text description into kitchen changes.

    "Dana burned her hand"  ->  [worker_skill w_grill grill x2, announce "..."]

The agent only *proposes* changes.  It never touches the state: the live
console hands its proposals to ``engine.queue_batch``, which validates each
one and applies the accepted ones at the start of the next minute, exactly as
worker decisions are validated before they change anything.

Standard library only; uses the same OpenRouter transport and sanitizing as
``llm.py`` (strict JSON schema, at most two attempts, key never logged).
"""

import json
import os
import time

from llm import _AttemptFailure, _post, _sanitize, urllib_transport

MAX_ATTEMPTS = 2
MAX_TOKENS = 2000
NON_RETRYABLE_STATUS = (400, 401, 402, 403, 404)

CHANGE_TYPES = ["station_available", "station_capacity", "station_speed", "worker_available",
                "worker_skill", "stock", "add_order", "order_deadline", "announce"]

_NULLABLE_STR = {"type": ["string", "null"]}
CHANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": CHANGE_TYPES},
        "station_id": _NULLABLE_STR,
        "worker_id": _NULLABLE_STR,
        "skill": _NULLABLE_STR,
        "ingredient_id": _NULLABLE_STR,
        "order_id": _NULLABLE_STR,
        "available": {"type": ["boolean", "null"]},
        "capacity": {"type": ["integer", "null"]},
        "multiplier": {"type": ["number", "null"]},
        "remove_skill": {"type": ["boolean", "null"]},
        # Providers reject null inside enums, so "none" stands for "not used".
        "stock_mode": {"type": "string", "enum": ["set", "add", "none"]},
        "amount": {"type": ["integer", "null"]},
        "deadline_minutes": {"type": ["integer", "null"]},
        "items": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "recipe": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["recipe", "quantity", "modifiers"],
                "additionalProperties": False,
            },
        },
        "text": _NULLABLE_STR,
        "why": {"type": "string"},
    },
    "additionalProperties": False,
}
CHANGE_SCHEMA["required"] = sorted(CHANGE_SCHEMA["properties"])

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": CHANGE_SCHEMA},
    },
    "required": ["summary", "changes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are the scenario agent of a restaurant kitchen simulation. The kitchen
manager describes something happening in plain English. Translate it into the
smallest set of structured changes that model its physical effect on the
kitchen. A simulation engine validates and applies your changes; the worker
agents then react to the updated kitchen on their own. Do not plan their work.

Building blocks (fill every field; use null for fields a type does not use, and
stock_mode "none" unless the change is a stock change):
- station_available: station_id, available (false = out of service, true = working again)
- station_capacity: station_id, capacity (how many tasks at once; restore to normal_capacity)
- station_speed: station_id, multiplier (task duration factor: 1 normal, 2 twice as slow, 0.5 twice as fast)
- worker_available: worker_id, available
- worker_skill: worker_id, skill, multiplier (duration factor for that skill), or remove_skill true
- stock: ingredient_id, stock_mode "set" or "add", amount (amount may be negative with "add")
- add_order: items [{recipe, quantity, modifiers}], deadline_minutes (after next_minute, at most run_deadline)
- order_deadline: order_id, deadline_minutes
- announce: text (one short sentence telling the workers what happened or what matters now)

Rules:
- Use only IDs, recipes and modifiers that appear in the kitchen JSON.
- Never give a worker a skill they do not have. You may slow down or remove existing skills.
- Multipliers are between 0.25 and 5.
- "Fixed", "back", "over", "resolved" mean undo the earlier effect: station working, multiplier 1,
  capacity back to normal_capacity, worker available, skill back to its normal_skills value.
- Kitchen-wide events (fire alarm, power cut) affect every station listed.
- Add one announce when the workers should know about the situation.
- If nothing physical changes, return only an announce. If the text is not about the
  kitchen at all, return no changes and say so in the summary.
- "why" is a few words explaining each change. "summary" is one sentence for the manager.
"""


class ScenarioError(Exception):
    """No valid interpretation could be obtained; attempts are sanitized."""

    def __init__(self, message, model=None, attempts=None):
        super().__init__(message)
        self.model = model
        self.attempts = attempts or []


# --------------------------------------------------------------------------
# Context and conversion
# --------------------------------------------------------------------------

def kitchen_context(state):
    """Compact, JSON-serializable description of the kitchen for the agent."""
    config = state.get("config") or {}
    menu = config.get("menu") or {}
    normal_capacity = {sid: rec.get("capacity") for sid, rec in (menu.get("stations") or {}).items()} \
        if isinstance(menu.get("stations"), dict) else {}
    normal_skills = {}
    for w in config.get("workers") or []:
        if isinstance(w, dict) and "id" in w:
            normal_skills[w["id"]] = w.get("skills", {})
    orders = state.get("orders") or {}
    return {
        "time": state.get("time"),
        "next_minute": state.get("next_tick"),
        "run_deadline": state.get("deadline"),
        "stations": [
            {"id": sid, "name": st.get("name"), "working": st.get("available", True),
             "capacity": st.get("capacity"), "normal_capacity": normal_capacity.get(sid, st.get("capacity")),
             "in_use": len(st.get("active_tasks") or []), "slowdown": st.get("duration_multiplier", 1)}
            for sid, st in sorted((state.get("stations") or {}).items())],
        "workers": [
            {"id": wid, "name": w.get("name"), "role": w.get("role"), "available": w.get("available"),
             "current_task": w.get("current_task"), "skills": w.get("skills"),
             "normal_skills": normal_skills.get(wid, w.get("skills"))}
            for wid, w in sorted((state.get("workers") or {}).items())],
        "ingredients": [
            {"id": iid, "name": rec.get("name"), "unit": rec.get("unit"),
             "available": rec.get("available"), "reserved": rec.get("reserved")}
            for iid, rec in sorted((state.get("ingredients") or {}).items())],
        "recipes": {rid: {"name": r.get("name"), "modifiers": sorted(r.get("modifiers") or {})}
                    for rid, r in sorted((menu.get("recipes") or {}).items())},
        "open_orders": [
            {"id": oid, "status": o.get("status"), "deadline": o.get("deadline_minutes"), "items": o.get("items")}
            for oid, o in sorted(orders.items()) if o.get("correct") is None],
        "active_incidents": sorted(state.get("incidents") or {}),
        "recent_announcements": [a.get("text") for a in (state.get("announcements") or [])[-3:]],
    }


def to_engine_command(change):
    """Map one agent change onto the engine's command vocabulary."""
    kind = change.get("type")
    why = change.get("why") or ""
    if kind == "station_available":
        cmd = {"type": "station", "station_id": change.get("station_id"), "available": change.get("available")}
    elif kind == "station_capacity":
        cmd = {"type": "station_capacity", "station_id": change.get("station_id"), "capacity": change.get("capacity")}
    elif kind == "station_speed":
        cmd = {"type": "station_speed", "station_id": change.get("station_id"), "multiplier": change.get("multiplier")}
    elif kind == "worker_available":
        cmd = {"type": "worker", "worker_id": change.get("worker_id"), "available": change.get("available")}
    elif kind == "worker_skill":
        cmd = {"type": "worker_skill", "worker_id": change.get("worker_id"), "skill": change.get("skill")}
        if change.get("remove_skill"):
            cmd["remove"] = True
        else:
            cmd["multiplier"] = change.get("multiplier")
    elif kind == "stock":
        cmd = {"type": "stock", "ingredient_id": change.get("ingredient_id"),
               "mode": change.get("stock_mode") if change.get("stock_mode") != "none" else None,
               "amount": change.get("amount")}
    elif kind == "add_order":
        cmd = {"type": "add_order", "items": change.get("items"), "deadline_minutes": change.get("deadline_minutes")}
    elif kind == "order_deadline":
        cmd = {"type": "order_deadline", "order_id": change.get("order_id"),
               "deadline_minutes": change.get("deadline_minutes")}
    elif kind == "announce":
        cmd = {"type": "announce", "text": change.get("text")}
    else:
        cmd = {"type": kind}
    cmd["why"] = why
    return cmd


def build_payload(text, state, model):
    context = kitchen_context(state)
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Kitchen:\n%s\n\nManager says: %s" % (
                json.dumps(context, sort_keys=True), text)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "kitchen_changes", "strict": True, "schema": RESPONSE_SCHEMA},
        },
        "provider": {"require_parameters": True},
        "max_tokens": MAX_TOKENS,
    }


def parse_response(status, text):
    if status != 200:
        raise _AttemptFailure("http_error", "HTTP %s: %s" % (status, text), http_status=status,
                              retryable=status not in NON_RETRYABLE_STATUS)
    try:
        body = json.loads(text)
        if isinstance(body, dict) and body.get("error"):
            raise _AttemptFailure("provider_error", "provider error: %s" % json.dumps(body["error"]))
        choice = body["choices"][0]
        message = choice["message"]
    except _AttemptFailure:
        raise
    except (ValueError, KeyError, IndexError, TypeError):
        raise _AttemptFailure("bad_response", "unexpected response: %s" % text)
    if message.get("refusal"):
        raise _AttemptFailure("refusal", "model refused: %s" % message["refusal"])
    if choice.get("finish_reason") == "length":
        raise _AttemptFailure("truncated", "output cut off at max_tokens=%d" % MAX_TOKENS)
    try:
        obj = json.loads(message.get("content") or "")
    except ValueError:
        raise _AttemptFailure("invalid_json", "content is not JSON: %s" % message.get("content"))
    if not isinstance(obj, dict) or not isinstance(obj.get("changes"), list) \
            or not isinstance(obj.get("summary"), str):
        raise _AttemptFailure("invalid_shape", "expected {summary, changes}: %s" % message.get("content"))
    for change in obj["changes"]:
        if not isinstance(change, dict) or change.get("type") not in CHANGE_TYPES:
            raise _AttemptFailure("invalid_change", "unknown change: %s" % json.dumps(change))
    return obj


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def interpret(text, state, *, model=None, api_key=None, timeout=30.0, transport=None,
              clock=time.monotonic):
    """Return {"summary", "changes": [engine commands], "model", "latency_ms", "attempts"}.

    Raises ScenarioError after at most two attempts.  Pure with respect to
    ``state``: it is only read to describe the kitchen.
    """
    model = model or os.environ.get("OPENROUTER_SCENARIO_MODEL") or os.environ.get("OPENROUTER_MODEL")
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not model:
        raise ScenarioError("OPENROUTER_MODEL is not set")
    if not api_key:
        raise ScenarioError("OPENROUTER_API_KEY is not set", model=model)
    payload = build_payload(text, state, model)
    transport = transport or urllib_transport
    attempts, started = [], clock()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempt_start = clock()
        try:
            status, body = _post(payload, api_key, timeout, transport)
            obj = parse_response(status, body)
        except _AttemptFailure as failure:
            attempts.append({"attempt": attempt, "error_type": failure.error_type,
                             "http_status": failure.http_status,
                             "latency_ms": int(round((clock() - attempt_start) * 1000)),
                             "detail": _sanitize(failure.detail, api_key)})
            if not failure.retryable:
                break
            continue
        return {"summary": obj["summary"].strip(), "changes": [to_engine_command(c) for c in obj["changes"]],
                "model": model, "latency_ms": int(round((clock() - started) * 1000)), "attempts": attempt}
    last = attempts[-1]
    raise ScenarioError("scenario agent failed after %d attempt(s): %s: %s"
                        % (len(attempts), last["error_type"], last["detail"]), model=model, attempts=attempts)
