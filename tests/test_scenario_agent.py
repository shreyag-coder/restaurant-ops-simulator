"""Scenario agent (mocked model) and the engine building blocks it drives."""

import json
import queue
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402
import policies  # noqa: E402
import scenario  # noqa: E402
import scenario_agent  # noqa: E402
from live import interactive, parse  # noqa: E402
from reporting import narrate_events, render_status  # noqa: E402
from scenario_agent import ScenarioError, interpret, kitchen_context  # noqa: E402

KEY = "sk-test-never-sent"


def new_engine():
    config = scenario.load_config(ROOT / "sim" / "config", "baseline")
    state = orchestrator.create_state(config, "det", run_id="r-agent")
    return orchestrator._engine_for(state, scenario, policies)


def change(kind, **fields):
    base = {name: None for name in scenario_agent.CHANGE_SCHEMA["properties"]}
    base["stock_mode"] = "none"
    base.update(type=kind, why=fields.pop("why", "because"), **fields)
    return base


def fake_transport(*responses):
    """Transport returning each canned (status, obj) in turn; records requests."""
    calls = []
    pending = list(responses)

    def transport(url, body, headers, timeout):
        calls.append(json.loads(body))
        status, obj = pending.pop(0)
        content = obj if isinstance(obj, str) else json.dumps(obj)
        return status, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
    transport.calls = calls
    return transport


def ok(summary, *changes):
    return 200, {"summary": summary, "changes": list(changes)}


# ---------------------------------------------------------------- agent


def test_interpret_returns_engine_commands_and_sends_kitchen_context():
    engine = new_engine()
    transport = fake_transport(ok("Dana grills slowly.",
                                  change("worker_skill", worker_id="w_grill", skill="grill", multiplier=2),
                                  change("announce", text="Dana is hurt; help on the grill.")))
    result = interpret("Dana burned her hand", engine.state, model="m/x", api_key=KEY, transport=transport)
    assert result["summary"] == "Dana grills slowly."
    assert result["changes"] == [
        {"type": "worker_skill", "worker_id": "w_grill", "skill": "grill", "multiplier": 2, "why": "because"},
        {"type": "announce", "text": "Dana is hurt; help on the grill.", "why": "because"}]
    request = transport.calls[0]
    assert request["model"] == "m/x"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert "Dana burned her hand" in request["messages"][1]["content"]
    assert '"normal_capacity"' in request["messages"][1]["content"]
    assert KEY not in json.dumps(request)


def test_interpret_retries_once_then_raises_sanitized_error():
    engine = new_engine()
    transport = fake_transport((200, "not json"), (500, "boom " + KEY))
    with pytest.raises(ScenarioError) as info:
        interpret("x", engine.state, model="m/x", api_key=KEY, transport=transport)
    assert len(transport.calls) == 2 and len(info.value.attempts) == 2
    assert KEY not in str(info.value) and KEY not in json.dumps(info.value.attempts)


def test_interpret_rejects_unknown_change_type_and_needs_credentials():
    engine = new_engine()
    bad = fake_transport(ok("?", dict(change("announce", text="hi"), type="teleport")),
                         ok("?", dict(change("announce", text="hi"), type="teleport")))
    with pytest.raises(ScenarioError, match="invalid_change"):
        interpret("x", engine.state, model="m/x", api_key=KEY, transport=bad)
    with pytest.raises(ScenarioError, match="API_KEY"):
        interpret("x", engine.state, model="m/x", api_key="", transport=bad)


def test_kitchen_context_is_json_and_does_not_mutate_state():
    engine = new_engine()
    before = json.dumps(engine.state, sort_keys=True)
    context = kitchen_context(engine.state)
    json.dumps(context)
    assert {s["id"] for s in context["stations"]} == set(engine.state["stations"])
    assert "burger" in context["recipes"]
    assert json.dumps(engine.state, sort_keys=True) == before


# ---------------------------------------------------------------- engine blocks


def test_queue_batch_applies_valid_and_reports_invalid_changes():
    engine = new_engine()
    accepted, rejected = engine.queue_batch([
        {"type": "station_speed", "station_id": "fryer", "multiplier": 2, "why": "old oil"},
        {"type": "worker_skill", "worker_id": "w_fry", "skill": "grill", "multiplier": 1},  # no such skill
        {"type": "station_capacity", "station_id": "grill", "capacity": 99},
    ], note={"text": "old oil and chaos", "summary": "s", "model": "m/x", "latency_ms": 5})
    assert [c["type"] for c in accepted] == ["station_speed"] and accepted[0]["why"] == "old oil"
    assert len(rejected) == 2
    assert "never add" in rejected[0]["error"]
    assert engine.state["operator_queue"][0]["type"] == "scenario_note"
    engine.run(1)
    assert engine.state["stations"]["fryer"]["duration_multiplier"] == 2
    note = next(e for e in engine.pending_events if e["type"] == "scenario_interpreted")
    assert note["payload"]["text"] == "old oil and chaos" and len(note["payload"]["rejected"]) == 2
    seqs = [e["seq"] for e in engine.pending_events if e["type"] in ("scenario_interpreted", "station_speed_changed")]
    assert seqs == sorted(seqs)


def test_station_slowdown_lengthens_new_tasks_and_views_agree():
    engine = new_engine()
    engine.queue_command({"type": "station_speed", "station_id": "fryer", "multiplier": 2})
    engine.run(1)
    fry = next(t for t in engine.state["tasks"].values() if t["station_id"] == "fryer" and t["status"] == "active")
    normal = new_engine()
    normal.run(1)
    normal_fry = normal.state["tasks"][fry["id"]]
    assert fry["finishes_at"] - fry["started_at"] == 2 * (normal_fry["finishes_at"] - normal_fry["started_at"])
    view = policies.build_worker_view(engine.snapshot(), "w_fry")
    fries = [t for t in view["ready_tasks"] if t["station_id"] == "fryer"]
    assert fries and all(t["my_minutes"] == 2 * 2 for t in fries)  # 2-minute fry, x2
    assert view["stations"]["fryer"]["slowdown"] == 2
    assert "slow x2" in render_status(engine.snapshot())


def test_capacity_cut_interrupts_extra_tasks():
    engine = new_engine()
    engine.run(1)
    grill = engine.state["stations"]["grill"]
    assert len(grill["active_tasks"]) == 2
    engine.queue_command({"type": "station_capacity", "station_id": "grill", "capacity": 1})
    engine.run(1)
    grill = engine.state["stations"]["grill"]
    assert grill["capacity"] == 1 and len(grill["active_tasks"]) <= 1
    reasons = [e["payload"].get("reason") for e in engine.pending_events if e["type"] == "task_interrupted"]
    assert "station_capacity" in reasons
    assert all(r["reserved"] >= 0 and r["available"] >= 0 for r in engine.state["ingredients"].values())


def test_skill_slowdown_and_removal_never_add_skills():
    engine = new_engine()
    engine.run(1)
    engine.queue_command({"type": "worker_skill", "worker_id": "w_grill", "skill": "grill", "remove": True})
    with pytest.raises(ValueError, match="never add"):
        engine.queue_command({"type": "worker_skill", "worker_id": "w_prep", "skill": "grill", "multiplier": 1})
    engine.run(1)
    assert "grill" not in engine.state["workers"]["w_grill"]["skills"]
    assert engine.state["tasks"].get(engine.state["workers"]["w_grill"]["current_task"] or "", {}).get(
        "required_skill") != "grill"


def test_add_order_grows_target_and_gets_tasks():
    engine = new_engine()
    engine.run(1)
    queued = engine.queue_command({"type": "add_order", "deadline_minutes": 20,
                                   "items": [{"recipe": "burger", "quantity": 2, "modifiers": ["no_cheese"]}]})
    assert queued["order_id"] == "O11"
    with pytest.raises(ValueError, match="Unknown recipe"):
        engine.queue_command({"type": "add_order", "deadline_minutes": 20,
                              "items": [{"recipe": "pizza", "quantity": 1, "modifiers": []}]})
    with pytest.raises(ValueError, match="Invalid order"):
        engine.queue_command({"type": "add_order", "deadline_minutes": 20,
                              "items": [{"recipe": "burger", "quantity": 1, "modifiers": ["no_pickles"]}]})
    engine.run(1)
    order = engine.state["orders"]["O11"]
    assert order["status"] == "pending" and order["arrival_minutes"] == 1
    assert engine.state["target"]["completed_orders"] == 11
    assert {t for t in engine.state["tasks"] if t.startswith("O11.")} >= {"O11.b1.patty", "O11.b2.patty", "O11.pack"}
    text = "\n".join(narrate_events(engine.snapshot(), engine.pending_events))
    assert "Manager: Adding a new order O11: 2 burgers (no cheese). Due by minute 20." in text
    assert "Kitchen: New order O11 came in" in text


def test_order_deadline_change():
    engine = new_engine()
    engine.run(1)
    engine.queue_command({"type": "order_deadline", "order_id": "O2", "deadline_minutes": 9})
    with pytest.raises(ValueError):
        engine.queue_command({"type": "order_deadline", "order_id": "O2", "deadline_minutes": 99})
    engine.run(1)
    assert engine.state["orders"]["O2"]["deadline_minutes"] == 9
    assert "Order Brooke #202 is now due by minute 9 (was 14)" in "\n".join(
        narrate_events(engine.snapshot(), engine.pending_events))


# ---------------------------------------------------------------- console


def test_console_sends_free_text_to_agent_and_keeps_builtins_local():
    engine, out, seen = new_engine(), [], []

    def fake_interpret(text, state):
        seen.append(text)
        return {"summary": "Fryer oil is old.", "model": "m/x", "latency_ms": 900,
                "changes": [{"type": "station_speed", "station_id": "fryer", "multiplier": 1.5, "why": "old oil"},
                            {"type": "worker_skill", "worker_id": "w_fry", "skill": "grill", "multiplier": 1}]}

    inbox = queue.Queue()
    for line in ["pause", "the fryer oil is really old", "fryer broken", "step", "quit"]:
        inbox.put(line)
    interactive(engine, None, pace=0, inbox=inbox, out=out.append, interpret=fake_interpret,
                render_status=render_status)
    assert seen == ["the fryer oil is really old"]  # "fryer broken" stayed a built-in command
    text = "\n".join(out)
    assert "Scenario agent (m/x, 0.9s): Fryer oil is old." in text
    assert "+ fryer works at x1.5 time" in text and "[old oil]" in text
    assert "x rejected worker_skill" in text
    assert engine.state["stations"]["fryer"]["available"] is False


def test_console_agent_failure_changes_nothing():
    engine, out = new_engine(), []

    def failing(text, state):
        raise ScenarioError("scenario agent failed after 2 attempt(s): timeout")

    inbox = queue.Queue()
    for line in ["pause", "something odd happens", "quit"]:
        inbox.put(line)
    interactive(engine, None, pace=0, inbox=inbox, out=out.append, interpret=failing)
    assert any("Scenario agent failed" in line and "Nothing was changed" in line for line in out)
    assert engine.state["operator_queue"] == []


@pytest.mark.parametrize("text", ["Dana burned her hand", "the grill is running slow", "the grill caught fire"])
def test_sentences_about_entities_reach_the_agent(text):
    kind, command = parse(text, new_engine().state)
    assert command["type"] == "announce" and command["free_text"]
